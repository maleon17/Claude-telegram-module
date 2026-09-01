#!/usr/bin/env bash
# Updates this install to the latest push on the tracking branch. Safe to
# re-run; does nothing destructive to local, untracked config (the bot's
# token/owner id live in the generated systemd unit, not in any file this
# touches).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: local changes to tracked files present -- resolve or stash them first." >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"

python3 -m py_compile bridge.py runtime.py telegram_api.py state_store.py chat_process.py handlers.py telegram_format.py

echo "Updated to $(git rev-parse --short HEAD) on $BRANCH."
