"""Telegram bot that bridges chats to Claude Code via the Agent SDK."""
import asyncio
import html
import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import audio
import config
from claude_bridge import ChatManager, TelegramIO, image_prompt
from formatting import md_to_telegram_html, split_message

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

HELP = """<b>Sir Vibe-a-lot</b> — Claude Code on Telegram

Send any text, voice note, photo, or file — it goes straight to Claude.

<b>Bot commands</b>
/new — start a fresh session
/resume — list &amp; resume earlier sessions
/stop — interrupt the current run
/status — session info &amp; cost
/model <i>[opus|sonnet|haiku|default]</i> — switch model
/mode <i>[ask|auto]</i> — tool permissions: ask via buttons, or auto-approve
/voice <i>[off|auto|always]</i> — voice replies (auto = reply to voice with voice)
/cwd <i>[path]</i> — change Claude's working directory
/restartbot — restart the bot process (after code changes)
/help — this message

<b>Claude slash commands</b>
Anything else starting with / is passed to Claude Code itself:
/compact, /context, /usage, /code-review, plus your custom skills.
"""


class TgIO(TelegramIO):
    def __init__(self):
        self.app: Application | None = None
        self.pending_perms: dict[str, asyncio.Future] = {}
        # chat_id -> (message_id, last_edit_monotonic)
        self._status: dict[int, tuple[int, float]] = {}

    # ----- plain sends -----

    async def send_text(self, chat_id: int, text: str, markdown: bool = True):
        chunks = split_message(md_to_telegram_html(text) if markdown else
                               html.escape(text))
        for chunk in chunks:
            try:
                await self.app.bot.send_message(chat_id, chunk, parse_mode="HTML",
                                                disable_web_page_preview=True)
            except Exception:
                # formatting fallback: send raw text
                await self.app.bot.send_message(
                    chat_id, chunk if not markdown else text[:4000])

    async def send_photo(self, chat_id: int, path: str, caption: str = ""):
        with open(path, "rb") as f:
            await self.app.bot.send_photo(chat_id, f, caption=caption[:1000] or None)

    async def send_file(self, chat_id: int, path: str, caption: str = ""):
        with open(path, "rb") as f:
            await self.app.bot.send_document(chat_id, f, caption=caption[:1000] or None)

    async def send_voice_text(self, chat_id: int, text: str):
        spoken = audio.speakable(text)
        if not spoken:
            return
        await self.app.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            ogg = tmp.name
        try:
            await audio.synthesize_voice(spoken, ogg)
            with open(ogg, "rb") as f:
                await self.app.bot.send_voice(chat_id, f)
        finally:
            Path(ogg).unlink(missing_ok=True)

    # ----- status line (one editable message per run) -----

    async def status_update(self, chat_id: int, text: str):
        text = text[:400]
        now = time.monotonic()
        entry = self._status.get(chat_id)
        try:
            if entry is None:
                msg = await self.app.bot.send_message(chat_id, text)
                self._status[chat_id] = (msg.message_id, now)
            else:
                msg_id, last = entry
                if now - last < 1.5:
                    return  # throttle edits
                await self.app.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=msg_id)
                self._status[chat_id] = (msg_id, now)
        except Exception:
            pass  # status is best-effort

    async def status_done(self, chat_id: int, text: str):
        entry = self._status.pop(chat_id, None)
        try:
            if entry is not None:
                await self.app.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=entry[0])
            else:
                await self.app.bot.send_message(chat_id, text)
        except Exception:
            pass

    # ----- permission prompts -----

    async def ask_permission(self, chat_id: int, summary: str) -> str:
        token = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self.pending_perms[token] = fut
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Allow", callback_data=f"p|{token}|allow"),
             InlineKeyboardButton("❌ Deny", callback_data=f"p|{token}|deny")],
            [InlineKeyboardButton("♾️ Always allow this tool",
                                  callback_data=f"p|{token}|always")],
        ])
        await self.app.bot.send_message(
            chat_id,
            f"🔐 Claude wants to run:\n<code>{html.escape(summary)}</code>",
            parse_mode="HTML", reply_markup=kb)
        try:
            return await fut
        finally:
            self.pending_perms.pop(token, None)


