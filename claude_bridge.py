"""Per-chat bridge between Telegram and the Claude Agent SDK."""
import asyncio
import base64
import functools
import json
import logging
import mmap
import os
import re
import shutil
import subprocess
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
    RateLimitEvent,
    TextBlock,
    ToolUseBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    tool,
    create_sdk_mcp_server,
)

import config

log = logging.getLogger(__name__)


# Usage-limit notices from Claude Code. Seen shapes:
#   "You've hit your session limit · resets 9:10pm (UTC)"
#   "5-hour limit reached ∙ resets 3am"
#   "Claude AI usage limit reached|1734393600"   (older CLI, epoch suffix)
_LIMIT_RE = re.compile(
    r"hit your (?:session|usage) limit|usage limit reached|hour limit reached",
    re.I)
_RESET_EPOCH_RE = re.compile(r"\|\s*(\d{9,})")
_RESET_TIME_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def parse_limit_reset(text: str | None) -> float | None:
    """Epoch seconds when `text` is a usage-limit notice, else None.
    Times without a date are interpreted as the next occurrence in UTC
    (the notices render UTC on this host). Unparseable reset → +30 min."""
    if not text or not _LIMIT_RE.search(text):
        return None
    now = time.time()
    m = _RESET_EPOCH_RE.search(text)
    if m:
        return float(m.group(1))
    m = _RESET_TIME_RE.search(text)
    if m:
        hour = int(m.group(1)) % 12
        if (m.group(3) or "").lower() == "pm":
            hour += 12
        minute = int(m.group(2) or 0)
        t = time.gmtime(now)
        import calendar
        reset = calendar.timegm(
            (t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, 0))
        while reset <= now:
            reset += 12 * 3600  # am/pm ambiguity: try the next half-day
        return reset
    return now + 30 * 60


def fmt_reset(ts: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts))


def fmt_cost(usd: float | None) -> str:
    """Label costs by billing mode: subscription usage is an estimate (≈),
    API-key usage is an actual charge ($)."""
    if usd is None:
        return ""
    if config.BILLING_MODE == "subscription":
        return f" · ≈${usd:.4f} plan usage"
    return f" · ${usd:.4f} billed"

SYSTEM_APPEND = f"""
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
{config.PROJECT_DIR} (bot.py, claude_bridge.py, audio.py,
formatting.py, config.py, login.py; git repo; venv at .venv). When the user
asks you to change or improve the bot:
1. Edit the code there.
2. Verify it compiles: .venv/bin/python -m py_compile bot.py claude_bridge.py
   audio.py formatting.py config.py login.py
3. Commit your change to git (so it can be rolled back with git revert).
4. Tell the user what you changed, then apply it with a DELAYED restart:
   sudo systemd-run --on-active=5 systemctl restart {config.SERVICE_NAME}
NEVER run `systemctl restart {config.SERVICE_NAME}` directly - you are running
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
    model: str | None = None        # what the user requested (None = default)
    active_model: str | None = None  # actual model id reported by the SDK
    mode: str = "auto"          # "ask" | "auto"
    voice: str = "off"          # "off" | "auto" | "always"
    always_allowed: list[str] = field(default_factory=list)
    last_cost: float | None = None
    total_cost: float = 0.0


def _project_dir(cwd: str) -> Path:
    """Claude Code's transcript directory for a working directory."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


MAX_CONTEXT_TOKENS = 200_000

MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")


def _cli_path() -> str | None:
    """The Claude Code binary the SDK will spawn (mirrors its lookup order:
    the SDK's bundled copy first, then PATH, then ~/.local/bin)."""
    import claude_agent_sdk
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    if found := shutil.which("claude"):
        return found
    local = Path.home() / ".local/bin/claude"
    return str(local) if local.is_file() else None


@functools.lru_cache(maxsize=1)
def cli_version() -> str | None:
    path = _cli_path()
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("claude --version failed: %s", e)
        return None
    return out.split()[0] if out else None  # "2.1.257 (Claude Code)" -> "2.1.257"


