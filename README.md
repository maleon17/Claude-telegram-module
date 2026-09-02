# claude-telegram-bridge

> Part of **[telegram-ai](https://github.com/maleon17/telegram-ai)** — Claude/Codex ↔ Telegram, four ways.

A Telegram bridge for [Claude Code](https://claude.com/claude-code) — message Claude from Telegram using your own Claude subscription (Pro/Max/Team), not a metered API key.

Drives `claude -p --output-format=stream-json` per message and streams progress back live, with real session management, multi-tenancy (each whitelisted person gets their own isolated Claude account), and support for photos/files in both directions.

## Features

- **Live progress streaming** — a "thinking" preview animates in Telegram while Claude works (via Bot API's `sendMessageDraft`), showing the current tool call and its result as separate code blocks, before the final answer and full process log land as normal messages.
- **Sessions** — `/new`, `/sessions`, `/resume <id>` map onto Claude Code's own `--resume`/session mechanism, so conversations persist across bot restarts.
- **Multi-tenant** — a simple `whitelist.txt` gates access. Anyone besides the owner who's whitelisted goes through their own `claude auth login` (a button + pasted code, right in the chat) and gets a fully isolated Claude account (own subscription, own sessions, own usage) — no shared billing.
- **Model / permission control** — `/model` to switch between Opus/Sonnet/Haiku/Fable and specific versions, `/mode` to switch between auto-approve and a real approve/deny gate for tool calls.
- **Files & photos** — incoming photos/documents are downloaded and handed to Claude to read natively; outgoing files Claude creates (or mentions by path) get sent back as Telegram attachments automatically.
- **Rich messages** — incoming Telegram "rich messages" (tables, collapsible sections, etc.) are converted to Markdown so they don't get silently dropped; outgoing collapsible process logs use the same rich-message format.
- **Forwarded-message batching** — forwarding a batch of messages at once combines them into a single prompt instead of processing only the first and bouncing the rest.

## Requirements

- Linux host with `python3` (stdlib only — no pip dependencies)
- [Claude Code CLI](https://claude.com/claude-code) installed and logged in with a Claude subscription (Pro, Max, or Team — the bridge shells out to your own logged-in `claude` binary, it does not use an API key)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- `systemd` (for running it as a persistent service) — not strictly required, but the setup script assumes it

## Quick start

```bash
# 1. Log in to Claude Code first (interactive browser OAuth, do this yourself)
claude auth login --claudeai

# 2. Clone this repo
git clone https://github.com/<your-username>/Claude-telegram-bot.git
cd claude-telegram-bridge

# 3. Run the installer -- it'll ask for your bot token and Telegram ID
./setup.sh
```

That's it — message your bot on Telegram to start.

## Manual install (alternative to `setup.sh`)

If you'd rather not run a script with `sudo`, do it by hand:

1. Copy `claude-telegram-bridge.service.example` to e.g. `/etc/systemd/system/claude-telegram-bridge.service`.
2. Replace the placeholders inside it:
   - `__USER__` — the Linux user to run as
   - `__INSTALL_DIR__` — absolute path to this repo's directory
   - `__BOT_TOKEN__` — your bot token from @BotFather
   - `__OWNER_ID__` — your numeric Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))
3. `sudo systemctl daemon-reload && sudo systemctl enable --now claude-telegram-bridge.service`

## Commands

| Command | What it does |
|---|---|
| `/new` | Start a new session |
| `/sessions` | List recent sessions |
| `/resume <id>` | Resume a session by ID (or prefix) |
| `/status` | Current session/model/mode/workspace/busy state |
| `/stop` | Interrupt the request currently running |
| `/usage` | Tokens, cost, and account rate limits |
| `/model <family> [version]` | Switch model, e.g. `/model opus 4.7`, `/model sonnet`, `/model default` |
| `/mode <mode>` | Permission mode: `bypass` / `default` / `acceptEdits` / `plan` |
| `/workspace <path>` | Change the working directory for this chat |
| `/approve` / `/approve session` | Allow a blocked tool call once, or bypass for the rest of the session |
| `/deny` | Reject a blocked tool call |
| `/login` | Start/re-authenticate the Claude account for this chat (including the owner account) |
| `/restart` | Owner-only: safe deferred restart (waits for in-flight turns to finish) |
| `/update` | Owner-only: `git pull` the latest push, then restart the same way `/restart` does |

## Updating

Either run `./update.sh` on the host, or send `/update` from the owner chat — both do the same thing (`git fetch` + fast-forward-only merge, then a deferred restart once no turn is in flight). Nothing to edit by hand: the bot token and owner ID live in the generated systemd unit, not in a file `git pull` would touch, so they survive every update.

## Multi-tenancy: giving someone else access

1. Add their numeric Telegram ID to `whitelist.txt` (comma or newline separated — no restart needed, it's re-read on every message).
2. Tell them to message the bot. They'll get a "not whitelisted" prompt with their ID and a button.
3. Once you've added their ID, they press the button (or just message the bot again) — the bot walks them through `claude auth login` for their **own** account: it sends a login link, they authorize it and paste back the code.
4. From then on, everything they do runs against their own Claude subscription, in `accounts/<their_chat_id>/` — fully separate sessions, usage, and billing from the owner's.

The owner (whoever's Telegram ID is in `OWNER_ID`) uses the default, un-isolated Claude account.  If its OAuth session expires, `/login` starts the same browser flow from Telegram: open the button, authorize Claude, then send the displayed code back to the chat.  No SSH access to the host is needed.

## Permission modes

- `bypass` (default) — no confirmation prompts, equivalent to `--dangerously-skip-permissions`. Convenient, but only use this if you trust everything the bot might be asked to do.
- `default` — dangerous tool calls require `/approve` before running.
- `acceptEdits` — file edits are auto-approved, everything else needs `/approve`.
- `plan` — read-only, Claude can't make any changes.

## Troubleshooting

- **"OWNER_ID" / "TELEGRAM_BOT_TOKEN" KeyError on startup** — both are required environment variables (no defaults), set them in the systemd unit's `Environment=` lines.
- **Bot doesn't respond at all** — check `journalctl -u <service-name> -f`; a common cause is `claude auth status` showing not logged in for the account the process is running as.
- **Draft/"thinking" preview doesn't show code formatting** — this depends on your Telegram client's active theme; some custom themes don't render `pre` blocks distinctly. Try the default Telegram theme to confirm.
- **A whitelisted user's login never completes** — check for a stuck `claude auth login` subprocess (`ps aux | grep "auth login"`); they can just send `/login` again to retry.

## License

MIT, see [LICENSE](LICENSE).

`telegram_format.py` is adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT License, Copyright (c) 2025 Nous Research) — see the file header for details.
