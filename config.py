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

# systemd unit name, used for self-restart (/restartbot and self-maintenance)
SERVICE_NAME = os.environ.get("SERVICE_NAME", "sir-vibe-a-lot")

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

# Long-lived OAuth token captured by the /login flow (claude setup-token).
# When present it is passed to every Claude session via CLAUDE_CODE_OAUTH_TOKEN,
# taking precedence over the host's ~/.claude login.
OAUTH_TOKEN_FILE = DATA_DIR / "oauth_token"


def load_oauth_token() -> str | None:
    try:
        return OAUTH_TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def save_oauth_token(token: str) -> None:
    fd = os.open(OAUTH_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    OAUTH_TOKEN_FILE.chmod(0o600)


def _detect_billing() -> tuple[str, str | None]:
    """How Claude usage is paid for.

    Returns ("api", None) when a metered API key is set (costs are real
    charges), ("subscription", "<plan>") for a claude.ai OAuth login (costs
    are API-equivalent estimates covered by the plan), else ("unknown", None).
    """
    import json
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api", None
    try:
        creds = json.loads(
            (Path.home() / ".claude" / ".credentials.json").read_text())
        plan = creds.get("claudeAiOauth", {}).get("subscriptionType")
        if plan:
            return "subscription", plan
    except Exception:
        pass
    if load_oauth_token():  # /login token (setup-token requires a subscription)
        return "subscription", None
    return "unknown", None


BILLING_MODE, SUBSCRIPTION_PLAN = _detect_billing()

for d in (DATA_DIR, MEDIA_DIR, VOICES_DIR):
    d.mkdir(parents=True, exist_ok=True)
