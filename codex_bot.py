#!/usr/bin/env python3
"""Compatibility launcher for the standalone Codex Telegram bot."""

import os
import sys
from pathlib import Path


BOT_ROOT = Path.home() / "codex-telegram-bot"
BOT_ENTRYPOINT = BOT_ROOT / "bot.py"
os.environ["CODEX_BOT_STATE_FILE"] = str(BOT_ROOT / "state.json")
os.execv(sys.executable, [sys.executable, str(BOT_ENTRYPOINT), *sys.argv[1:]])
