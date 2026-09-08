#!/usr/bin/env python3
"""Pre-restart check for the bot: which chats are busy or were recently
active, and which of those look like they are waiting on background work
(monitors, wake-ups, background tasks) that a restart would kill.

    .venv/bin/python tools/restart_check.py            # report
    .venv/bin/python tools/restart_check.py --continue CHAT_ID [CHAT_ID ...]
        # after the restart, the bot sends those chats a "continue" prompt

The bot re-submits a prompt that was mid-turn by itself; a session that had
already answered and was waiting on a monitor gets nothing unless it is
listed with --continue.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from claude_bridge import _project_dir  # noqa: E402

BACKGROUND_TOOLS = {"Monitor", "ScheduleWakeup", "CronCreate", "Workflow"}
RECENT_S = 90 * 60


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def inspect(path: Path) -> dict:
    """Last real prompt, last assistant text, and background-work signs
    after that prompt."""
    last_prompt = last_text = None
    bg: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = rec.get("type")
        msg = rec.get("message") or {}
        if t == "user":
            txt = " ".join(_text(msg.get("content")).split())
            if txt.startswith("<task-notification"):
                bg.add("task-notification")
                continue
            if txt and not txt.startswith("<"):
                last_prompt, last_text, bg = txt[:100], None, set()
        elif t == "assistant":
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b["text"].strip():
                    last_text = " ".join(b["text"].split())[:140]
                elif b.get("type") == "tool_use":
                    name, inp = b.get("name"), b.get("input") or {}
                    if name in BACKGROUND_TOOLS:
                        bg.add(name)
                    elif name in ("Bash", "Agent") and inp.get("run_in_background"):
                        bg.add(f"{name}(background)")
    return {"last_prompt": last_prompt, "last_text": last_text, "background": sorted(bg)}


def report() -> int:
    now = time.time()
    try:
        pending = json.loads(config.PENDING_FILE.read_text())
    except (OSError, ValueError):
        pending = {}
    try:
        state = json.loads(config.STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    busy = {cid: items for cid, items in pending.items() if items}
    print("BUSY (prompt in flight or queued — the bot re-submits these itself):")
    for cid, items in busy.items() or {}.items():
        p = items[0].get("prompt")
        p = p if isinstance(p, str) else "(image/voice prompt)"
        print(f"  chat {cid}: {len(items)} item(s) — {p[:100]!r}")
    if not busy:
        print("  none")
    print("\nRECENTLY ACTIVE (last 90 min) — needs a 'continue' if waiting on background work:")
    rows = []
    for cid, s in state.items():
        sid, cwd = s.get("session_id"), s.get("cwd") or ""
        if not sid:
            continue
        p = _project_dir(cwd) / f"{sid}.jsonl"
        if not p.exists():
            continue
        age = now - p.stat().st_mtime
        if age > RECENT_S:
            continue
        info = inspect(p)
        rows.append((age, cid, cwd, info))
    for age, cid, cwd, info in sorted(rows):
        flag = ("⚠️ waiting on " + ", ".join(info["background"])) if info.get("background") else "idle"
        print(f"  chat {cid} [{cwd}] last write {int(age // 60)}m ago — {flag}")
        if info.get("last_prompt"):
            print(f"      prompt: {info['last_prompt']!r}")
        if info.get("last_text"):
            print(f"      last reply: {info['last_text']!r}")
    if not rows:
        print("  none")
    need = [cid for _, cid, _, info in rows if info.get("background") and cid not in busy]
    print()
    if need:
        print("Suggested: .venv/bin/python tools/restart_check.py --continue " + " ".join(need))
    else:
        print("No session needs a continue prompt.")
    return 0


def mark_continue(chat_ids: list[str]) -> None:
    f = config.DATA_DIR / "continue_after_restart.json"
    f.write_text(json.dumps([int(c) for c in chat_ids]))
    print(f"after the next restart the bot will send a continue prompt to: {', '.join(chat_ids)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--continue", dest="cont", nargs="*", metavar="CHAT_ID",
                    help="chats that should get a 'continue' prompt after the restart")
    a = ap.parse_args()
    if a.cont:
        mark_continue(a.cont)
    else:
        sys.exit(report())
