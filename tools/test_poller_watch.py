"""Offline check of poller_watch: a Conflict burst gives one ERROR and one DM
when it starts and one DM when it ends; a webhook is reported; a rejected
token gives one CRITICAL line. Run: .venv/bin/python tools/test_poller_watch.py"""
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

config.OWNER_IDS = {1}
config.BOT_TOKEN = "123456:secret"
import poller_watch  # noqa: E402
from telegram.error import Conflict, InvalidToken  # noqa: E402


class Bot:
    def __init__(self, hook_url=""):
        self.sent = []
        self.hook_url = hook_url

    async def send_message(self, chat, text):
        self.sent.append((chat, text))

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.hook_url, ip_address="203.0.113.7" if self.hook_url else None,
                               pending_update_count=0, last_error_message=None)


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.recs = []

    def emit(self, rec):
        self.recs.append(rec)


async def main():
    rec = Records()
    logging.getLogger("poller_watch").addHandler(rec)
    logging.getLogger("poller_watch").setLevel(logging.DEBUG)
    logging.getLogger("telegram.ext").setLevel(logging.DEBUG)
    poller_watch.QUIET_S = 0.2
    poller_watch.CHECK_INTERVAL_S = 0.05

    w = poller_watch.PollerWatch()
    bot = Bot()
    err = Conflict("terminated by other getUpdates request")
    assert w.is_conflict(err) and not w.is_conflict(InvalidToken())
    for _ in range(5):
        w.hit(bot, err)
    await asyncio.sleep(0.05)
    errors = [r for r in rec.recs if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert len(bot.sent) == 1 and "Second poller detected" in bot.sent[0][1], bot.sent
    assert "doesn't reveal" in bot.sent[0][1]
    task = asyncio.create_task(w.loop(bot))
    await asyncio.sleep(0.4)
    task.cancel()
    assert len(bot.sent) == 2 and "5 conflicts" in bot.sent[1][1], bot.sent
    assert w.since is None

    hook_bot = Bot("https://evil.example/hook")
    s = await poller_watch.webhook_summary(hook_bot)
    assert "https://evil.example/hook" in s and "203.0.113.7" in s, s

    rec.recs.clear()
    try:
        raise InvalidToken("Unauthorized")
    except InvalidToken:
        for _ in range(3):
            logging.getLogger("telegram.ext").exception("Invalid token. Aborting retry loop.")
    crit = [r for r in rec.recs if r.levelno == logging.CRITICAL]
    assert len(crit) == 1 and "123456" in crit[0].getMessage(), crit
    print("ok")


logging.basicConfig(level=logging.CRITICAL + 1)
asyncio.run(main())
