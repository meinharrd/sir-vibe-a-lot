"""Second-poller and revoked-token detection.

Telegram never tells one getUpdates poller where another poller of the same
token runs: a Conflict error is the only signal. So instead of a traceback on
every failed poll, log one line and DM the owners when a conflict starts and
when it ends. The one case where Telegram does reveal the other side is a
webhook (getWebhookInfo returns its URL and IP), so that is looked up and
reported too.

A rejected token (revoked in BotFather) makes python-telegram-bot abort
polling for good while the process keeps running. Nothing can be sent with a
rejected token, so that gets one clear CRITICAL log line instead of a DM.
"""
import asyncio
import logging
import time

from telegram.error import Conflict, InvalidToken

import config

log = logging.getLogger("poller_watch")

QUIET_S = 120        # no Conflict for this long: the other poller is gone
CHECK_INTERVAL_S = 30


def _bot_id() -> str:
    return (config.BOT_TOKEN or "").split(":", 1)[0] or "?"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


async def _dm_owners(bot, text: str) -> None:
    for owner in config.OWNER_IDS:
        try:
            await bot.send_message(owner, text)
        except Exception as e:
            # WARNING, not ERROR: an ERROR would land in the health report.
            log.warning("poller alert to %s failed: %s", owner, e)


async def webhook_summary(bot) -> str:
    """What getWebhookInfo says: the only way Telegram shows where another
    user of this token receives updates."""
    try:
        info = await bot.get_webhook_info()
    except Exception as e:
        return f"getWebhookInfo failed: {e}"
    if not info.url:
        return ("No webhook is set, so the other side polls with getUpdates. "
                "Telegram doesn't reveal where it runs.")
    parts = [f"Webhook set by the other side: {info.url}"]
    if info.ip_address:
        parts.append(f"IP {info.ip_address}")
    if info.pending_update_count:
        parts.append(f"{info.pending_update_count} updates pending")
    if info.last_error_message:
        parts.append(f"last delivery error: {info.last_error_message}")
    return ", ".join(parts)


class PollerWatch:
    def __init__(self):
        self.since: float | None = None
        self.last = 0.0
        self.count = 0

    def is_conflict(self, err) -> bool:
        return isinstance(err, Conflict)

    def hit(self, bot, err) -> None:
        """Called from the error handler for every Conflict."""
        now = time.time()
        self.last = now
        self.count += 1
        if self.since is not None:
            return
        self.since = now
        # One ERROR per conflict, so the health report lists it once.
        log.error("Second poller on bot %s since %s: %s",
                  _bot_id(), _clock(now), err)
        asyncio.get_running_loop().create_task(self._started(bot, str(err)))

    async def _started(self, bot, err: str) -> None:
        hook = await webhook_summary(bot)
        log.warning("Second poller on bot %s: %s", _bot_id(), hook)
        await _dm_owners(bot, (
            f"Second poller detected on this bot (id {_bot_id()}) since "
            f"{_clock(self.since or time.time())}. Something else is using this "
            f"bot's token, and Telegram is splitting updates between it and "
            f"this bot, so some of your messages may not arrive here.\n\n"
            f"Telegram says: {err}\n{hook}\n\n"
            f"If it isn't another copy of this bot you started, revoke the "
            f"token in @BotFather."))

    async def _ended(self, bot) -> None:
        since, count = self.since or self.last, self.count
        self.since, self.count = None, 0
        mins = max(1, round((self.last - since) / 60))
        log.warning("Second poller on bot %s gone: %s to %s, %d conflicts",
                    _bot_id(), _clock(since), _clock(self.last), count)
        await _dm_owners(bot, (
            f"The second poller on this bot is gone: it ran from "
            f"{_clock(since)} to {_clock(self.last)} (about {mins} min, "
            f"{count} conflicts). This bot receives all updates again."))

    async def loop(self, bot) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                if self.since is not None and time.time() - self.last > QUIET_S:
                    await self._ended(bot)
            except Exception as e:
                log.warning("poller watch failed: %s", e)


class TokenRejectedHandler(logging.Handler):
    """Attached to the telegram.ext logger: python-telegram-bot logs the
    InvalidToken that aborts polling there and doesn't pass it to the error
    handler."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.fired = False

    def emit(self, rec: logging.LogRecord) -> None:
        exc = rec.exc_info[1] if rec.exc_info else None
        if self.fired or not isinstance(exc, InvalidToken):
            return
        self.fired = True
        log.critical(
            "Telegram rejected the token of bot %s (revoked in @BotFather?). "
            "Polling has stopped and nothing can be sent. Put the new token "
            "in TELEGRAM_BOT_TOKEN in %s/.env and restart the service.",
            _bot_id(), config.PROJECT_DIR)


watch = PollerWatch()
logging.getLogger("telegram.ext").addHandler(TokenRejectedHandler())
