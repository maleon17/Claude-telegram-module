#!/usr/bin/env python3
"""Claude Code <-> Telegram bridge.

Drives `claude -p --output-format=stream-json` per inbound Telegram message,
streaming tool calls and intermediate text back as live message edits, with
per-chat session management (.new / .sessions / .resume).

Formatting (format_message) is ported from hermes-agent, MIT License,
Copyright (c) 2025 Nous Research — see telegram_format.py.
"""

import json
import mimetypes
import os
import re
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
PROJECTS_DIR = os.path.join(
    os.path.expanduser("~/.claude/projects"), WORKDIR.replace("/", "-")
)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
EDIT_THROTTLE_S = 1.3
MAX_MSG_LEN = 4000
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

# chat_id -> subprocess.Popen of the currently running `claude` turn.
active_procs = {}
# chat_ids with a turn currently in flight (guards against overlapping
# --resume calls onto the same session).
busy_chats = set()

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
ACCOUNTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)), "accounts"
)

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


def claude_env(config_dir):
    if not config_dir:
        return None
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return env

# ---------------------------------------------------------------------------
# Telegram Bot API (stdlib only)
# ---------------------------------------------------------------------------


def tg_call(method, params=None, timeout=20):
    url = f"{API_BASE}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message(chat_id, text, reply_to=None):
    formatted = format_message(text)
    if len(formatted) > MAX_MSG_LEN:
        return send_message_chunked(chat_id, text, reply_to)
    params = {"chat_id": chat_id, "text": formatted, "parse_mode": "MarkdownV2"}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    r = tg_call("sendMessage", params)
    if not r.get("ok"):
        # Fall back to plain text if MarkdownV2 parsing rejected something.
        params["text"] = strip_mdv2(text)
        params.pop("parse_mode", None)
        r = tg_call("sendMessage", params)
    return r


def send_message_chunked(chat_id, text, reply_to=None):
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_MSG_LEN:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    last = None
    for part in parts:
        formatted = format_message(part)
        params = {"chat_id": chat_id, "text": formatted, "parse_mode": "MarkdownV2"}
        r = tg_call("sendMessage", params)
        if not r.get("ok"):
            params["text"] = strip_mdv2(part)
            params.pop("parse_mode", None)
            r = tg_call("sendMessage", params)
        last = r
    return last


def edit_message(chat_id, message_id, text):
    formatted = format_message(text)
    if len(formatted) > MAX_MSG_LEN:
        formatted = formatted[: MAX_MSG_LEN - 20] + "\n\n_\\.\\.\\.continuing_"
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": formatted,
        "parse_mode": "MarkdownV2",
    }
    r = tg_call("editMessageText", params)
    if not r.get("ok"):
        desc = str(r.get("description", ""))
        if "not modified" in desc.lower():
            return r
        params["text"] = strip_mdv2(text)[:MAX_MSG_LEN]
        params.pop("parse_mode", None)
        r = tg_call("editMessageText", params)
    return r


def send_typing(chat_id):
    tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def _multipart_request(method, fields, file_field, file_path, timeout=60):
    boundary = uuid.uuid4().hex
    url = f"{API_BASE}/{method}"
    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    filename = os.path.basename(file_path)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'
    ).encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    with open(file_path, "rb") as f:
        body += f.read()
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_photo(chat_id, path, caption=None):
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    return _multipart_request("sendPhoto", fields, "photo", path)


def send_document(chat_id, path, caption=None):
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    return _multipart_request("sendDocument", fields, "document", path)


def send_attachment(chat_id, path, caption=None):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    size = os.path.getsize(path)
    if ext in IMAGE_EXTS and size <= MAX_PHOTO_BYTES:
        r = send_photo(chat_id, path, caption)
    elif size <= MAX_DOCUMENT_BYTES:
        r = send_document(chat_id, path, caption)
    else:
        send_message(chat_id, f"Файл `{path}` слишком большой для отправки ({size} байт).")
        return
    if not r.get("ok"):
        send_message(chat_id, f"Не удалось отправить `{path}`: {r.get('description', r)}")


def download_telegram_file(chat_id, file_id, filename_hint=None):
    r = tg_call("getFile", {"file_id": file_id})
    if not r.get("ok"):
        return None
    file_path = r["result"]["file_path"]
    url = f"{FILE_API_BASE}/{file_path}"
    name = filename_hint or os.path.basename(file_path) or f"{file_id}.bin"
    name = re.sub(r"[^\w.\-]", "_", name)
    chat_dir = os.path.join(UPLOADS_DIR, str(chat_id))
    local_path = os.path.join(chat_dir, f"{int(time.time() * 1000)}_{name}")
    try:
        os.makedirs(chat_dir, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp, open(local_path, "wb") as f:
            f.write(resp.read())
        return local_path
    except Exception:
        return None


def extract_existing_files(text):
    found = []
    seen = set()
    for m in FILE_PATH_RE.finditer(text or ""):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# Rich messages (Telegram Bot API 10.1) — raw markdown, no MarkdownV2 escaping
# needed. Supports <details><summary>...</summary>...</details> (collapsible
# blocks), real tables, and GFM task lists. Falls back to the legacy
# MarkdownV2 path on any capability/permanent error.
# ---------------------------------------------------------------------------


def send_rich(chat_id, markdown_text, reply_to=None):
    markdown_text = markdown_text[:RICH_MAX_CHARS]
    params = {"chat_id": chat_id, "rich_message": {"markdown": markdown_text}}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to}
    r = tg_call("sendRichMessage", params)
    if not r.get("ok"):
        return send_message(chat_id, markdown_text, reply_to)
    return r


def edit_rich(chat_id, message_id, markdown_text):
    markdown_text = markdown_text[:RICH_MAX_CHARS]
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {"markdown": markdown_text},
    }
    r = tg_call("editMessageText", params)
    if not r.get("ok"):
        desc = str(r.get("description", ""))
        if "not modified" in desc.lower():
            return r
        return edit_message(chat_id, message_id, markdown_text)
    return r


# ---------------------------------------------------------------------------
# Incoming rich_message (Bot API 10.1) -> plain Markdown, so a forwarded/sent
# rich message becomes a readable prompt instead of being silently dropped
# (the bridge only ever looked at `text`, but a rich message has no `text`
# at all -- the content lives entirely in `rich_message.blocks`). Schema per
# https://core.telegram.org/bots/api#richtext / #richblock.
# ---------------------------------------------------------------------------


