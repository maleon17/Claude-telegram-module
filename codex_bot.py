#!/usr/bin/env python3
"""Compatibility launcher; the Codex bot lives in codex-telegram-bot/."""

import os
import sys
from pathlib import Path


BOT_ENTRYPOINT = Path(__file__).resolve().parent / "codex-telegram-bot" / "bot.py"
os.execv(sys.executable, [sys.executable, str(BOT_ENTRYPOINT), *sys.argv[1:]])
