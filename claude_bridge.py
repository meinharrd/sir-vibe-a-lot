"""Per-chat bridge between Telegram and the Claude Agent SDK."""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    SystemMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    tool,
    create_sdk_mcp_server,
)

import config

log = logging.getLogger(__name__)


def fmt_cost(usd: float | None) -> str:
    """Label costs by billing mode: subscription usage is an estimate (≈),
    API-key usage is an actual charge ($)."""
    if usd is None:
        return ""
    if config.BILLING_MODE == "subscription":
        return f" · ≈${usd:.4f} plan usage"
    return f" · ${usd:.4f} billed"

SYSTEM_APPEND = """
You are being used through a Telegram bot bridge (not a terminal).

Output rules:
- Responses are rendered as Telegram messages. Prefer short paragraphs and
  small code blocks. Avoid wide tables and ASCII art.
- To show the user an image, chart, screenshot, or photo you produced or
  found on disk, call mcp__telegram__send_photo with its file path.
- To give the user any other file, call mcp__telegram__send_file.
- To speak to the user out loud, call mcp__telegram__send_voice with the text
  to say. Use it when the user asks to "hear" something or asks for a voice
  reply.
- Files the user sends you (photos, documents, voice notes) are saved on disk
  and their paths are included in the message; you can Read them.

Self-maintenance: this bot's own source code lives in
/home/ubuntu/telegram-claude-bot (bot.py, claude_bridge.py, audio.py,
formatting.py, config.py; git repo; venv at .venv). When the user asks you to
change or improve the bot:
1. Edit the code there.
2. Verify it compiles: .venv/bin/python -m py_compile bot.py claude_bridge.py
   audio.py formatting.py config.py
3. Commit your change to git (so it can be rolled back with git revert).
4. Tell the user what you changed, then apply it with a DELAYED restart:
   sudo systemd-run --on-active=5 systemctl restart telegram-claude-bot
NEVER run `systemctl restart telegram-claude-bot` directly - you are running
inside that service, and an immediate restart kills you before your reply
reaches the user. The delayed restart fires after your reply is delivered,
and the conversation resumes automatically afterwards. If the bot ever fails
to come back, the user can roll back over SSH with git revert + systemctl
restart.
""".strip()


@dataclass
class ChatState:
    session_id: str | None = None
    cwd: str = config.DEFAULT_CWD
    model: str | None = None
    mode: str = "ask"           # "ask" | "auto"
    voice: str = "auto"         # "off" | "auto" | "always"
    always_allowed: list[str] = field(default_factory=list)
    last_cost: float | None = None
    total_cost: float = 0.0


class TelegramIO:
    """Implemented by bot.py — everything the bridge needs from Telegram."""
    async def send_text(self, chat_id: int, text: str, markdown: bool = True): ...
    async def send_photo(self, chat_id: int, path: str, caption: str = ""): ...
    async def send_file(self, chat_id: int, path: str, caption: str = ""): ...
    async def send_voice_text(self, chat_id: int, text: str): ...
    async def status_update(self, chat_id: int, text: str): ...
    async def status_done(self, chat_id: int, text: str): ...
    async def ask_permission(self, chat_id: int, text: str) -> str: ...
    """Returns "allow" | "deny" | "always"."""