def _richtext_to_md(rt):
    if rt is None:
        return ""
    if isinstance(rt, str):
        return rt
    if isinstance(rt, list):
        return "".join(_richtext_to_md(x) for x in rt)
    if not isinstance(rt, dict):
        return str(rt)

    t = rt.get("type")
    inner = _richtext_to_md(rt.get("text"))

    if t == "bold":
        return f"**{inner}**"
    if t == "italic":
        return f"_{inner}_"
    if t == "underline":
        return f"__{inner}__"
    if t == "strikethrough":
        return f"~~{inner}~~"
    if t == "spoiler":
        return f"||{inner}||"
    if t == "marked":
        return f"=={inner}=="
    if t == "code":
        return f"`{inner}`"
    if t == "superscript":
        return f"^{inner}^"
    if t == "custom_emoji":
        return rt.get("alternative_text", "")
    if t == "mathematical_expression":
        return f"${rt.get('expression', '')}$"
    if t == "url":
        return f"[{inner}]({rt.get('url', '')})"
    if t == "email_address":
        return f"[{inner}](mailto:{rt.get('email_address', '')})"
    if t == "mention":
        return f"@{rt.get('username') or inner}"
    if t == "hashtag":
        return f"#{rt.get('hashtag') or inner}"
    if t == "cashtag":
        return f"${rt.get('cashtag') or inner}"
    if t == "bot_command":
        return f"/{rt.get('bot_command') or inner}"
    # date_time, text_mention, subscript, phone_number, bank_card_number,
    # anchor(_link), reference(_link) -- no clean markdown equivalent, just
    # keep the visible text.
    return inner


def _richtable_to_md(block):
    rows = block.get("cells") or []
    if not rows:
        return ""
    md_rows = []
    for row in rows:
        cells = []
        for cell in row:
            txt = _richtext_to_md((cell or {}).get("text")) if cell else ""
            cells.append(txt.replace("|", "\\|").replace("\n", " "))
        md_rows.append(cells)
    width = max(len(r) for r in md_rows)
    lines = []
    header, *rest = md_rows
    header = header + [""] * (width - len(header))
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(" --- " for _ in range(width)) + "|")
    for row in rest:
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(row) + " |")
    out = "\n".join(lines)
    caption = block.get("caption")
    if caption:
        out = f"**{_richtext_to_md(caption)}**\n\n{out}"
    return out


def _richblock_to_md(block):
    t = block.get("type")

    if t == "paragraph":
        return _richtext_to_md(block.get("text"))
    if t == "heading":
        level = max(1, min(6, block.get("size") or 3))
        return f"{'#' * level} {_richtext_to_md(block.get('text'))}"
    if t == "pre":
        return f"```{block.get('language', '')}\n{_richtext_to_md(block.get('text'))}\n```"
    if t == "footer":
        return f"_{_richtext_to_md(block.get('text'))}_"
    if t == "divider":
        return "---"
    if t == "mathematical_expression":
        return f"$$\n{block.get('expression', '')}\n$$"
    if t == "anchor":
        return ""
    if t == "list":
        lines = []
        for item in block.get("items", []):
            content = " ".join(_richblock_to_md(b) for b in item.get("blocks", []) if b)
            if item.get("has_checkbox"):
                prefix = "- [x] " if item.get("is_checked") else "- [ ] "
            else:
                label = item.get("label") or "-"
                prefix = f"{label} " if label == "-" else f"{label}. "
            lines.append(f"{prefix}{content}")
        return "\n".join(lines)
    if t == "blockquote":
        content = "\n".join(_richblock_to_md(b) for b in block.get("blocks", []) if b)
        quoted = "\n".join(f"> {ln}" for ln in content.splitlines()) if content else ">"
        credit = block.get("credit")
        if credit:
            quoted += f"\n> — {_richtext_to_md(credit)}"
        return quoted
    if t == "pullquote":
        out = f"> {_richtext_to_md(block.get('text'))}"
        credit = block.get("credit")
        if credit:
            out += f"\n> — {_richtext_to_md(credit)}"
        return out
    if t == "table":
        return _richtable_to_md(block)
    if t == "details":
        summary = _richtext_to_md(block.get("summary"))
        content = "\n".join(_richblock_to_md(b) for b in block.get("blocks", []) if b)
        return f"<details>\n<summary>{summary}</summary>\n\n{content}\n</details>"
    if t in ("collage", "slideshow", "animation", "audio", "photo", "video", "voice_note"):
        caption = block.get("caption")
        cap_text = _richtext_to_md(caption.get("text")) if isinstance(caption, dict) else ""
        return f"[{t}{': ' + cap_text if cap_text else ''}]"
    # "thinking" can't appear in a received message at all.
    return ""


def rich_message_to_markdown(rich_message):
    blocks = rich_message.get("blocks") or []
    parts = [_richblock_to_md(b) for b in blocks]
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def get_session(state, chat_id):
    return state.get(str(chat_id), {}).get("session_id")


def set_session(state, chat_id, session_id):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        entry["session_id"] = session_id
        entry["updated_at"] = time.time()
        save_state(state)


def clear_session(state, chat_id):
    with state_lock:
        entry = state.get(str(chat_id))
        if entry:
            entry.pop("session_id", None)
            save_state(state)


def _empty_usage():
    return {
        "calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "last_context_tokens": 0,
        "by_model": {},
    }


