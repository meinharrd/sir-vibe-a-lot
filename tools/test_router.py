"""Switching behaviour on a usage limit, against a throwaway state file.

Run from anywhere:  .venv/bin/python tools/test_router.py
Touches no real account state (alan's state file is redirected to a tempdir)
and makes no API calls."""
import asyncio, pathlib, sys, tempfile, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import router, claude_bridge as cb

mod = router._alan()
tmp = pathlib.Path(tempfile.mkdtemp())
mod.STATE_FILE = tmp / "accounts.json"
mod.LOCK_FILE = tmp / "accounts.lock"

class IO(cb.TelegramIO):
    def __init__(self): self.msgs = []
    async def send_text(self, chat_id, text, markdown=True): self.msgs.append(text)
    async def status_update(self, chat_id, text): self.msgs.append(text)
    async def status_done(self, chat_id, text): self.msgs.append(text)

def session(pin=None):
    io = IO()
    s = cb.ChatSession(1, cb.ChatState(account=pin), io, lambda: None, lambda: None)
    s.account = "host"
    s.state.active_model = "claude-opus-5"
    return s, io

async def main():
    soon = time.time() + 3600

    # 1. a limit on host moves the chat to second
    cb.USAGE_LIMIT.until = 0
    s, io = session()
    await s._limit_hit(soon, text="5-hour limit reached · resets 3am")
    assert s._switched_to == "second", s._switched_to
    assert s._needs_reconnect and not cb.USAGE_LIMIT.active
    assert "retrying on second" in io.msgs[0], io.msgs
    print("1 switch:", io.msgs[0])

    # the same limit reported twice in a turn is announced once
    await s._limit_hit(soon, text="5-hour limit reached")
    assert len(io.msgs) == 1, io.msgs

    # 2. with second capped too, every chat waits
    s2, io2 = session()
    s2.account = "second"
    await s2._limit_hit(soon, text="5-hour limit reached")
    assert s2._switched_to is None
    assert cb.USAGE_LIMIT.active
    assert "usage limit reached" in io2.msgs[0], io2.msgs
    print("2 wait:  ", io2.msgs[0][:80])

    # 3. a pinned chat never switches
    cb.USAGE_LIMIT.until = 0
    mod.STATE_FILE.unlink()
    s3, io3 = session(pin="host")
    await s3._limit_hit(soon, text="5-hour limit reached")
    assert s3._switched_to is None and cb.USAGE_LIMIT.active
    print("3 pinned:", io3.msgs[0][:60])

    # 4. a model cap parks that family only, so the account still serves others
    cb.USAGE_LIMIT.until = 0
    mod.STATE_FILE.unlink()
    s4, io4 = session()
    s4.state.active_model = "claude-fable-5"
    await s4._limit_hit(soon, text="You've reached your Fable 5 limit")
    assert s4._switched_to == "second"
    assert mod.model_cooldown("host", "claude-fable-5") > time.time()
    assert mod.cooldown("host")[0] is None, "account parked by a model cap"
    assert mod.has_allowance("host", "claude-opus-5")
    print("4 model cap: fable parked on host, opus still allowed")

asyncio.run(main())
print("ALL OK")