class ChatSession:
    def __init__(self, chat_id: int, state: ChatState, io: TelegramIO,
                 on_state_change: Callable[[], None]):
        self.chat_id = chat_id
        self.state = state
        self.io = io
        self._save = on_state_change
        self.client: ClaudeSDKClient | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.busy = False
        self._needs_reconnect = False

    # ---------- MCP tools Claude can call to reach the user ----------

    def _mcp_server(self):
        chat_id, io = self.chat_id, self.io

        @tool("send_photo", "Send an image file to the user on Telegram.",
              {"path": str, "caption": str})
        async def send_photo(args: dict[str, Any]):
            p = Path(args["path"]).expanduser()
            if not p.is_file():
                return {"content": [{"type": "text",
                                     "text": f"Error: file not found: {p}"}],
                        "isError": True}
            await io.send_photo(chat_id, str(p), args.get("caption", ""))
            return {"content": [{"type": "text", "text": f"Photo {p} sent."}]}

        @tool("send_file", "Send any file (document) to the user on Telegram.",
              {"path": str, "caption": str})
        async def send_file(args: dict[str, Any]):
            p = Path(args["path"]).expanduser()
            if not p.is_file():
                return {"content": [{"type": "text",
                                     "text": f"Error: file not found: {p}"}],
                        "isError": True}
            await io.send_file(chat_id, str(p), args.get("caption", ""))
            return {"content": [{"type": "text", "text": f"File {p} sent."}]}

        @tool("send_voice",
              "Speak text to the user as a Telegram voice note (TTS). "
              "Plain conversational text only - no markdown or code.",
              {"text": str})
        async def send_voice(args: dict[str, Any]):
            await io.send_voice_text(chat_id, args["text"])
            return {"content": [{"type": "text", "text": "Voice note sent."}]}

        return create_sdk_mcp_server(
            name="telegram", version="1.0.0",
            tools=[send_photo, send_file, send_voice],
        )

    # ---------- permissions ----------

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context):
        if tool_name in config.SAFE_TOOLS or tool_name in self.state.always_allowed:
            return PermissionResultAllow()
        from formatting import tool_summary
        summary = tool_summary(tool_name, tool_input)
        try:
            answer = await asyncio.wait_for(
                self.io.ask_permission(self.chat_id, summary),
                timeout=config.PERMISSION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return PermissionResultDeny(
                message="The user did not respond to the permission prompt in time.")
        if answer == "always":
            self.state.always_allowed.append(tool_name)
            self._save()
            return PermissionResultAllow()
        if answer == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message="The user denied this tool call.")

    # ---------- client lifecycle ----------

    def _build_options(self) -> ClaudeAgentOptions:
        ask = self.state.mode == "ask"
        return ClaudeAgentOptions(
            cwd=self.state.cwd,
            model=self.state.model,
            resume=self.state.session_id,
            permission_mode="default" if ask else "bypassPermissions",
            can_use_tool=self._can_use_tool if ask else None,
            setting_sources=["user", "project", "local"],
            mcp_servers={"telegram": self._mcp_server()},
            allowed_tools=[
                "mcp__telegram__send_photo",
                "mcp__telegram__send_file",
                "mcp__telegram__send_voice",
            ],
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND},
        )

    async def _ensure_client(self):
        if self.client is not None and self._needs_reconnect:
            await self._disconnect()
        if self.client is None:
            self.client = ClaudeSDKClient(options=self._build_options())
            await self.client.connect()
            self._needs_reconnect = False

    async def _disconnect(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def mark_dirty(self):
        """Options changed (model/cwd/mode) — reconnect before next prompt,
        resuming the same session where possible."""
        self._needs_reconnect = True

    async def reset(self):
        """/new — forget the session and start fresh."""
        await self._disconnect()
        self.state.session_id = None
        self._save()

    async def interrupt(self):
        if self.client is not None and self.busy:
            await self.client.interrupt()
            return True
        return False

    # ---------- prompt execution ----------

    def submit(self, prompt, want_voice: bool = False):
        """Queue a prompt (str, or dict for rich content). Starts worker."""
        self.queue.put_nowait((prompt, want_voice))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self._worker())
        return self.queue.qsize()

    async def _worker(self):
        while not self.queue.empty():
            prompt, want_voice = await self.queue.get()
            self.busy = True
            try:
                text = await self._run(prompt)
                if want_voice and text:
                    try:
                        await self.io.send_voice_text(self.chat_id, text)
                    except Exception as e:
                        log.warning("TTS failed: %s", e)
            except Exception as e:
                log.exception("query failed for chat %s", self.chat_id)
                self._needs_reconnect = True
                await self.io.send_text(
                    self.chat_id, f"⚠️ Claude session error: {e}", markdown=False)
            finally:
                self.busy = False

    async def _run(self, prompt):
        await self._ensure_client()
        started = time.time()

        if isinstance(prompt, dict):  # rich content (images etc.)
            async def gen():
                yield {"type": "user",
                       "message": {"role": "user", "content": prompt["content"]}}
            await self.client.query(gen())
        else:
            await self.client.query(prompt)

        collected_text: list[str] = []
        async for message in self.client.receive_response():
            if isinstance(message, SystemMessage):
                if message.subtype == "init":
                    sid = message.data.get("session_id")
                    if sid:
                        self.state.session_id = sid
                        self._save()
                elif message.subtype == "compact_boundary":
                    await self.io.status_update(self.chat_id, "🗜 compacted context")
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        collected_text.append(block.text)
                        await self.io.send_text(self.chat_id, block.text)
                    elif isinstance(block, ToolUseBlock):
                        from formatting import tool_summary
                        await self.io.status_update(
                            self.chat_id,
                            "🔧 " + tool_summary(block.name, block.input or {}))
            elif isinstance(message, ResultMessage):
                self.state.session_id = message.session_id
                if message.total_cost_usd is not None:
                    self.state.last_cost = message.total_cost_usd
                    self.state.total_cost += message.total_cost_usd
                self._save()
                elapsed = time.time() - started
                cost = fmt_cost(message.total_cost_usd)
                if message.subtype == "success":
                    await self.io.status_done(
                        self.chat_id, f"✅ done in {elapsed:.0f}s{cost}")
                    # /compact, /context etc. return their output only in
                    # result.result — surface it if nothing was streamed.
                    if not collected_text and message.result:
                        await self.io.send_text(self.chat_id, message.result)
                else:
                    await self.io.status_done(
                        self.chat_id,
                        f"⚠️ ended: {message.subtype} ({elapsed:.0f}s){cost}")

        return "\n\n".join(collected_text)


class ChatManager:
    """Owns all per-chat sessions and persists their state."""

    def __init__(self, io: TelegramIO):
        self.io = io
        self.sessions: dict[int, ChatSession] = {}
        self.states: dict[int, ChatState] = {}
        self._load()

    def _load(self):
        if config.STATE_FILE.exists():
            try:
                raw = json.loads(config.STATE_FILE.read_text())
                for cid, s in raw.items():
                    self.states[int(cid)] = ChatState(**s)
            except Exception:
                log.exception("could not load state file")

    def save(self):
        data = {str(cid): vars(s) for cid, s in self.states.items()}
        config.STATE_FILE.write_text(json.dumps(data, indent=2))

    def get(self, chat_id: int) -> ChatSession:
        if chat_id not in self.sessions:
            state = self.states.setdefault(chat_id, ChatState())
            self.sessions[chat_id] = ChatSession(
                chat_id, state, self.io, self.save)
        return self.sessions[chat_id]


def image_prompt(image_path: str, caption: str) -> dict:
    """Build a rich prompt containing an image + text."""
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    suffix = Path(image_path).suffix.lower()
    media = {".png": "image/png", ".webp": "image/webp",
             ".gif": "image/gif"}.get(suffix, "image/jpeg")
    text = caption.strip() if caption.strip() else "The user sent this image."
    text += f"\n(Saved on disk at {image_path})"
    return {"content": [
        {"type": "image",
         "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": text},
    ]}