io = TgIO()
manager = ChatManager(io)


# ---------------- handlers ----------------

async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is not None and user.id in config.OWNER_IDS:
        return
    if update.effective_message and user is not None:
        log.warning("Unauthorized user id=%s username=%s", user.id, user.username)
        await update.effective_message.reply_text(
            f"Not authorized. Your Telegram user ID is {user.id}.\n"
            "Add it to OWNER_IDS in the bot's .env file and restart.")
    raise ApplicationHandlerStop


async def cmd_start(update: Update, context):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def cmd_new(update: Update, context):
    session = manager.get(update.effective_chat.id)
    await session.reset()
    await update.message.reply_text("🆕 Fresh session started.")


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds / 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


async def cmd_resume(update: Update, context):
    session = manager.get(update.effective_chat.id)
    arg = " ".join(context.args).strip() if context.args else ""
    if not arg:
        sessions = session.list_sessions()
        if not sessions:
            await update.message.reply_text(
                "No previous sessions found for this directory.")
            return
        session.resume_choices = [s["id"] for s in sessions]
        now = time.time()
        lines = [f"<b>Recent sessions</b> in <code>{html.escape(session.state.cwd)}</code>:"]
        buttons = []
        for i, s in enumerate(sessions, 1):
            mark = " ← current" if s["current"] else ""
            lines.append(f"{i}. <i>{_fmt_age(now - s['mtime'])}</i> — "
                         f"{html.escape(s['preview'])}{mark}")
            buttons.append([InlineKeyboardButton(
                f"{i}. {s['preview']}", callback_data=f"r|{s['id']}")])
        lines.append("\nTap a session to resume it.")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons))
        return
    if session.busy:
        await update.message.reply_text(
            "A run is in progress — /stop it first, then /resume.")
        return
    if arg.isdigit() and 1 <= int(arg) <= len(session.resume_choices):
        sid = session.resume_choices[int(arg) - 1]
    elif len(arg) >= 8 and all(c in "0123456789abcdefABCDEF-" for c in arg):
        sid = arg
    else:
        await update.message.reply_text(
            "Give a number from the /resume list, or a full session id.")
        return
    await session.resume(sid)
    await update.message.reply_text(
        f"⏪ Resumed <code>{html.escape(sid)}</code> — your next message "
        "continues that conversation.", parse_mode="HTML")


async def cmd_stop(update: Update, context):
    session = manager.get(update.effective_chat.id)
    stopped = await session.interrupt()
    await update.message.reply_text(
        "🛑 Interrupted." if stopped else "Nothing is running.")


async def cmd_status(update: Update, context):
    s = manager.get(update.effective_chat.id)
    st = s.state
    lines = [
        f"<b>session</b>: <code>{st.session_id or '(none yet)'}</code>",
        f"<b>cwd</b>: <code>{st.cwd}</code>",
        f"<b>model</b>: {_model_line(st)}",
        f"<b>permissions</b>: {st.mode}",
        f"<b>voice replies</b>: {st.voice}",
        f"<b>busy</b>: {'yes' if s.busy else 'no'}"
        + (f" · queued: {s.queue.qsize()}" if s.queue.qsize() else ""),
    ]
    lines.append(_billing_line(st))
    if st.always_allowed:
        lines.append("<b>always allowed</b>: " + ", ".join(st.always_allowed))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _model_line(st) -> str:
    if st.active_model:
        if st.model and st.model.lower() not in st.active_model.lower():
            return f"{st.active_model} (requested: {st.model})"
        return st.active_model
    return st.model or "default (resolves on first message)"