@functools.lru_cache(maxsize=1)
def model_aliases() -> dict[str, str]:
    """What each short alias (fable/opus/sonnet/haiku) resolves to.

    Aliases are resolved client-side inside the Claude Code binary, so the
    table is read out of the binary the SDK actually runs — the host's
    `claude` may be a different version.  ANTHROPIC_DEFAULT_<ALIAS>_MODEL
    env vars override the built-in mapping, as they do in the CLI.
    """
    table: dict[str, str] = {}
    path = _cli_path()
    if path:
        # A regex over the ~200 MB binary takes seconds; a literal find for the
        # rare `:"claude-` marker and a local match around each hit is fast.
        pat = re.compile(rb'\b(fable|opus|sonnet|haiku):"(claude-[a-z0-9-]+)"')
        try:
            with open(path, "rb") as f, \
                    mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                pos = 0
                while (pos := mm.find(b':"claude-', pos)) != -1:
                    if m := pat.search(mm, max(0, pos - 6), pos + 40):
                        table.setdefault(m.group(1).decode(), m.group(2).decode())
                    pos += 1
        except (OSError, ValueError) as e:
            log.warning("could not read model aliases from %s: %s", path, e)
    for alias in MODEL_ALIASES:
        if env := os.environ.get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"):
            table[alias] = env
    return table


def default_model_setting() -> str | None:
    """What `/model default` falls back to: ANTHROPIC_MODEL, else the `model`
    key in ~/.claude/settings.json (the SDK loads user settings)."""
    if env := os.environ.get("ANTHROPIC_MODEL"):
        return env
    try:
        return json.loads((Path.home() / ".claude/settings.json").read_text()).get("model")
    except (OSError, ValueError, AttributeError):
        return None


def resolve_model(name: str | None) -> str | None:
    """Full model id for an alias, a full id, or None (= default)."""
    if name is None:
        name = default_model_setting()
        if name is None:
            return None
    return model_aliases().get(name.lower(), name)


