import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import urllib.parse
import urllib.error
import uuid
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_format import format_message, strip_mdv2, escape_mdv2

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = os.environ["OWNER_ID"]
WORKDIR = os.environ.get("BRIDGE_WORKDIR", "/home/mishin")
STATE_FILE = os.environ.get(
    "BRIDGE_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
SERVICE_NAME = os.environ.get("SERVICE_NAME")  # this instance's own systemd unit, for /restart
PROJECTS_DIR = os.path.join(
    os.path.expanduser("~/.claude/projects"), WORKDIR.replace("/", "-")
)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
EDIT_THROTTLE_S = 1.3
# Same Braille frame set as jarvis-ask's THINKING_SPINNER_FRAMES (claude_ask.py)
# -- a purely cosmetic "still alive" cue for the live-progress message (see
# _flush_draft), advanced at most once per EDIT_THROTTLE_S/spinner-ticker tick,
# nowhere near the ~0.5s dedicated-timer cadence that got jarvis-ask's account
# banned from a group once -- this only ever rides on the same throttled edit
# calls that already existed, never a faster loop of its own.
THINKING_SPINNER_FRAMES = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"
MAX_MSG_LEN = 4000
COST_WARNING_USD = 100.0  # one-time per-session heads-up, see add_usage()
RICH_MAX_CHARS = 32000  # Bot API 10.1 cap is 32768; leave headroom.

# Per-instance uploads dir, derived from STATE_FILE so the two bridge
# instances (different bot tokens) never share incoming-file storage.
UPLOADS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)),
    "uploads_" + os.path.splitext(os.path.basename(STATE_FILE))[0],
)

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 45 * 1024 * 1024
FILE_PATH_RE = re.compile(
    r"(/(?:[\w.\-]+/)+[\w.\-]+\.(?:"
    r"png|jpe?g|gif|webp|bmp|svg|pdf|zip|tar|gz|txt|md|csv|json"
    r"|py|js|ts|html|mp3|mp4|wav|docx?|xlsx?|pptx?"
    r")\b)"
)

# chat_id -> {"proc", "signature", "reader_thread", "last_activity",
# "original_prompt"} for that chat's PERSISTENT `claude --input-format=
# stream-json` process (2026-08-18 migration off spawn-per-message -- see
# _ensure_chat_process/_chat_reader_loop). Only 2 real users on this
# instance (owner + father), so holding one live process per chat
# indefinitely is cheap and buys back the per-message CLI cold-start
# (~6s measured) and MCP reconnect churn spawn-per-message paid every
# single turn, plus fixes background-task notifications structurally
# (confirmed live: a backgrounded Bash/Agent task's completion arrives as
# a spontaneous new stream event on the SAME open stdout, no new stdin
# message needed -- the old WAKEUP_SIGNAL_DIR file-signal convention
# below was a workaround for exactly this gap and is no longer load-
# bearing, kept only as a harmless legacy fallback).
chat_procs = {}
chat_procs_lock = threading.Lock()
# Reap a chat's persistent process after this long with no activity --
# not for cost (RAM is not the constraint here), just hygiene against an
# unbounded number of long-idle MCP connections/fds piling up forever.
CHAT_PROC_IDLE_TIMEOUT_S = 6 * 3600
# chat_ids with a turn currently in flight (guards against overlapping
# --resume calls onto the same session).
busy_chats = set()
# Mutable box so the restart-watcher background thread (see
# _restart_watcher_loop) can read main()'s current getUpdates offset
# without needing it passed in explicitly.
current_offset = [0]

# Guards every read-modify-write on the shared `state` dict + its on-disk
# save. Without this, two chats messaging concurrently could each load,
# mutate, and save `state` in an interleaved order, silently losing one
# side's update (last writer wins on the whole file, not just their key).
state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Multi-tenant accounts: each whitelisted chat_id other than OWNER_ID gets its
# own isolated CLAUDE_CONFIG_DIR (own OAuth login, own Pro subscription, own
# sessions/usage) instead of running on OWNER_ID's account. OWNER_ID keeps
# using the default, unisolated `~/.claude` it always has, so existing state
# is untouched.
# ---------------------------------------------------------------------------

WHITELIST_FILE = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)), "whitelist.txt"
)
RESTART_SIGNAL_FILE = STATE_FILE + ".restart_signal"
ACCOUNTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)), "accounts"
)

# Background-task wakeup (LEGACY, kept as a harmless fallback -- see 2026-
# 08-18 migration note on `chat_procs` above): originally built because a
# `claude -p --resume` turn's process exited the instant its reply was
# sent, so a backgrounded shell command finishing AFTER that had nothing
# left alive to notice. Under the persistent-process model this no longer
# happens -- confirmed live that a backgrounded task's completion arrives
# as a spontaneous new event on the chat's still-open stdout, no signal
# file needed. Left in place (env vars still injected into every turn,
# watcher thread still runs) in case anything still relies on the
# convention, but nothing should need to reach for it going forward.
WAKEUP_SIGNAL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)),
    "wakeup_signals_" + os.path.splitext(os.path.basename(STATE_FILE))[0],
)
os.makedirs(WAKEUP_SIGNAL_DIR, exist_ok=True)

# chat_id -> {"proc": Popen, "fifo": path} for a login flow in progress.
pending_logins = {}


def load_whitelist():
    ids = {str(OWNER_ID)}
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE) as f:
                raw = f.read()
            for part in raw.replace("\n", ",").split(","):
                part = part.strip()
                if part:
                    ids.add(part)
        except Exception:
            pass
    return ids


def account_dir(chat_id):
    if str(chat_id) == str(OWNER_ID):
        return None  # default ~/.claude, unchanged behavior
    d = os.path.join(ACCOUNTS_DIR, str(chat_id))
    os.makedirs(d, exist_ok=True)
    return d


def claude_env(config_dir, chat_id=None):
    # Used to short-circuit to None (inherit parent env as-is) when there
    # was nothing to override -- now always builds an explicit dict once
    # chat_id needs injecting too. Functionally equivalent for existing
    # callers that don't pass chat_id and have no config_dir: an explicit
    # copy of os.environ behaves the same as env=None for Popen.
    if not config_dir and chat_id is None:
        return None
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    if chat_id is not None:
        # Lets a backgrounded shell command self-report completion without
        # Claude needing to already know/hardcode its own chat_id -- see
        # WAKEUP_SIGNAL_DIR above.
        env["CHAT_ID"] = str(chat_id)
        env["WAKEUP_SIGNAL_DIR"] = WAKEUP_SIGNAL_DIR
    return env

# ---------------------------------------------------------------------------
BATCH_DEBOUNCE_S = 1.5
pending_batches = {}
batch_timers = {}


