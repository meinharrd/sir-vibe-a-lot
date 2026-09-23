"""Telegram sends survive a transient connection failure.

Run from anywhere:  .venv/bin/python tools/test_tg_retry.py
Talks to no network: a fake bot raises the errors PTB would raise.

Why this exists: on 2026-09-23 a single httpx.ConnectError to
api.telegram.org killed a chat's receiver task, so the turn surfaced as
"⚠️ Claude session error" and Claude's answer was lost even though the
model had finished. Retrying the send keeps one blip from costing a turn.
"""
import asyncio, pathlib, sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

import bot as botmod


def make_io(fail_times: int, exc):
    """A TgIO whose send_message/send_photo fail `fail_times` times first."""
    calls = {"n": 0}

    async def send_message(chat_id, text, **kw):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return SimpleNamespace(message_id=calls["n"])

    async def send_photo(chat_id, f, caption=None):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return SimpleNamespace(message_id=calls["n"], read=f.read())

    io = botmod.TgIO()
    io.app = SimpleNamespace(bot=SimpleNamespace(
        send_message=send_message, send_photo=send_photo,
        edit_message_text=send_message))
    return io, calls


def run(coro):
    """Run with sleeps stubbed out, so backoff costs no wall-clock."""
    real_sleep = asyncio.sleep
    slept = []

    async def fake_sleep(d, *a, **kw):
        slept.append(d)
        return await real_sleep(0)

    asyncio.sleep = fake_sleep
    try:
        return asyncio.run(coro()), slept
    finally:
        asyncio.sleep = real_sleep


# 1. a ConnectError-shaped NetworkError is retried, and the send goes through
io, calls = make_io(2, NetworkError("httpx.ConnectError: "))
msg, slept = run(lambda: io._send_retrying(-1, "hello"))
assert calls["n"] == 3, calls
assert msg.message_id == 3
assert len(slept) == 2 and all(0 < d <= 8.5 for d in slept), slept
print(f"1 text send: 2 ConnectErrors survived, sent on attempt 3 "
      f"(backoff {', '.join(f'{d:.1f}s' for d in slept)})")

# 2. TimedOut is a NetworkError subclass and takes the same path
io, calls = make_io(1, TimedOut())
msg, _ = run(lambda: io._send_retrying(-1, "hello"))
assert calls["n"] == 2, calls
print("2 timeout: retried like any connection failure")

# 3. BadRequest must NOT be retried: it subclasses NetworkError in PTB, but
#    send_text() depends on it propagating to fall back to unformatted text.
io, calls = make_io(1, BadRequest("can't parse entities"))
try:
    run(lambda: io._send_retrying(-1, "<b>bad"))
    raise SystemExit("FAIL: BadRequest was swallowed; formatting fallback dies")
except BadRequest:
    pass
assert calls["n"] == 1, calls
print("3 formatting error: raised at once, not retried")

# 4. a wedged connection still surfaces, rather than hanging the turn forever
io, calls = make_io(99, NetworkError("httpx.ConnectError: "))
try:
    run(lambda: io._send_retrying(-1, "hello"))
    raise SystemExit("FAIL: exhausted retries should re-raise")
except NetworkError:
    pass
assert calls["n"] == botmod.TgIO.NET_RETRIES + 1, calls
print(f"4 persistent outage: gave up after {calls['n']} attempts and re-raised")

# 5. flood control keeps its own (server-dictated) delay
io, calls = make_io(1, RetryAfter(3))
msg, slept = run(lambda: io._send_retrying(-1, "hello"))
assert calls["n"] >= 2 and slept and slept[0] >= 3, (calls, slept)
print(f"5 flood control: waited the {slept[0]:.1f}s Telegram asked for")

# 6. uploads retry too, re-opening the file each attempt
io, calls = make_io(2, NetworkError("httpx.ConnectError: "))
tmp = pathlib.Path("/tmp/_tg_retry_probe.bin")
tmp.write_bytes(b"payload")
try:
    res, _ = run(lambda: io._upload_retrying(-1, "photo", io.app.bot.send_photo,
                                             str(tmp), "cap"))
finally:
    tmp.unlink(missing_ok=True)
assert calls["n"] == 3 and res.read == b"payload", (calls, res)
print("6 upload: file re-opened per attempt, full payload sent on attempt 3")

print("\nall tg retry tests passed")