def _session_meta(path: Path) -> dict | None:
    """Title, model, context size and turn count from a session transcript.

    Returns None for transcripts with no real user message (e.g. warmups).
    """
    title = None
    preview = None
    model = None
    ctx_tokens = 0
    turns = 0
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("type")
                if t == "ai-title":
                    title = rec.get("aiTitle") or title
                elif t == "user":
                    content = rec.get("message", {}).get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
                    else:
                        continue
                    text = " ".join(text.split())
                    if not text or text.startswith("<"):
                        continue  # system reminders / command wrappers
                    turns += 1
                    if preview is None:
                        preview = text[:200]
                elif t == "assistant":
                    msg = rec.get("message", {})
                    model = msg.get("model") or model
                    usage = msg.get("usage") or {}
                    tokens = (usage.get("input_tokens", 0)
                              + usage.get("cache_read_input_tokens", 0)
                              + usage.get("cache_creation_input_tokens", 0))
                    if tokens:
                        ctx_tokens = tokens
    except OSError:
        return None
    if preview is None:
        return None
    if model:  # "claude-fable-5" -> "fable-5"
        model = re.sub(r"^claude-", "", re.sub(r"-\d{8}$", "", model))
    return {"title": title or preview, "model": model, "turns": turns,
            "ctx_pct": min(100, round(ctx_tokens * 100 / MAX_CONTEXT_TOKENS))}


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
        self.resume_choices: list[str] = []  # ids from the last /resume listing
        # Continuous message receiver (one per connected client). Turns can
        # start without a user prompt (background tasks re-invoke the agent
        # when they finish), so replies must be consumed and delivered
        # whether or not a query is in flight — a per-query
        # receive_response() loop silently drops those turns and then feeds
        # the buffered leftovers to the *next* query, desyncing every reply
        # after the first background task.
        self._receiver: asyncio.Task | None = None
        self._turn_done: asyncio.Future | None = None
        self._collected: list[str] = []
        self._turn_started: float = 0.0
        # Usage-limit backoff: epoch until which prompts are held. Set when a
        # turn ends in a limit notice; the worker sleeps it out and retries,
        # so the bot stays responsive (acks + queues) instead of erroring.
        self.limited_until: float = 0.0
        self._hit_limit = False

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
        env = {}
        token = config.load_oauth_token()
        if token:  # from the /login flow — overrides the host's ~/.claude login
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return ClaudeAgentOptions(
            env=env,
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
            # The SDK defaults to 1 MiB per JSON line from the CLI; large tool
            # results (big file reads, long command output) exceed that and
            # kill the session with "JSON message exceeded maximum buffer size".
            max_buffer_size=64 * 1024 * 1024,
        )

    async def _ensure_client(self):
        if self.client is not None and self._needs_reconnect:
            await self._disconnect()
        if self.client is None:
            sid = self.state.session_id
            if sid and not (_project_dir(self.state.cwd) / f"{sid}.jsonl").is_file():
                # A resume id with no transcript for this cwd makes the CLI
                # exit 1 ("No conversation found") on every connect, wedging
                # the chat until the id is cleared.
                log.warning("chat %s: dropping stale session %s (no transcript in %s)",
                            self.chat_id, sid, self.state.cwd)
                self.state.session_id = None
                self._save()
                await self.io.send_text(
                    self.chat_id,
                    "⚠️ The previous session's transcript is missing for this "
                    "working directory — starting a fresh session.",
                    markdown=False)
            self.client = ClaudeSDKClient(options=self._build_options())
            await self.client.connect()
            self._needs_reconnect = False
            self._receiver = asyncio.get_running_loop().create_task(
                self._receive_loop())

    async def _disconnect(self):
        if self._receiver is not None:
            self._receiver.cancel()
            self._receiver = None
        fut = self._turn_done
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError("client disconnected mid-turn"))
        self._turn_done = None
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

    def list_sessions(self, limit: int = 8) -> list[dict]:
        """Recent Claude sessions for this chat's cwd, newest first."""
        pdir = _project_dir(self.state.cwd)
        out = []
        for f in sorted(pdir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            meta = _session_meta(f)
            if meta is None:
                continue
            meta.update(id=f.stem, mtime=f.stat().st_mtime,
                        current=f.stem == self.state.session_id)
            out.append(meta)
            if len(out) >= limit:
                break
        return out

    async def resume(self, session_id: str):
        """/resume — continue an earlier session of this cwd."""
        await self._disconnect()
        self.state.session_id = session_id
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
        if time.time() < self.limited_until:
            asyncio.get_running_loop().create_task(self.io.send_text(
                self.chat_id,
                f"⏳ Claude usage limit is active — queued; I'll process this "
                f"automatically after the reset (~{fmt_reset(self.limited_until)}).",
                markdown=False))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self._worker())
        return self.queue.qsize()

    async def _wait_if_limited(self):
        """Sleep out an active usage-limit window (chunked, so a restart or
        an earlier-than-advertised reset doesn't strand the queue long)."""
        while True:
            wait = self.limited_until - time.time()
            if wait <= 0:
                return
            await asyncio.sleep(min(wait + 5, 300))

    async def _worker(self):
        while not self.queue.empty():
            await self._wait_if_limited()
            prompt, want_voice = await self.queue.get()
            self.busy = True
            self._hit_limit = False
            try:
                text = await self._run(prompt)
                if self._hit_limit:
                    # The turn died on the usage limit: re-queue the prompt and
                    # let the loop sleep out the window, then retry it.
                    self.queue.put_nowait((prompt, want_voice))
                    continue
                if want_voice and text:
                    try:
                        await self.io.send_voice_text(self.chat_id, text)
                    except Exception as e:
                        log.warning("TTS failed: %s", e)
            except Exception as e:
                reset_ts = parse_limit_reset(str(e))
                if reset_ts:
                    # Limit surfaced as an exception (e.g. connect failure):
                    # same treatment — back off, re-queue, retry.
                    self.limited_until = max(self.limited_until, reset_ts)
                    self._needs_reconnect = True
                    self.queue.put_nowait((prompt, want_voice))
                    await self.io.send_text(
                        self.chat_id,
                        f"⏳ Claude usage limit reached — I'll retry "
                        f"automatically after the reset (~{fmt_reset(self.limited_until)}).",
                        markdown=False)
                    continue
                log.exception("query failed for chat %s", self.chat_id)
                self._needs_reconnect = True
                await self.io.send_text(
                    self.chat_id, f"⚠️ Claude session error: {e}", markdown=False)
            finally:
                self.busy = False

    async def _run(self, prompt):
        await self._ensure_client()
        self._turn_started = time.time()
        fut = asyncio.get_running_loop().create_future()
        self._turn_done = fut

        if isinstance(prompt, dict):  # rich content (images etc.)
            async def gen():
                yield {"type": "user",
                       "message": {"role": "user", "content": prompt["content"]}}
            await self.client.query(gen())
        else:
            await self.client.query(prompt)

        # The receiver resolves the future at the next end-of-turn
        # (ResultMessage). If the prompt got injected into a turn that a
        # background task started, that turn's end covers the reply too.
        try:
            return await fut
        finally:
            if self._turn_done is fut:
                self._turn_done = None

    async def _on_rate_limit(self, info):
        """Surface Claude rate-limit transitions with an ETA. The CLI emits
        these once per state change, so a warning is shown once, and a
        rejection sets limited_until so the worker knows how long to wait."""
        window = (info.rate_limit_type or "usage").replace("_", " ")
        eta = f" · resets ~{fmt_reset(info.resets_at)}" if info.resets_at else ""
        if info.status == "allowed_warning":
            used = (f" · {info.utilization * 100:.0f}% used"
                    if info.utilization is not None else "")
            await self.io.status_update(
                self.chat_id, f"⚠️ approaching Claude {window} limit{used}{eta}")
        elif info.status == "rejected":
            if info.resets_at:
                self.limited_until = max(self.limited_until, float(info.resets_at))
            await self.io.status_update(
                self.chat_id,
                f"⏳ Claude {window} limit hit — waiting for the reset{eta}. "
                f"Messages sent meanwhile are queued.")

    async def _receive_loop(self):
        """Consume and deliver every message the agent produces, for the
        lifetime of the client — including turns no query started."""
        try:
            async for message in self.client.receive_messages():
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("receiver died for chat %s", self.chat_id)
            self._needs_reconnect = True
            fut = self._turn_done
            if fut is not None and not fut.done():
                fut.set_exception(e)

    async def _handle_message(self, message):
        if not self._turn_started:  # turn started by a background task
            self._turn_started = time.time()
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                sid = message.data.get("session_id")
                if sid and not self._needs_reconnect:
                    self.state.session_id = sid
                active = message.data.get("model")
                if active:
                    self.state.active_model = active
                self._save()
            elif message.subtype == "compact_boundary":
                await self.io.status_update(self.chat_id, "🗜 compacted context")
        elif isinstance(message, RateLimitEvent):
            await self._on_rate_limit(message.rate_limit_info)
        elif isinstance(message, AssistantMessage):
            if message.model:
                self.state.active_model = message.model
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self._collected.append(block.text)
                    await self.io.send_text(self.chat_id, block.text)
                elif isinstance(block, ToolUseBlock):
                    from formatting import tool_summary
                    await self.io.status_update(
                        self.chat_id,
                        "🔧 " + tool_summary(block.name, block.input or {}))
        elif isinstance(message, ResultMessage):
            if not self._needs_reconnect:
                # After /cwd (or /model, /mode) marked the client dirty, this
                # id belongs to the old options — writing it back would undo
                # e.g. the session reset that /cwd just did.
                self.state.session_id = message.session_id
            if message.total_cost_usd is not None:
                self.state.last_cost = message.total_cost_usd
                self.state.total_cost += message.total_cost_usd
            self._save()
            elapsed = time.time() - self._turn_started
            cost = fmt_cost(message.total_cost_usd)
            reset_ts = parse_limit_reset(message.result or "")
            if reset_ts:
                # Usage limit — regardless of subtype. Flag the turn so the
                # worker re-queues its prompt, and tell the user once with
                # the reset time instead of surfacing the raw notice.
                self.limited_until = max(self.limited_until, reset_ts)
                self._hit_limit = True
                self._needs_reconnect = True
                await self.io.status_done(
                    self.chat_id,
                    f"⏳ Claude usage limit reached — resuming automatically "
                    f"after the reset (~{fmt_reset(self.limited_until)}). "
                    f"Messages sent meanwhile are queued.")
            elif message.subtype == "success":
                await self.io.status_done(
                    self.chat_id, f"✅ done in {elapsed:.0f}s{cost}")
                # /compact, /context etc. return their output only in
                # result.result — surface it if nothing was streamed.
                if not self._collected and message.result:
                    await self.io.send_text(self.chat_id, message.result)
            else:
                await self.io.status_done(
                    self.chat_id,
                    f"⚠️ ended: {message.subtype} ({elapsed:.0f}s){cost}")
            text = "\n\n".join(self._collected)
            self._collected = []
            self._turn_started = 0.0
            fut = self._turn_done
            if fut is not None and not fut.done():
                fut.set_result(text)


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