def add_usage(state, chat_id, session_id, result_event):
    if not session_id:
        return
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        sessions = entry.setdefault("sessions", {})
        usage = sessions.setdefault(session_id, _empty_usage())
        usage.setdefault("by_model", {})

        usage["calls"] += 1
        usage["cost_usd"] += result_event.get("total_cost_usd") or 0.0
        u = result_event.get("usage") or {}
        usage["input_tokens"] += u.get("input_tokens") or 0
        usage["output_tokens"] += u.get("output_tokens") or 0
        usage["cache_read_tokens"] += u.get("cache_read_input_tokens") or 0
        usage["cache_creation_tokens"] += u.get("cache_creation_input_tokens") or 0
        usage["last_context_tokens"] = (
            (u.get("input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
        )

        for model_name, mu in (result_event.get("modelUsage") or {}).items():
            bm = usage["by_model"].setdefault(
                model_name, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
            )
            bm["cost_usd"] += mu.get("costUSD") or 0.0
            bm["input_tokens"] += mu.get("inputTokens") or 0
            bm["output_tokens"] += mu.get("outputTokens") or 0

        save_state(state)


def get_usage(state, chat_id, session_id):
    if not session_id:
        return _empty_usage()
    return state.get(str(chat_id), {}).get("sessions", {}).get(session_id, _empty_usage())


def get_model(state, chat_id):
    return state.get(str(chat_id), {}).get("model")


def set_model(state, chat_id, model):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        if model:
            entry["model"] = model
        else:
            entry.pop("model", None)
        save_state(state)


def get_permission_mode(state, chat_id):
    """None means bypass (current default behavior, unchanged)."""
    return state.get(str(chat_id), {}).get("permission_mode")


def set_permission_mode(state, chat_id, mode):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        if mode and mode != "bypass":
            entry["permission_mode"] = mode
        else:
            entry.pop("permission_mode", None)
        save_state(state)


def get_workspace(state, chat_id):
    return state.get(str(chat_id), {}).get("workspace") or WORKDIR


def set_workspace(state, chat_id, path):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        if path:
            entry["workspace"] = path
        else:
            entry.pop("workspace", None)
        save_state(state)


def set_pending_prompt(state, chat_id, prompt):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        entry["pending_prompt"] = prompt
        save_state(state)


def get_pending_prompt(state, chat_id):
    return state.get(str(chat_id), {}).get("pending_prompt")


def clear_pending_prompt(state, chat_id):
    with state_lock:
        entry = state.get(str(chat_id))
        if entry:
            entry.pop("pending_prompt", None)
            save_state(state)


def get_account_status(state, chat_id):
    """None (not started) / "awaiting_code" / "ready"."""
    if str(chat_id) == str(OWNER_ID):
        return "ready"
    return state.get(str(chat_id), {}).get("account_status")


def set_account_status(state, chat_id, status):
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        if status:
            entry["account_status"] = status
        else:
            entry.pop("account_status", None)
        save_state(state)


def fetch_account_limits(config_dir=None):
    """Shell out to Claude Code's own /usage slash command for real account-level
    5-hour/weekly rate limit info. Runs as a standalone call (no --resume) so it
    doesn't pollute any conversation's session transcript.

    Returns just the two headline lines (session / week), not the verbose
    "what's contributing" breakdown.
    """
    try:
        proc = subprocess.run(
            [
                CLAUDE_BIN,
                "-p",
                "/usage",
                "--output-format=stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
            ],
            cwd=WORKDIR,
            env=claude_env(config_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "result":
                text = d.get("result", "")
                headline = [
                    ln.strip()
                    for ln in text.splitlines()
                    if ln.strip().startswith(("Current session", "Current week"))
                ]
                return "\n".join(headline) if headline else text
    except Exception as e:
        return f"(не удалось получить лимиты: {e})"
    return "(нет данных)"


def projects_dir_for(config_dir, workspace):
    root = os.path.expanduser(config_dir) if config_dir else os.path.expanduser("~/.claude")
    return os.path.join(root, "projects", (workspace or WORKDIR).replace("/", "-"))


def session_message_count(session_id, projects_dir=PROJECTS_DIR):
    if not session_id:
        return None
    path = os.path.join(projects_dir, f"{session_id}.jsonl")
    if not os.path.exists(path):
        return None
    count = 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") in ("user", "assistant"):
                    count += 1
    except Exception:
        pass
    return count


def list_sessions(projects_dir=PROJECTS_DIR, limit=10):
    files = glob.glob(os.path.join(projects_dir, "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for path in files[:limit]:
        sid = os.path.basename(path)[:-6]
        mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        preview = ""
        try:
            with open(path) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    content = d.get("content")
                    if isinstance(content, str) and content.strip():
                        preview = content.strip().replace("\n", " ")[:60]
                        break
        except Exception:
            pass
        out.append((sid, mtime, preview))
    return out


# ---------------------------------------------------------------------------
# Claude Code invocation
# ---------------------------------------------------------------------------


def tool_call_line(name, tool_input):
    if name == "Bash":
        cmd = tool_input.get("command", "")[:500]
        return f"🔧 Bash:\n```\n{cmd}\n```"
    if name in ("Read",):
        return f"📖 Read: `{tool_input.get('file_path', '')}`"
    if name in ("Write", "Edit"):
        return f"✏️ {name}: `{tool_input.get('file_path', '')}`"
    if name in ("WebSearch",):
        return f"🔍 WebSearch: `{tool_input.get('query', '')[:150]}`"
    if name in ("WebFetch",):
        return f"🌐 WebFetch: `{tool_input.get('url', '')}`"
    keys_preview = json.dumps(tool_input)[:300]
    return f"🔧 {name}:\n```\n{keys_preview}\n```"


def draft_tool_label_and_content(name, tool_input):
    """Same tool dispatch as tool_call_line, but returns (label, content)
    separately instead of one pre-formatted string -- for the live draft,
    where the label sits on its own line above the code block, not baked
    into it."""
    if name == "Bash":
        return "🔧 Bash", tool_input.get("command", "")
    if name == "Read":
        return "📖 Read", tool_input.get("file_path", "")
    if name in ("Write", "Edit"):
        return f"✏️ {name}", tool_input.get("file_path", "")
    if name == "WebSearch":
        return "🔍 WebSearch", tool_input.get("query", "")
    if name == "WebFetch":
        return "🌐 WebFetch", tool_input.get("url", "")
    return f"🔧 {name}", json.dumps(tool_input)


def run_claude(
    chat_id,
    prompt,
    session_id,
    state,
    model=None,
    permission_mode=None,
    workspace=None,
    config_dir=None,
):
    """Drive one turn. Live progress is shown via a `sendMessageDraft`
    (Bot API 9.3) animated one-line preview, keyed on chat_id as draft_id --
    ephemeral, expires after 30s if not refreshed, and can't itself persist
    a message. Once the turn finishes, the full tool-call/reasoning log (if
    any) and the final answer are each sent ONCE as real, persisted
    messages -- no more repeated edits of long-lived messages.

    permission_mode: None (or "bypass") -> --dangerously-skip-permissions
    (current default, unchanged). Any other value -> --permission-mode <mode>,
    which can produce real permission_denials in the result event.
    """
    args = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format=stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if permission_mode and permission_mode != "bypass":
        args.append(f"--permission-mode={permission_mode}")
    else:
        args.append("--dangerously-skip-permissions")
    if session_id:
        args.append(f"--resume={session_id}")
    if model:
        args.append(f"--model={model}")

    proc = subprocess.Popen(
        args,
        cwd=workspace or WORKDIR,
        env=claude_env(config_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    active_procs[chat_id] = proc

    log_lines = []
    current_session_id = session_id
    last_draft_edit = 0.0
    final_text = None
    last_text_log_index = None
    last_text_log_raw = None
    denials = []
    written_files = []

    # Draft rendering state, two levels:
    # - draft_thought: the current "thinking"/text block, plain (no code
    #   formatting). Stays as an anchor across however many tool calls
    #   happen under it.
    # - draft_cmd / draft_res: the current tool call + its result, each
    #   their own code block, overwriting the previous pair the same way
    #   as before -- but scoped UNDER the current thought.
    # A new thought clears everything (old thought + whatever tool
    # cmd/result was showing) and starts fresh; a new tool call only
    # clears the previous cmd/result, leaving the thought in place.
    draft_thought = None
    draft_cmd_label = None
    draft_cmd = None
    draft_res_blocks = []  # list of (label, content) -- Bash can have both stdout and stderr

    def _draft_clean(s, limit=200):
        s = strip_mdv2(s)
        s = s.replace("```", "").replace("`", "").replace("\\", "")
        return re.sub(r"\s+", " ", s).strip()[:limit]

    def flush_draft(force=False):
        # Telegram's own draft bubble already renders a native animated
        # indicator on its own -- our own spinner char + a literal "..."
        # on top of that was three competing animations in one line. Just
        # send the status text and let Telegram do the "alive" part.
        nonlocal last_draft_edit
        now = time.time()
        if not force and (now - last_draft_edit) < EDIT_THROTTLE_S:
            return
        lines = []
        if draft_thought:
            # Unlike draft_cmd/draft_res (inside a code span, where regular
            # punctuation needs no escaping), this is raw top-level
            # MarkdownV2 text -- real sentences are full of unescaped
            # ".,()-!" etc., which made sendMessageDraft fail its parse
            # silently (no error is ever checked) and just freeze on
            # whatever the last successful frame was.
            lines.append(escape_mdv2(draft_thought))
        if draft_cmd:
            # Triple backticks (a "pre" entity) instead of single -- single
            # backtick is just inline "code" (tap-to-copy, no distinct
            # visual box); pre blocks get their own background, matching
            # what the final persisted process message already does.
            lines.append(escape_mdv2(f"{draft_cmd_label}:"))
            lines.append(f"```\n{draft_cmd}\n```")
            for label, content in draft_res_blocks:
                lines.append(escape_mdv2(f"{label}:"))
                lines.append(f"```\n{content}\n```")
        text = "\n".join(lines) if lines else "Думаю"
        r = tg_call("sendMessageDraft", {
            "chat_id": chat_id,
            "draft_id": chat_id,
            "parse_mode": "MarkdownV2",
            "text": text,
        })
        if not r.get("ok"):
            # A parse failure here used to just freeze the draft on its
            # last successful frame with no indication anything was wrong.
            # Fall back to plain text (no entities) so it keeps moving.
            tg_call("sendMessageDraft", {
                "chat_id": chat_id,
                "draft_id": chat_id,
                "text": strip_mdv2(text).replace("```", ""),
            })
        last_draft_edit = now

    try:
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                d = json.loads(raw_line)
            except Exception:
                continue

            t = d.get("type")

            if t == "system" and d.get("subtype") == "init":
                current_session_id = d.get("session_id") or current_session_id
                continue

            if t == "stream_event":
                ev = d.get("event", {})
                if ev.get("type") == "content_block_delta":
                    flush_draft()
                continue

            if t == "assistant":
                content = d.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        log_lines.append(tool_call_line(name, tool_input))
                        # New tool call: clear the previous command+result
                        # pair, but leave the current thought (if any) in
                        # place -- tool calls happen "under" a thought.
                        label, content = draft_tool_label_and_content(name, tool_input)
                        draft_cmd_label = label
                        draft_cmd = _draft_clean(content)
                        draft_res_blocks = []
                        flush_draft(force=True)
                        if name == "Write":
                            fp = tool_input.get("file_path")
                            if fp:
                                written_files.append(fp)
                    elif block.get("type") in ("text", "thinking"):
                        # "text" is visible reasoning/answer text; "thinking"
                        # is an extended-thinking block (separate field name,
                        # `thinking` not `text`) -- both are a new "thought"
                        # for draft purposes, and both clear everything
                        # (previous thought + whatever tool cmd/result was
                        # showing under it), since a new thought starts a new
                        # episode.
                        text = block.get("text") or block.get("thinking") or ""
                        if text.strip():
                            if block.get("type") == "text":
                                log_lines.append(f"💬 {text}")
                                last_text_log_index = len(log_lines) - 1
                                last_text_log_raw = text
                            draft_thought = _draft_clean(text, limit=300)
                            draft_cmd_label = None
                            draft_cmd = None
                            draft_res_blocks = []
                            flush_draft(force=True)
                flush_draft()
                continue

            if t == "user":
                content = d.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_result":
                        is_error = block.get("is_error", False)
                        # Bash results carry separate stdout/stderr at the
                        # top level (tool_use_result), even when the command
                        # succeeded overall -- e.g. warnings on stderr with
                        # exit 0. The merged `content` field used below loses
                        # that distinction.
                        tur = d.get("tool_use_result")
                        stdout_text = tur.get("stdout") if isinstance(tur, dict) else None
                        stderr_text = tur.get("stderr") if isinstance(tur, dict) else None

                        if stdout_text is not None or stderr_text is not None:
                            stdout_text = (stdout_text or "").strip()
                            stderr_text = (stderr_text or "").strip()
                            log_parts = []
                            res_blocks = []
                            if stdout_text:
                                preview = stdout_text[:400]
                                log_parts.append(f"✅ Stdout:\n```\n{preview}\n```")
                                res_blocks.append(("✅ Stdout", _draft_clean(preview)))
                            if stderr_text:
                                preview = stderr_text[:400]
                                log_parts.append(f"❌ Stderr:\n```\n{preview}\n```")
                                res_blocks.append(("❌ Stderr", _draft_clean(preview)))
                            if not log_parts:
                                icon = "❌" if is_error else "✅"
                                log_parts.append(f"{icon} (пусто)")
                            log_lines.append("\n".join(log_parts))
                            draft_res_blocks = res_blocks
                        else:
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = " ".join(
                                    c.get("text", "") for c in result_content if isinstance(c, dict)
                                )
                            result_content = str(result_content)
                            icon = "❌" if is_error else "✅"
                            preview = result_content.strip()[:400]
                            log_lines.append(f"{icon}\n```\n{preview}\n```")
                            # Result of the CURRENT command: append below
                            # it, don't clear draft_cmd.
                            draft_res_blocks = [(f"{icon} Результат", _draft_clean(preview))]
                        flush_draft(force=True)
                continue

            if t == "result":
                current_session_id = d.get("session_id") or current_session_id
                final_text = d.get("result", "")
                denials.extend(d.get("permission_denials") or [])
                add_usage(state, chat_id, current_session_id, d)
                continue

        proc.wait(timeout=10)
    except Exception:
        log_lines.append(f"❌ bridge error:\n```\n{traceback.format_exc()[-1500:]}\n```")
    finally:
        if proc.poll() is None:
            proc.kill()
        active_procs.pop(chat_id, None)

    stopped = final_text is None and (proc.returncode or 0) < 0

    # The last assistant text block is what becomes the final answer (echoed
    # in the "result" event) — drop it from the process log so it isn't
    # duplicated between the collapsible process block and the answer.
    if (
        last_text_log_index is not None
        and final_text is not None
        and last_text_log_raw is not None
        and last_text_log_raw.strip() == final_text.strip()
        and 0 <= last_text_log_index < len(log_lines)
    ):
        del log_lines[last_text_log_index]

    # Final, ONE-TIME persisted process-log message (only if there's
    # anything to show -- a plain text-only turn with no tool calls has
    # nothing left in log_lines after the dedup above, so it's skipped
    # entirely instead of sending an empty bubble like before).
    if log_lines:
        MAIN_BUDGET = 30000
        LAST_TOOL_RESERVE = 2000
        budget = MAIN_BUDGET + LAST_TOOL_RESERVE
        visible = []
        used = 0
        for line in reversed(log_lines):
            cost = len(line) + 1
            if used + cost > budget and visible:
                break
            visible.append(line)
            used += cost
        visible.reverse()
        hidden = len(log_lines) - len(visible)
        body_lines = [f"…и ещё {hidden} шагов выше…"] if hidden > 0 else []
        body_lines.extend(visible)
        body = "\n".join(body_lines)
        send_rich(chat_id, f"<details><summary>🔧 Процесс ({len(log_lines)})</summary>\n{body}\n</details>")

    if current_session_id:
        set_session(state, chat_id, current_session_id)

    if stopped:
        text_out = "⏹ Остановлено по /stop."
    else:
        text_out = final_text if final_text is not None else "(нет ответа — смотри процесс выше)"
    return text_out, denials, written_files, stopped


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


MODEL_VERSIONS = {
    "opus": ["4.5", "4.6", "4.7", "4.8", "5"],
    "sonnet": ["4.5", "4.6", "5"],
    "haiku": ["4.5"],
    "fable": ["5"],
}
MODEL_ALIASES = tuple(MODEL_VERSIONS.keys())

PERMISSION_MODES = ("bypass", "default", "acceptEdits", "plan")

COMMANDS = [
    ("new", "Начать новую сессию"),
    ("sessions", "Список последних сессий"),
    ("resume", "Продолжить сессию по id"),
    ("status", "Текущее состояние: сессия/модель/режим/workspace"),
    ("stop", "Прервать текущий запрос"),
    ("usage", "Токены, стоимость и лимиты аккаунта"),
    ("model", "Модель: /model opus 4.7, /model sonnet, /model default"),
    ("mode", "Режим подтверждений: bypass/default/acceptEdits/plan"),
    ("workspace", "Рабочая директория для этой сессии"),
    ("approve", "Разрешить заблокированное действие (once/session)"),
    ("deny", "Отклонить заблокированное действие"),
    ("login", "Переподключить свой аккаунт Claude"),
]


def handle_command(chat_id, text, state):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().strip().lstrip("/.")
    arg = arg.strip()

    if cmd == "start":
        return True

    if cmd == "new":
        clear_session(state, chat_id)
        send_message(chat_id, "Начинаю новую сессию.")
        return True

    pdir = projects_dir_for(account_dir(chat_id), get_workspace(state, chat_id))

    if cmd == "sessions":
        sessions = list_sessions(pdir)
        if not sessions:
            send_message(chat_id, "Сессий не найдено.")
            return True
        lines = ["Последние сессии:"]
        current = get_session(state, chat_id)
        for sid, mtime, preview in sessions:
            marker = " ← текущая" if sid == current else ""
            lines.append(f"`{sid[:8]}` {mtime} {preview}{marker}")
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "resume":
        if not arg:
            send_message(chat_id, "Использование: /resume <session_id или префикс>")
            return True
        matches = glob.glob(os.path.join(pdir, f"{arg}*.jsonl"))
        if not matches:
            send_message(chat_id, f"Сессия {arg} не найдена.")
            return True
        sid = os.path.basename(matches[0])[:-6]
        set_session(state, chat_id, sid)
        send_message(chat_id, f"Продолжаю сессию {sid[:8]}.")
        return True

    if cmd == "usage":
        session_id = get_session(state, chat_id)
        u = get_usage(state, chat_id, session_id)
        msg_count = session_message_count(session_id, pdir)
        context_tokens = u.get("last_context_tokens")
        model = get_model(state, chat_id) or "default"

        def fmt(n):
            return f"{n:,}".replace(",", " ")

        lines = [
            "📊 **Session**",
            f"`{session_id[:8] if session_id else 'нет активной'}`  •  Model: {model}",
            f"Messages: {msg_count if msg_count is not None else '—'}",
            (
                f"Context: ~{fmt(context_tokens)} tokens"
                if context_tokens
                else "Context: no data yet"
            ),
            "",
            "🔢 **Tokens (this session)**",
            f"{u['calls']} calls",
            f"in {fmt(u['input_tokens'])}  ·  out {fmt(u['output_tokens'])}  ·  "
            f"cache-r {fmt(u['cache_read_tokens'])}  ·  cache-w {fmt(u['cache_creation_tokens'])}",
            f"(~${u['cost_usd']:.4f} эквивалент по API-тарифу)",
        ]

        by_model = u.get("by_model") or {}
        if by_model:
            lines.append("")
            lines.append("**By model**")
            for name, mu in by_model.items():
                lines.append(
                    f"`{name}`  {fmt(mu['input_tokens'])}/{fmt(mu['output_tokens'])} in/out  "
                    f"(~${mu['cost_usd']:.4f})"
                )

        limits = fetch_account_limits(account_dir(chat_id))
        lines.append("")
        lines.append("📈 **Account limits** (subscription, not credits)")
        for ln in limits.splitlines():
            lines.append(ln)

        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "model":
        if not arg:
            current = get_model(state, chat_id) or "default"
            lines = [f"Текущая модель: `{current}`", "", "Доступно:"]
            for fam, versions in MODEL_VERSIONS.items():
                lines.append(f"  {fam}: {', '.join(versions)} (последняя: {versions[-1]})")
            lines.append("")
            lines.append("Использование: /model <семейство> [версия], /model default")
            send_message(chat_id, "\n".join(lines))
            return True

        parts = arg.lower().split()
        choice = parts[0]

        if choice == "default":
            set_model(state, chat_id, None)
            send_message(chat_id, "Модель сброшена на дефолтную.")
            return True

        if choice not in MODEL_VERSIONS:
            send_message(chat_id, f"Неизвестное семейство. Доступно: {', '.join(MODEL_ALIASES)}, default")
            return True

        versions = MODEL_VERSIONS[choice]
        if len(parts) == 1:
            version = versions[-1]
        else:
            version = parts[1]
            if version not in versions:
                send_message(
                    chat_id,
                    f"У {choice} нет версии {version}. Доступно: {', '.join(versions)}",
                )
                return True

        model_id = f"claude-{choice}-{version.replace('.', '-')}"
        set_model(state, chat_id, model_id)
        send_message(chat_id, f"Модель переключена на {choice} {version} (`{model_id}`).")
        return True

    if cmd == "mode":
        if not arg:
            current = get_permission_mode(state, chat_id) or "bypass"
            send_message(
                chat_id,
                f"Текущий режим: `{current}`\nДоступно: {', '.join(PERMISSION_MODES)}\n\n"
                "bypass — без подтверждений (по умолчанию)\n"
                "default — каждое опасное действие требует /approve\n"
                "acceptEdits — правки файлов авто, остальное требует /approve\n"
                "plan — только чтение, ничего не меняет",
            )
            return True
        choice = arg.lower().strip()
        if choice not in PERMISSION_MODES:
            send_message(chat_id, f"Неизвестный режим. Доступно: {', '.join(PERMISSION_MODES)}")
            return True
        set_permission_mode(state, chat_id, choice)
        send_message(chat_id, f"Режим переключён на {choice}.")
        return True

    if cmd == "workspace":
        if not arg:
            current = get_workspace(state, chat_id)
            send_message(chat_id, f"Текущий workspace: `{current}`\nИспользование: /workspace <путь>, /workspace default")
            return True
        if arg.lower() == "default":
            set_workspace(state, chat_id, None)
            send_message(chat_id, f"Workspace сброшен на {WORKDIR}.")
            return True
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.isdir(path):
            send_message(chat_id, f"Директория не существует: `{path}`")
            return True
        set_workspace(state, chat_id, path)
        send_message(chat_id, f"Workspace переключён на `{path}`.")
        return True

    if cmd == "status":
        session_id = get_session(state, chat_id)
        model = get_model(state, chat_id) or "default"
        mode = get_permission_mode(state, chat_id) or "bypass"
        workspace = get_workspace(state, chat_id)
        busy = "да, выполняется запрос (можно /stop)" if chat_id in busy_chats else "нет"
        acc = get_account_status(state, chat_id) or "не начат"
        lines = [
            "ℹ️ **Статус**",
            f"Сессия: `{session_id[:8] if session_id else 'нет активной'}`",
            f"Модель: `{model}`",
            f"Режим: `{mode}`",
            f"Workspace: `{workspace}`",
            f"Занят: {busy}",
            f"Аккаунт Claude: {acc}",
        ]
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "login":
        if str(chat_id) == str(OWNER_ID):
            send_message(chat_id, "Владелец использует аккаунт по умолчанию, переподключение не нужно.")
            return True
        start_login(chat_id, state)
        send_message(chat_id, "Начинаю переподключение аккаунта Claude...")
        return True

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def register_commands():
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    tg_call("setMyCommands", payload)
    # Some hosts (e.g. this bot token was previously used by the official
    # Channels plugin) have a stale all_private_chats scope registered,
    # which takes precedence over the default scope in private chats and
    # would otherwise mask our command list. Overwrite it explicitly.
    tg_call("setMyCommands", {**payload, "scope": {"type": "all_private_chats"}})


def _run_turn_thread(chat_id, prompt, state, force_permission_mode=None):
    try:
        dispatch_turn(chat_id, prompt, state, force_permission_mode=force_permission_mode)
    except Exception:
        err = traceback.format_exc()[-1500:]
        print(err, flush=True)
        send_message(chat_id, f"Ошибка моста:\n```\n{err}\n```")
    finally:
        busy_chats.discard(chat_id)


def spawn_turn(chat_id, prompt, state, force_permission_mode=None):
    """Run a turn in the background so the poll loop stays responsive to
    /stop and other commands while `claude` is running."""
    if chat_id in busy_chats:
        send_message(chat_id, "Уже выполняю предыдущий запрос. Дождись ответа или используй /stop.")
        return
    busy_chats.add(chat_id)
    threading.Thread(
        target=_run_turn_thread,
        args=(chat_id, prompt, state),
        kwargs={"force_permission_mode": force_permission_mode},
        daemon=True,
    ).start()


# Forwarding a batch of messages (or just typing several in quick succession)
# used to hit busy_chats after the first one and bounce every message after
# it with "already busy" -- one per message. Instead, collect everything
# that arrives within a short debounce window per chat and dispatch it as a
# single combined turn.
BATCH_DEBOUNCE_S = 1.5
BUSY_NOTICE_THROTTLE_S = 5.0
pending_batches = {}
batch_timers = {}
last_busy_notice = {}


def queue_prompt(chat_id, prompt, state):
    if chat_id in busy_chats:
        now = time.time()
        if now - last_busy_notice.get(chat_id, 0) > BUSY_NOTICE_THROTTLE_S:
            send_message(chat_id, "Уже выполняю предыдущий запрос. Дождись ответа или используй /stop.")
            last_busy_notice[chat_id] = now
        return

    pending_batches.setdefault(chat_id, []).append(prompt)
    old_timer = batch_timers.get(chat_id)
    if old_timer:
        old_timer.cancel()

    def fire():
        prompts = pending_batches.pop(chat_id, [])
        batch_timers.pop(chat_id, None)
        if not prompts:
            return
        combined = prompts[0] if len(prompts) == 1 else "\n\n---\n\n".join(prompts)
        spawn_turn(chat_id, combined, state)

    timer = threading.Timer(BATCH_DEBOUNCE_S, fire)
    timer.daemon = True
    batch_timers[chat_id] = timer
    timer.start()


def dispatch_turn(chat_id, prompt, state, force_permission_mode=None):
    """Run one turn against Claude and report denials, if any, offering
    /approve (once) / /approve session / /deny instead of blindly retrying."""
    send_typing(chat_id)

    session_id = get_session(state, chat_id)
    model = get_model(state, chat_id)
    permission_mode = force_permission_mode or get_permission_mode(state, chat_id)
    workspace = get_workspace(state, chat_id)
    config_dir = account_dir(chat_id)

    final_text, denials, written_files, stopped = run_claude(
        chat_id,
        prompt,
        session_id,
        state,
        model,
        permission_mode,
        workspace,
        config_dir,
    )
    send_rich(chat_id, final_text or "(пусто)")

    if not stopped:
        attachments = list(written_files)
        for p in extract_existing_files(final_text or ""):
            if p not in attachments:
                attachments.append(p)
        for path in attachments:
            if os.path.isfile(path):
                send_attachment(chat_id, path)

    if denials:
        set_pending_prompt(state, chat_id, prompt)
        lines = ["🚫 **Заблокировано** (нужно разрешение):"]
        for d in denials[:10]:
            lines.append(f"`{d.get('tool_name', '?')}`  {json.dumps(d.get('tool_input', {}))[:150]}")
        lines.append("")
        lines.append("/approve — повторить один раз с bypass")
        lines.append("/approve session — включить bypass насовсем для этой сессии")
        lines.append("/deny — оставить как есть")
        send_message(chat_id, "\n".join(lines))
    else:
        clear_pending_prompt(state, chat_id)


# ---------------------------------------------------------------------------
# Multi-tenant onboarding: whitelist check -> isolated Claude login per
# non-owner chat_id, so each whitelisted person uses their OWN subscription
# instead of piggy-backing on OWNER_ID's.
# ---------------------------------------------------------------------------

LOGIN_TIMEOUT_S = 180


def send_whitelist_prompt(chat_id):
    text = (
        f"Вы не внесены в белый список.\n"
        f"Ваш Telegram ID: `{chat_id}`\n\n"
        f"Добавьте его в конфиг через запятую и нажмите на кнопку снизу:"
    )
    tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": format_message(text),
        "parse_mode": "MarkdownV2",
        "reply_markup": {
            "inline_keyboard": [[{"text": "Готово ✅", "callback_data": "check_whitelist"}]]
        },
    })


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
        params["show_alert"] = show_alert
    tg_call("answerCallbackQuery", params)


def start_login(chat_id, state):
    """Kick off an isolated `claude auth login` under this chat's own
    CLAUDE_CONFIG_DIR, using the same script+FIFO pty trick used for the
    original owner login. Runs in a background thread; the login URL is
    sent to the user as soon as it appears in the CLI's output."""
    config_dir = account_dir(chat_id)
    fifo_path = os.path.join(config_dir, "login_stdin.fifo")
    if os.path.exists(fifo_path):
        os.remove(fifo_path)
    os.mkfifo(fifo_path)

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    shell_cmd = f'exec script -qefc "{CLAUDE_BIN} auth login --claudeai" /dev/null 0<>{fifo_path}'
    proc = subprocess.Popen(
        ["bash", "-c", shell_cmd],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pending_logins[chat_id] = {"proc": proc, "fifo": fifo_path, "config_dir": config_dir}
    set_account_status(state, chat_id, "awaiting_code")

    def reader():
        deadline = time.time() + LOGIN_TIMEOUT_S
        try:
            for line in proc.stdout:
                m = re.search(r"https://\S+", line.strip())
                if m:
                    send_message(
                        chat_id,
                        "1. Нажми на кнопку ниже\n"
                        "2. Войди в свой аккаунт Claude\n"
                        "3. Пришли мне сюда код, который дадут после входа\n\n"
                        "Примечание: для входа нужна подписка Pro или выше.",
                    )
                    # A URL button instead of a raw pasted link -- keeps the
                    # giant OAuth URL out of the chat text entirely.
                    tg_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": "🔗 Войти в Claude",
                        "reply_markup": {
                            "inline_keyboard": [[{"text": "🔗 Войти в Claude", "url": m.group(0)}]]
                        },
                    })
                    break
                if time.time() > deadline:
                    break
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()


def feed_login_code(chat_id, code, state):
    info = pending_logins.get(chat_id)
    if not info:
        return False
    try:
        with open(info["fifo"], "w") as f:
            f.write(code.strip() + "\n")
    except Exception:
        send_message(chat_id, "Не смог передать код процессу логина. Попробуй /login заново.")
        return False

    def check():
        time.sleep(3)
        for _ in range(10):
            try:
                r = subprocess.run(
                    [CLAUDE_BIN, "auth", "status"],
                    env=claude_env(info["config_dir"]),
                    capture_output=True, text=True, timeout=15,
                )
                d = json.loads(r.stdout)
                if d.get("loggedIn"):
                    set_account_status(state, chat_id, "ready")
                    pending_logins.pop(chat_id, None)
                    send_message(chat_id, "✅ Аккаунт подключён. Можно пользоваться ботом.")
                    return
            except Exception:
                pass
            time.sleep(2)
        send_message(chat_id, "Не удалось подтвердить вход. Проверь код и попробуй /login ещё раз.")

    threading.Thread(target=check, daemon=True).start()
    return True


def handle_onboarding(chat_id, user_id, text, state, whitelist):
    """Returns True if this update was fully handled here (whitelist prompt /
    login kickoff / code consumption) and the main loop should move on.
    Returns False if the account is ready and normal dispatch should proceed."""
    if str(user_id) not in whitelist:
        send_whitelist_prompt(chat_id)
        return True

    status = get_account_status(state, chat_id)
    if status == "ready":
        return False

    if status == "awaiting_code":
        if text and not text.startswith(("/", ".")):
            feed_login_code(chat_id, text.strip(), state)
        else:
            send_message(chat_id, "Жду код авторизации (пришли его текстом, без команд).")
        return True

    start_login(chat_id, state)
    send_message(chat_id, "Ты в списке — начинаю подключение твоего аккаунта Claude...")
    return True


def handle_callback_query(cq, state):
    data = cq.get("data")
    from_id = cq.get("from", {}).get("id")
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    if not chat_id or data != "check_whitelist":
        answer_callback_query(cq["id"])
        return

    whitelist = load_whitelist()
    if str(from_id) not in whitelist:
        answer_callback_query(cq["id"], "Ещё не добавлен в список.", show_alert=True)
        return

    answer_callback_query(cq["id"], "Принято!")
    status = get_account_status(state, chat_id)
    if status == "ready":
        send_message(chat_id, "Аккаунт уже подключён.")
    elif status != "awaiting_code":
        start_login(chat_id, state)
        send_message(chat_id, "Ты в списке — начинаю подключение твоего аккаунта Claude...")


def main():
    state = load_state()
    offset = 0
    print("Claude Telegram bridge starting...", flush=True)
    register_commands()

    while True:
        try:
            r = tg_call(
                "getUpdates",
                {
                    "offset": offset, "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=35,
            )
        except Exception as e:
            print(f"getUpdates error: {e}", flush=True)
            time.sleep(3)
            continue

        if not r.get("ok"):
            print(f"getUpdates not ok: {r}", flush=True)
            time.sleep(3)
            continue

        for update in r.get("result", []):
            offset = update["update_id"] + 1

            cq = update.get("callback_query")
            if cq:
                try:
                    handle_callback_query(cq, state)
                except Exception:
                    print(traceback.format_exc()[-1500:], flush=True)
                continue

            msg = update.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            user_id = msg.get("from", {}).get("id")
            text = msg.get("text", "")
            photo = msg.get("photo")
            document = msg.get("document")
            caption = msg.get("caption", "")
            rich_message = msg.get("rich_message")

            if rich_message and not text:
                try:
                    text = rich_message_to_markdown(rich_message)
                except Exception:
                    print(traceback.format_exc()[-1500:], flush=True)

            if not text and not photo and not document:
                continue

            whitelist = load_whitelist()
            if handle_onboarding(chat_id, user_id, text, state, whitelist):
                continue

            try:
                cmd = text.strip().lower().lstrip("/.").split()[0] if text.strip() else ""

                if cmd == "stop" and text.startswith(("/", ".")):
                    proc = active_procs.get(chat_id)
                    if proc and proc.poll() is None:
                        proc.terminate()
                        send_message(chat_id, "⏹ Прерываю текущий запрос...")
                    else:
                        send_message(chat_id, "Сейчас ничего не выполняется.")
                    continue

                if cmd == "approve" and text.startswith(("/", ".")):
                    pending = get_pending_prompt(state, chat_id)
                    if not pending:
                        send_message(chat_id, "Нет заблокированного действия для approve.")
                        continue
                    arg = text.partition(" ")[2].strip().lower()
                    if arg == "session":
                        set_permission_mode(state, chat_id, "bypass")
                        send_message(chat_id, "Bypass включён для этой сессии насовсем. Повторяю...")
                        clear_pending_prompt(state, chat_id)
                        spawn_turn(chat_id, pending, state)
                    else:
                        send_message(chat_id, "Разрешаю один раз. Повторяю...")
                        clear_pending_prompt(state, chat_id)
                        spawn_turn(chat_id, pending, state, force_permission_mode="bypass")
                    continue

                if cmd == "deny" and text.startswith(("/", ".")):
                    if get_pending_prompt(state, chat_id):
                        clear_pending_prompt(state, chat_id)
                        send_message(chat_id, "Отклонено.")
                    else:
                        send_message(chat_id, "Нечего отклонять.")
                    continue

                if not photo and not document and text.startswith(("/", ".")):
                    if handle_command(chat_id, text, state):
                        continue

                attachment_note = ""
                if photo:
                    largest = photo[-1]
                    local_path = download_telegram_file(chat_id, largest["file_id"])
                    if local_path:
                        attachment_note += f"\n\n[Прикреплено изображение: {local_path}]"
                    else:
                        send_message(chat_id, "Не удалось скачать изображение.")
                if document:
                    local_path = download_telegram_file(
                        chat_id, document["file_id"], filename_hint=document.get("file_name")
                    )
                    if local_path:
                        attachment_note += f"\n\n[Прикреплён файл: {local_path}]"
                    else:
                        send_message(chat_id, "Не удалось скачать файл.")

                prompt = ((text or caption or "").strip() + attachment_note).strip()
                if not prompt:
                    continue

                queue_prompt(chat_id, prompt, state)
            except Exception:
                err = traceback.format_exc()[-1500:]
                print(err, flush=True)
                send_message(chat_id, f"Ошибка моста:\n```\n{err}\n```")


if __name__ == "__main__":
    main()