def _billing_line(st) -> str:
    if config.BILLING_MODE == "subscription":
        plan = (config.SUBSCRIPTION_PLAN or "").title()
        line = f"<b>billing</b>: Claude {plan} subscription (no per-token charges)"
    elif config.BILLING_MODE == "api":
        line = "<b>billing</b>: API key — costs are real charges"
    else:
        line = "<b>billing</b>: unknown"
    if st.last_cost is not None:
        est = "≈" if config.BILLING_MODE == "subscription" else ""
        line += (f"\n<b>last run</b>: {est}${st.last_cost:.4f}"
                 f" · <b>total</b>: {est}${st.total_cost:.2f}")
        if config.BILLING_MODE == "subscription":
            line += "\n<i>$ figures are API-equivalent estimates of plan usage, not money spent</i>"
    return line


async def cmd_cost(update: Update, context):
    st = manager.get(update.effective_chat.id).state
    await update.message.reply_text(_billing_line(st), parse_mode="HTML")


async def cmd_model(update: Update, context):
    s = manager.get(update.effective_chat.id)
    arg = " ".join(context.args).strip().lower()
    if not arg:
        await update.message.reply_text(
            f"Current model: {_model_line(s.state)}\n"
            "Usage: /model opus | sonnet | haiku | default | <full-model-id>")
        return
    s.state.model = None if arg == "default" else arg
    s.state.active_model = None  # re-resolved on the next message
    s.mark_dirty()
    manager.save()
    await update.message.reply_text(f"Model set to {arg}. Applies to the next message.")


async def cmd_mode(update: Update, context):
    s = manager.get(update.effective_chat.id)
    arg = " ".join(context.args).strip().lower()
    if arg not in ("ask", "auto"):
        await update.message.reply_text(
            f"Current mode: {s.state.mode}\n"
            "/mode ask — confirm tool calls with buttons\n"
            "/mode auto — auto-approve everything (be careful)")
        return
    s.state.mode = arg
    s.mark_dirty()
    manager.save()
    await update.message.reply_text(f"Permission mode: {arg}")


async def cmd_voice(update: Update, context):
    s = manager.get(update.effective_chat.id)
    arg = " ".join(context.args).strip().lower()
    if arg not in ("off", "auto", "always"):
        await update.message.reply_text(
            f"Current: {s.state.voice}\n"
            "/voice off — text only\n"
            "/voice auto — voice reply when you send voice\n"
            "/voice always — voice reply to everything")
        return
    s.state.voice = arg
    manager.save()
    await update.message.reply_text(f"Voice replies: {arg}")


async def cmd_cwd(update: Update, context):
    s = manager.get(update.effective_chat.id)
    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_text(f"Current cwd: {s.state.cwd}")
        return
    p = Path(arg).expanduser()
    if not p.is_dir():
        await update.message.reply_text(f"Not a directory: {p}")
        return
    s.state.cwd = str(p)
    s.state.session_id = None  # sessions are scoped to the working directory
    s.mark_dirty()
    manager.save()
    await update.message.reply_text(
        f"Working directory set to {p}. Starting a new session there.")


async def cmd_restartbot(update: Update, context):
    await update.message.reply_text("♻️ Restarting the bot… back in a few seconds.")
    subprocess.Popen(
        ["sudo", "systemd-run", "--on-active=2",
         "systemctl", "restart", config.SERVICE_NAME])


def _submit(update: Update, prompt, was_voice: bool = False):
    chat_id = update.effective_chat.id
    session = manager.get(chat_id)
    want_voice = (session.state.voice == "always"
                  or (session.state.voice == "auto" and was_voice))
    queued = session.submit(prompt, want_voice=want_voice)
    return session, queued


async def on_text(update: Update, context):
    session, queued = _submit(update, update.message.text)
    if queued > 1 or session.busy:
        await update.message.reply_text(f"⏳ Queued (position {queued}).")


async def on_unknown_command(update: Update, context):
    # Pass unrecognized /commands straight through to Claude Code
    # (/compact, /context, /usage, /code-review, custom skills, ...).
    await on_text(update, context)


