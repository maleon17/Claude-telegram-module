#!/usr/bin/env bash
# Interactive installer for the Claude Code <-> Telegram bridge.
#
# What this script does:
#   - checks python3 and the `claude` CLI are present
#   - checks you're already logged in via `claude auth login` (this part is
#     NOT automated -- it's an interactive browser OAuth flow, run it
#     yourself first if needed)
#   - asks for your bot token / Telegram ID / install directory
#   - generates a systemd unit from claude-telegram-bridge.service.example
#     and installs it as a system service
#
# Safe to re-run: it only overwrites the generated unit file, nothing else.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "== Claude Code <-> Telegram bridge setup =="
echo

# --- prerequisites -----------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it first (e.g. 'sudo apt install python3') and re-run this script."
    exit 1
fi

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
fi
if [ -z "$CLAUDE_BIN" ]; then
    echo "Claude Code CLI not found."
    echo "Install it first:"
    echo "  curl -fsSL https://claude.ai/install.sh | bash"
    echo "then re-run this script."
    exit 1
fi
echo "Found Claude Code CLI: $CLAUDE_BIN"

if ! "$CLAUDE_BIN" auth status 2>/dev/null | grep -q '"loggedIn": true\|loggedIn.*true'; then
    echo
    echo "You're not logged in to Claude Code yet (needs a Pro/Team/Max subscription --"
    echo "this bridge runs on your subscription, not a metered API key)."
    echo "Run this yourself first (it opens a browser for OAuth login):"
    echo "  $CLAUDE_BIN auth login --claudeai"
    echo "Then re-run this script."
    exit 1
fi
echo "Claude Code is authenticated."
echo

# --- gather config -------------------------------------------------------

read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
if [ -z "$BOT_TOKEN" ]; then
    echo "A bot token is required."
    exit 1
fi

read -rp "Your Telegram numeric user ID (message @userinfobot to get it): " OWNER_ID
if ! [[ "$OWNER_ID" =~ ^[0-9]+$ ]]; then
    echo "Owner ID must be numeric."
    exit 1
fi

read -rp "Service name [claude-telegram-bridge]: " SERVICE_NAME
SERVICE_NAME="${SERVICE_NAME:-claude-telegram-bridge}"

INSTALL_USER="$(whoami)"
INSTALL_DIR="$SCRIPT_DIR"

echo
echo "Will install as systemd service '$SERVICE_NAME', running as user '$INSTALL_USER',"
echo "from '$INSTALL_DIR'."
read -rp "Continue? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# --- generate + install the unit ----------------------------------------

UNIT_FILE="/tmp/${SERVICE_NAME}.service"
sed \
    -e "s|__USER__|${INSTALL_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    -e "s|__BOT_TOKEN__|${BOT_TOKEN}|g" \
    -e "s|__OWNER_ID__|${OWNER_ID}|g" \
    claude-telegram-bridge.service.example > "$UNIT_FILE"

echo
echo "Generated unit at $UNIT_FILE -- installing (requires sudo):"
sudo cp "$UNIT_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$UNIT_FILE"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Done. Checking status:"
sudo systemctl status "${SERVICE_NAME}.service" --no-pager || true

echo
echo "Next: message your bot on Telegram to start using it."
echo "Logs:   journalctl -u ${SERVICE_NAME}.service -f"
echo "Whitelist for other users: edit whitelist.txt in $INSTALL_DIR (comma/newline-separated IDs, no restart needed)."
