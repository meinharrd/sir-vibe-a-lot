"""Configuration for the Telegram <-> Claude Code bridge."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Comma-separated Telegram user IDs allowed to talk to the bot.
OWNER_IDS = {
    int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x
}

# Directory Claude works in by default (change per-chat with /cwd).
DEFAULT_CWD = os.environ.get("CLAUDE_CWD", str(Path.home()))

# Where per-chat state, downloaded media, and TTS voices live.
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_DIR / "data"))
MEDIA_DIR = DATA_DIR / "media"
STATE_FILE = DATA_DIR / "state.json"

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
VOICES_DIR = DATA_DIR / "voices"

# Tools that never need confirmation in "ask" mode (read-only / harmless).
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite",
    "Task", "NotebookRead", "TodoRead", "ListMcpResources", "ReadMcpResource",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    "mcp__telegram__send_photo", "mcp__telegram__send_file",
    "mcp__telegram__send_voice",
}

PERMISSION_TIMEOUT_S = 600  # deny a tool call if not answered in 10 min

for d in (DATA_DIR, MEDIA_DIR, VOICES_DIR):
    d.mkdir(parents=True, exist_ok=True)