async def on_voice(update: Update, context):
    msg = update.message
    media = msg.voice or msg.audio
    chat_dir = config.MEDIA_DIR / str(update.effective_chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / f"voice_{int(time.time())}.oga"
    tg_file = await media.get_file()
    await tg_file.download_to_drive(path)
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    try:
        text = await audio.transcribe(str(path))
    except Exception as e:
        await msg.reply_text(f"⚠️ Could not transcribe voice note: {e}")
        return
    if not text:
        await msg.reply_text("🎤 (couldn't hear anything in that)")
        return
    await msg.reply_text(f"🎤 <i>{html.escape(text)}</i>", parse_mode="HTML")
    _submit(update, text, was_voice=True)


async def on_photo(update: Update, context):
    msg = update.message
    chat_dir = config.MEDIA_DIR / str(update.effective_chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    photo = msg.photo[-1]  # largest size
    path = chat_dir / f"photo_{int(time.time())}.jpg"
    tg_file = await photo.get_file()
    await tg_file.download_to_drive(path)
    _submit(update, image_prompt(str(path), msg.caption or ""))


async def on_document(update: Update, context):
    msg = update.message
    doc = msg.document
    chat_dir = config.MEDIA_DIR / str(update.effective_chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(doc.file_name or f"file_{int(time.time())}").name
    path = chat_dir / f"{int(time.time())}_{safe_name}"
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(path)
    if (doc.mime_type or "").startswith("image/"):
        _submit(update, image_prompt(str(path), msg.caption or ""))
        return
    caption = msg.caption or "The user sent this file."
    _submit(update, f"{caption}\n(The file is saved at {path})")


async def on_perm_button(update: Update, context):
    query = update.callback_query
    try:
        _, token, answer = query.data.split("|")
    except ValueError:
        await query.answer()
        return
    fut = io.pending_perms.get(token)
    if fut is not None and not fut.done():
        fut.set_result(answer)
        label = {"allow": "✅ allowed", "deny": "❌ denied",
                 "always": "♾️ always allowed"}[answer]
        try:
            await query.edit_message_text(
                query.message.text_html + f"\n\n{label}",
                parse_mode="HTML")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(query.message.text_html
                                          + "\n\n(expired)", parse_mode="HTML")
        except Exception:
            pass
    await query.answer()


async def on_resume_button(update: Update, context):
    query = update.callback_query
    sid = query.data.split("|", 1)[1]
    session = manager.get(update.effective_chat.id)
    if session.busy:
        await query.answer("A run is in progress — /stop it first.",
                           show_alert=True)
        return
    await session.resume(sid)
    await query.answer("Resumed")
    try:
        await query.edit_message_text(
            query.message.text_html
            + f"\n\n⏪ Resumed <code>{html.escape(sid)}</code> — your next "
            "message continues that conversation.",
            parse_mode="HTML")
    except Exception:
        pass


async def post_init(app: Application):
    io.app = app
    await app.bot.set_my_commands([
        BotCommand("new", "start a fresh session"),
        BotCommand("resume", "list & resume earlier sessions"),
        BotCommand("stop", "interrupt the current run"),
        BotCommand("status", "session info and cost"),
        BotCommand("cost", "billing mode and usage totals"),
        BotCommand("model", "switch model"),
        BotCommand("mode", "tool permissions: ask / auto"),
        BotCommand("voice", "voice replies: off / auto / always"),
        BotCommand("cwd", "change working directory"),
        BotCommand("restartbot", "restart the bot process"),
        BotCommand("compact", "compact the conversation (Claude)"),
        BotCommand("help", "show help"),
    ])
    log.info("Bot ready.")


def main():
    if not config.BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Create a bot with @BotFather, then put the token in "
            f"{config.PROJECT_DIR}/.env")
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)

    app.add_handler(CallbackQueryHandler(on_perm_button, pattern=r"^p\|"))
    app.add_handler(CallbackQueryHandler(on_resume_button, pattern=r"^r\|"))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("restartbot", cmd_restartbot))
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
