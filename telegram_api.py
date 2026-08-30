import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from runtime import (
    API_BASE, FILE_API_BASE, FILE_PATH_RE, IMAGE_EXTS, MAX_DOCUMENT_BYTES,
    MAX_MSG_LEN, MAX_PHOTO_BYTES, RICH_MAX_CHARS, UPLOADS_DIR,
)
from telegram_format import format_message, strip_mdv2

def _tg_rate_limited(response):
    return response.get("error_code") == 429


def tg_call(method, params=None, timeout=20):
    url = f"{API_BASE}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read().decode())
        except Exception:
            result = {"ok": False, "error": str(e)}
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    if not result.get("ok"):
        if _tg_rate_limited(result):
            retry_after = (result.get("parameters") or {}).get("retry_after")
            print(
                f"Telegram {method} not ok: 429 Too Many Requests; "
                f"bot rate-limited for {retry_after} seconds: {result}",
                flush=True,
            )
        else:
            print(f"Telegram {method} not ok: {result}", flush=True)
    return result


def send_message(chat_id, text, reply_to=None):
    formatted = format_message(text)
    if len(formatted) > MAX_MSG_LEN:
        return send_message_chunked(chat_id, text, reply_to)
    params = {"chat_id": chat_id, "text": formatted, "parse_mode": "MarkdownV2"}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    r = tg_call("sendMessage", params)
    if not r.get("ok") and not _tg_rate_limited(r):
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
        if not r.get("ok") and not _tg_rate_limited(r):
            params["text"] = strip_mdv2(part)
            params.pop("parse_mode", None)
            r = tg_call("sendMessage", params)
        if _tg_rate_limited(r):
            return r
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
        if _tg_rate_limited(r):
            return r
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


_whisper_model = None
_whisper_lock = threading.Lock()


def transcribe_voice(path):
    """Local speech-to-text via faster-whisper. Lazy-loads the model on first
    use so bridge.py startup and non-voice messages pay no extra cost."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _info = _whisper_model.transcribe(path, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()


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
        if _tg_rate_limited(r):
            return r
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
    # A transient edit rate-limit must stay on this same message. Returning
    # the first 429 made callers fall back to sendMessage, leaving the old
    # live Thinking card alongside a duplicate final answer.
    if _tg_rate_limited(r):
        retry_after = (r.get("parameters") or {}).get("retry_after")
        try:
            time.sleep(min(60.0, max(1.0, float(retry_after))))
        except (TypeError, ValueError):
            time.sleep(1.0)
        r = tg_call("editMessageText", params)
    if not r.get("ok"):
        if _tg_rate_limited(r):
            return r
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
