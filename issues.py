"""Delivery-health log: everything that may have kept a reply from reaching
the user, written to data/issues.log (one JSON object per line) and scanned
periodically so the owners hear about it instead of guessing.

Kinds:
  error            an ERROR log record (crashed receiver, failed send, ...)
  silent           a turn the user started ended with nothing visible after
                   its last step; the bot asked Claude to reply in text
  silent_unresolved  the same, still nothing visible after that nudge
  question_bare    question buttons turned back for lack of visible text
"""
import asyncio
import json
import logging
import time
from collections import Counter, defaultdict

import config

log = logging.getLogger(__name__)

ISSUES_FILE = config.DATA_DIR / "issues.log"
OFFSET_FILE = config.DATA_DIR / "issues.offset"
SCAN_INTERVAL_S = 30 * 60

# Corrected on the spot, so only counted in the report, not listed.
AUTO_FIXED = {"silent", "question_bare"}


def record(kind: str, chat_id: int | None = None, detail: str = "") -> None:
    entry = {"ts": time.time(), "kind": kind, "chat": chat_id,
             "detail": detail[:500]}
    try:
        with open(ISSUES_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never let the health log break the bot


class ErrorFileHandler(logging.Handler):
    """Copies ERROR records (with the exception's last line) into the log."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            detail = f"{rec.name}: {rec.getMessage()}"
            if rec.exc_info and rec.exc_info[1] is not None:
                e = rec.exc_info[1]
                detail += f" ({type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''})"
            record("error", None, detail)
        except Exception:
            pass


def _read_new() -> list[dict]:
    if not ISSUES_FILE.exists():
        return []
    try:
        offset = int(OFFSET_FILE.read_text())
    except (OSError, ValueError):
        offset = 0
    size = ISSUES_FILE.stat().st_size
    if offset > size:  # file was rotated or truncated
        offset = 0
    with open(ISSUES_FILE) as f:
        f.seek(offset)
        data = f.read()
        OFFSET_FILE.write_text(str(f.tell()))
    out = []
    for line in data.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def report(entries: list[dict], chat_label) -> str | None:
    """The owner message for these entries, None when there's nothing to say."""
    serious = [e for e in entries if e["kind"] not in AUTO_FIXED]
    if not serious:
        return None  # auto-corrected events alone aren't worth a ping
    lines = [f"🩺 Bot health: {len(serious)} problem(s) since the last check"]
    by_kind = defaultdict(list)
    for e in serious:
        by_kind[e["kind"]].append(e)
    for e in by_kind.get("silent_unresolved", []):
        lines.append(f"• Silent reply in {chat_label(e['chat'])} at "
                     f"{time.strftime('%H:%M', time.localtime(e['ts']))}: "
                     "Claude answered nothing visible, even after a nudge.")
    errs = Counter(e["detail"] for e in by_kind.get("error", []))
    for detail, n in errs.most_common(8):
        lines.append(f"• Error{f' ×{n}' if n > 1 else ''}: {detail[:300]}")
    for kind, es in by_kind.items():
        if kind not in ("silent_unresolved", "error"):
            lines.append(f"• {kind} ×{len(es)}")
    fixed = Counter(e["kind"] for e in entries if e["kind"] in AUTO_FIXED)
    if fixed:
        lines.append("Auto-corrected meanwhile: " + ", ".join(
            f"{k} ×{n}" for k, n in fixed.items()))
    lines.append(f"Details: {ISSUES_FILE}")
    return "\n".join(lines)


async def scan_loop(bot, chat_label) -> None:
    """Every SCAN_INTERVAL_S (and once at startup, which catches errors from
    before a crash) DM the owners what went wrong, if anything."""
    while True:
        try:
            text = report(_read_new(), chat_label)
            if text:
                for owner in config.OWNER_IDS:
                    try:
                        await bot.send_message(owner, text)
                    except Exception as e:
                        # WARNING, not ERROR: an ERROR would feed this loop.
                        log.warning("health report to %s failed: %s", owner, e)
        except Exception as e:
            log.warning("health scan failed: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_S)
