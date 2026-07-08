# Telegram ⇄ Claude Code bridge

A Telegram bot that gives you the Claude Code experience from your phone:
text, voice notes, and photos in — text, voice, images, and files out.
Built on the Claude Agent SDK (the same harness as the `claude` TUI), so it
uses this server's existing Claude login, settings, and skills.

## Features

- **Persistent sessions** per chat, resumed across bot restarts (`/new` to reset)
- **Slash-command pass-through**: `/compact`, `/context`, `/usage`,
  `/code-review`, and your custom skills go straight to Claude Code
- **Tool permissions like the TUI**: inline Allow / Deny / Always-allow
  buttons (`/mode auto` to auto-approve everything)
- **Voice notes in**: transcribed locally with faster-whisper
- **Voice replies out**: Piper TTS (`/voice off|auto|always`)
- **Photos/files in**: sent to Claude as vision input / saved to disk
- **Images/files/voice out**: Claude has `send_photo`, `send_file`, and
  `send_voice` tools to message you directly
- **Owner-locked**: only Telegram user IDs in `OWNER_IDS` can use it

## Setup

1. Create a bot: message **@BotFather** on Telegram → `/newbot` → copy the token.
2. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN`.
3. Run it once: `./.venv/bin/python bot.py`, message the bot — it replies with
   your user ID. Put that in `OWNER_IDS` in `.env` and restart.
4. Install as a service:
   ```sh
   sudo cp telegram-claude-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-claude-bot
   ```
   Logs: `journalctl -u telegram-claude-bot -f`

## Bot commands

| Command | What it does |
|---|---|
| `/new` | fresh Claude session |
| `/stop` | interrupt the current run |
| `/status` | session id, cwd, model, cost |
| `/model opus\|sonnet\|haiku\|default` | switch model |
| `/mode ask\|auto` | tool permission prompts on/off |
| `/voice off\|auto\|always` | voice replies |
| `/cwd <path>` | change Claude's working directory (starts a new session) |
| anything else starting with `/` | passed to Claude Code verbatim |

## Notes

- Sessions are scoped to the working directory (`/cwd` starts a new one).
- State lives in `data/state.json`; media you send is saved under `data/media/`.
- First voice note downloads the Whisper model (~500 MB, one time).
- `/mode auto` runs Claude with `bypassPermissions` — anyone with your
  Telegram account can then run arbitrary commands on this server. Keep
  `ask` mode unless you trust the task.
