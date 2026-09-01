"""Remote Claude login: drives `claude setup-token` over a pty.

The TUI-only OAuth flow can't run through the Agent SDK, but `claude
setup-token` needs nothing more than a terminal: it prints a sign-in URL,
waits for the pasted authorization code, and emits a long-lived OAuth token.
This module runs it in a pseudo-terminal so the URL and code can travel over
Telegram instead.
"""
import asyncio
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
from pathlib import Path

URL_RE = re.compile(r"https://claude\.com/[^\s\x07\x1b\"]+")
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
FAIL_RE = re.compile(r"invalid|expired|failed|error", re.IGNORECASE)

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (hyperlinks, window title)
    r"|\x1b\[[0-9;<>=?]*[A-Za-z]"         # CSI (colors, cursor moves)
    r"|\x1b."                             # any other escape
)


def _clean(raw: str) -> str:
    """Human-readable text from raw pty output (words are positioned with
    cursor-move sequences, so escapes become spaces, not nothing)."""
    text = _ANSI_RE.sub(" ", raw)
    lines = (" ".join(l.split()) for l in text.replace("\r", "\n").split("\n"))
    return "\n".join(l for l in lines if l).strip()


class LoginError(Exception):
    pass


def _find_claude() -> str:
    """Locate the claude CLI. Under systemd the service PATH is minimal,
    so check the same install locations the Agent SDK falls back to."""
    if cli := shutil.which("claude"):
        return cli
    for path in (
        Path.home() / ".local/bin/claude",
        Path.home() / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        Path.home() / "node_modules/.bin/claude",
        Path.home() / ".yarn/bin/claude",
        Path.home() / ".claude/local/claude",
    ):
        if path.is_file():
            return str(path)
    raise LoginError("claude CLI not found on this machine")


class LoginFlow:
    """One interactive `claude setup-token` run.

    start() returns the sign-in URL; submit_code() feeds the pasted code
    back and returns the long-lived token; cancel() kills the process.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._fd: int | None = None
        self._buf = ""

    async def start(self, timeout: float = 30.0) -> str:
        cli = _find_claude()
        master, slave = pty.openpty()
        # Wide terminal: at the default 80 columns the CLI hard-wraps long
        # lines, splitting the OAuth token across lines so a regex over the
        # output would capture only its first fragment.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 500, 0, 0))
        try:
            self._proc = subprocess.Popen(
                [cli, "setup-token"],
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(os.environ, TERM="xterm-256color"),
                start_new_session=True)
        finally:
            os.close(slave)
        self._fd = master
        try:
            await self._read_until(
                lambda: URL_RE.search(self._buf) and "Paste" in self._buf,
                timeout)
        except Exception:
            await self.cancel()
            raise
        return URL_RE.search(self._buf).group(0)

    async def submit_code(self, code: str, timeout: float = 120.0) -> str:
        if self._fd is None:
            raise LoginError("The login flow is no longer running.")
        mark = len(self._buf)
        os.write(self._fd, code.strip().encode())
        # The CLI groups rapid input as a bracketed paste: a "\r" in the same
        # chunk is swallowed into the pasted text instead of submitting the
        # form. Pause past the paste-detection window, then press Enter.
        await asyncio.sleep(0.5)
        os.write(self._fd, b"\r")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                chunk = await self._read_chunk(1.0)
                if chunk:
                    self._buf += chunk.decode(errors="replace")
                    continue
                # Quiet for a second, or EOF: any token is now complete.
                # (Matching on every chunk could return a half-received one.)
                m = TOKEN_RE.search(self._buf, mark)
                if m:
                    return m.group(0)
                # clean before dropping the echoed code, so no escape
                # sequence is sliced in half; the CLI masks the paste with
                # asterisks, so drop those runs too
                new = _clean(self._buf[mark:]).replace(code.strip(), "")
                new = re.sub(r"\*{4,}\S*", "", new)
                if chunk == b"" or FAIL_RE.search(new):
                    raise LoginError(
                        "Login failed:\n" + (new.strip()[-500:] or "(no output)"))
                if loop.time() >= deadline:
                    raise LoginError("Timed out waiting for the code to be accepted.")
        finally:
            await self.cancel()

    async def cancel(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()

    async def _read_until(self, cond, timeout: float):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not cond():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LoginError(
                    "Timed out waiting for `claude setup-token`:\n"
                    + _clean(self._buf)[-500:])
            chunk = await self._read_chunk(remaining)
            if chunk is None:
                continue
            if chunk == b"":
                raise LoginError(
                    "`claude setup-token` exited early:\n"
                    + (_clean(self._buf)[-500:] or "(no output)"))
            self._buf += chunk.decode(errors="replace")

    async def _read_chunk(self, timeout: float) -> bytes | None:
        """One pty read: bytes, b"" on EOF, None on timeout."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fd = self._fd
        if fd is None:
            return b""

        def on_readable():
            loop.remove_reader(fd)
            if fut.done():
                return
            try:
                fut.set_result(os.read(fd, 4096))
            except OSError:  # EIO — slave side closed
                fut.set_result(b"")

        loop.add_reader(fd, on_readable)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            loop.remove_reader(fd)
