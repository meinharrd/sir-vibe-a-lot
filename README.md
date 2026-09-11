# Sir Vibe-a-lot 🤖🎙

A Telegram bot that gives you the full Claude Code experience from your phone:
text, voice notes, and photos in — text, voice, images, and files out.

Built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
(the same harness as the `claude` TUI), so it uses the host machine's existing
Claude login, settings, skills, and slash commands. Speech-to-text
(faster-whisper) and text-to-speech (Piper) run locally — no extra API keys.

## Features

- **Persistent sessions** per chat, resumed across bot restarts (`/new` to reset)
- **Slash-command pass-through**: `/compact`, `/context`, `/usage`,
  `/code-review`, and your custom skills go straight to Claude Code
- **Tool permissions like the TUI**: inline Allow / Deny / Always-allow
  buttons (`/mode ask`; new chats default to `/mode auto`)
- **Voice notes in** — transcribed locally with faster-whisper
- **Voice replies out** — Piper TTS (`/voice off|auto|always`, off by default)
- **Photos/files in** — sent to Claude as vision input / saved to disk
- **Images/files/voice out** — Claude gets `send_photo`, `send_file`, and
  `send_voice` MCP tools to message you directly
- **Remote login** — `/login` runs Claude's OAuth flow over Telegram: tap the
  link, paste the code back, done (no SSH needed)
- **Self-maintaining** — ask the bot to improve its own code; it edits,
  compile-checks, commits, and schedules its own restart
- **Owner-locked** — only Telegram user IDs in `OWNER_IDS` can use it
- **Billing-aware** — `/cost` shows whether usage is subscription plan usage
  (≈ estimates) or real metered API charges

## Requirements

- Linux with systemd, Python 3.11+, `ffmpeg`
- [Claude Code](https://claude.com/claude-code) installed for the user that
  runs the bot; log in on the host, or later via `/login` in the chat
  (which stores a long-lived token in `data/oauth_token`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```sh
git clone <this-repo> sir-vibe-a-lot && cd sir-vibe-a-lot

# dependencies
sudo apt-get install -y ffmpeg
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# TTS voice (one time, ~60 MB)
./.venv/bin/python -m piper.download_voices en_US-lessac-medium --data-dir data/voices

# configuration
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN
chmod 600 .env
```

Run it once with `./.venv/bin/python bot.py`, message your bot — it replies
with your Telegram user ID. Put that in `OWNER_IDS` in `.env`.

Install as a service (edit the paths/user in the unit file if your clone
lives somewhere other than `/home/ubuntu/sir-vibe-a-lot`):

```sh
sudo cp sir-vibe-a-lot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sir-vibe-a-lot
journalctl -u sir-vibe-a-lot -f   # logs
```

The first voice note you send downloads the Whisper model (~500 MB, one time).

## Bot commands

| Command | What it does |
|---|---|
| `/new` | fresh Claude session |
| `/stop` | interrupt the current run |
| `/status` | session id, cwd, model, cost |
| `/retry` | retry queued messages now while a usage limit is active (e.g. right after topping up) |
| `/cost` | billing mode and usage totals |
| `/model fable\|opus\|sonnet\|haiku\|default\|<model-id>` | switch model; with no argument, shows what each alias resolves to |
| `/account auto\|<name>` | which Claude subscription the chat runs on; with no argument, lists them with their usage and cooldowns |
| `/mode ask\|auto` | tool permission prompts on/off |
| `/voice off\|auto\|always` | voice replies |
| `/cwd <path>` | change Claude's working directory (starts a new session) |
| `/login` | log in to a Claude account via OAuth link + pasted code |
| `/restartbot` | restart the bot process (after code changes) |
| anything else starting with `/` | passed to Claude Code verbatim |

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | from @BotFather (required) |
| `OWNER_IDS` | — | comma-separated Telegram user IDs allowed to use the bot |
| `CLAUDE_CWD` | `$HOME` | Claude's default working directory |
| `WHISPER_MODEL` | `small` | STT model: tiny / base / small / medium |
| `PIPER_VOICE` | `en_US-lessac-medium` | TTS voice |
| `SERVICE_NAME` | `sir-vibe-a-lot` | systemd unit name (for self-restart) |
| `LIMIT_RETRY_INTERVAL_S` | `300` | how often queued messages are retried while a Claude usage limit is active (the limit lifts early when usage is topped up) |
| `ALAN_ACCOUNTS` | `~/alan/accounts.py` | account registry used for routing (see below); routing is off when the file is missing |

## Multiple subscriptions

If [alan](../alan) is installed on the same box, the bot routes its sessions
over every Claude subscription registered there (`secrets/accounts.toml`) and
shares alan's limit state (`state/accounts.json`, flock-protected), so the two
never retry an account the other just found capped. Selection is alan's:
among the accounts with allowance, the one whose weekly window resets soonest.

When a session walks into a usage limit, the chat moves to another
subscription and the prompt is retried there instead of waiting for the reset
(a notice naming a model — "Fable 5 limit" — only parks that model on that
account). `/account <name>` pins a chat to one subscription; a pinned chat
waits out its limits instead of switching. With no registry present nothing
changes: sessions use the host login, or the `/login` token.

Cursor subscriptions (`kind = "cursor"` in alan's registry) are listed but not
usable here — they run only through the cursor-agent CLI, not the Claude Agent
SDK this bot speaks.

## Security notes

- The bot gives Claude tool access to the machine it runs on. Keep `OWNER_IDS`
  tight. New chats start in `/mode auto`, which runs Claude with
  `bypassPermissions`: anyone controlling an owner Telegram account can then
  run arbitrary commands on the host. Switch a chat to `/mode ask` to confirm
  every non-read-only tool call with buttons instead.
- Sessions are scoped to the working directory; state lives in
  `data/state.json`, received media under `data/media/` (both git-ignored,
  as is `.env`).
