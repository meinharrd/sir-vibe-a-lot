"""Offline checks for webhook mode. (1) poller_watch puts back a webhook that
someone else deleted or replaced, without logging our secret URL. (2) The real
python-telegram-bot webhook server on 127.0.0.1 accepts an update with the
right secret on the right path and rejects a wrong secret or path.
Run: .venv/bin/python tools/test_webhook.py"""
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

config.OWNER_IDS = {1}
config.BOT_TOKEN = "123456:secret"
config.WEBHOOK_URL = "https://example.test/tg/s3cretpath"
config.WEBHOOK_SECRET = "hook_secret-1"
import poller_watch  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.ext import Application, ExtBot  # noqa: E402


class Bot:
    def __init__(self, url):
        self.url, self.sent, self.set_calls = url, [], []

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.url, last_error_date=None, last_error_message=None)

    async def set_webhook(self, url, **kw):
        self.set_calls.append((url, kw))
        self.url = url

    async def send_message(self, chat, text):
        self.sent.append(text)


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


async def guard():
    rec = Records()
    lg = logging.getLogger("poller_watch")
    lg.addHandler(rec)
    lg.setLevel(logging.DEBUG)
    w = poller_watch.PollerWatch()

    ok = Bot(config.WEBHOOK_URL)
    await w.check_webhook(ok)
    assert not ok.set_calls and not ok.sent

    for intruder in ("", "https://evil.example/hook"):
        b = Bot(intruder)
        w._hook_dm_at = 0
        await w.check_webhook(b)
        assert b.url == config.WEBHOOK_URL, b.url
        assert b.set_calls[0][1]["secret_token"] == config.WEBHOOK_SECRET
        assert len(b.sent) == 1 and "revoke it" in b.sent[0], b.sent
        await w.check_webhook(Bot(intruder))  # DM rate limit: no second DM
    assert "evil.example" in " ".join(rec.msgs)
    assert not any("s3cretpath" in m for m in rec.msgs + ok.sent), "secret path leaked"


class FakeBot(ExtBot):
    async def get_me(self, *a, **kw):
        return None

    async def initialize(self):
        pass

    async def set_webhook(self, *a, **kw):
        return True

    async def delete_webhook(self, *a, **kw):
        return True


def post(port, path, secret):
    body = json.dumps({"update_id": 7, "message": {
        "message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "o"}, "text": "hi"}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", body,
                                 {"Content-Type": "application/json",
                                  "X-Telegram-Bot-Api-Secret-Token": secret})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


async def server():
    app = Application.builder().bot(FakeBot(config.BOT_TOKEN)).updater(None).build()
    from telegram.ext import Updater
    q = asyncio.Queue()
    up = Updater(app.bot, q)
    await up.initialize()
    port = 18350
    await up.start_webhook(listen="127.0.0.1", port=port, url_path="tg/s3cretpath",
                           webhook_url=config.WEBHOOK_URL, secret_token=config.WEBHOOK_SECRET,
                           allowed_updates=Update.ALL_TYPES)
    loop = asyncio.get_running_loop()
    good = await loop.run_in_executor(None, post, port, "tg/s3cretpath", config.WEBHOOK_SECRET)
    bad_secret = await loop.run_in_executor(None, post, port, "tg/s3cretpath", "wrong")
    bad_path = await loop.run_in_executor(None, post, port, "tg/other", config.WEBHOOK_SECRET)
    await up.stop()
    await up.shutdown()
    assert good == 200, good
    assert bad_secret == 403, bad_secret
    assert bad_path == 404, bad_path
    assert q.qsize() == 1, q.qsize()
    u = q.get_nowait()
    assert u.message.text == "hi"


async def main():
    await guard()
    await server()
    print("ok")


logging.basicConfig(level=logging.CRITICAL + 1)
asyncio.run(main())
