#!/usr/bin/env python3
"""Relay ``codex exec --json`` JSONL progress from stdin to Telegram."""

import json
import os
import sys
import time
import urllib.error
import urllib.request


EDIT_THROTTLE_S = 1.3
MAX_MESSAGE_LEN = 4000  # Leave headroom below Telegram's 4096-char limit.
HTTP_TIMEOUT_S = 20


def log(message):
    print(message, file=sys.stderr, flush=True)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"codex_relay.py: required environment variable {name} is not set")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID = require_env("CHAT_ID")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_call(method, params=None, timeout=HTTP_TIMEOUT_S):
    """Call Telegram once. In particular, never retry a 429 response here."""
    request = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    if not result.get("ok"):
        if result.get("error_code") == 429:
            retry_after = (result.get("parameters") or {}).get("retry_after")
            log(
                f"Telegram {method} not ok: 429 Too Many Requests; "
                f"bot rate-limited for {retry_after} seconds: {result}"
            )
        else:
            log(f"Telegram {method} not ok: {result}")
    return result


def compact(value, limit=1000):
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def format_item(item):
    item_type = item.get("type", "unknown")
    if item_type == "agent_message":
        return f"💬 {compact(item.get('text', ''))}"
    if item_type == "command_execution":
        command = compact(item.get("command", ""), 700)
        exit_code = item.get("exit_code")
        status = "" if exit_code is None else f" (exit {exit_code})"
        output = item.get("aggregated_output")
        text = f"⚙️ {command}{status}"
        if output not in (None, ""):
            text += f"\n{compact(output, 1000)}"
        return text
    if item_type == "reasoning":
        detail = item.get("text", item.get("summary", item))
        return f"🧠 {compact(detail)}"
    if item_type == "file_change":
        detail = item.get("path", item.get("changes", item))
        return f"📝 {compact(detail)}"

    # Item types are intentionally open-ended. Preserve all available data.
    return f"• {item_type}: {compact(item)}"


def format_usage(usage):
    if not isinstance(usage, dict):
        return compact(usage, 300)
    parts = []
    for key, label in (
        ("input_tokens", "in"),
        ("cached_input_tokens", "cached"),
        ("output_tokens", "out"),
        ("reasoning_output_tokens", "reasoning"),
    ):
        if key in usage:
            parts.append(f"{label}: {usage[key]}")
    return ", ".join(parts) if parts else compact(usage, 300)


class Relay:
    def __init__(self):
        self.steps = []
        self.message_id = None
        self.last_request_at = 0.0
        self.finished = False
        self.usage = None

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            self.steps.append(f"🧵 Поток запущен: {thread_id or 'без id'}")
        elif event_type == "turn.started":
            self.steps.append("▶️ Выполнение начато")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.steps.append(format_item(item))
            else:
                self.steps.append(f"• item.completed: {compact(item)}")
        elif event_type == "turn.completed":
            self.finished = True
            self.usage = event.get("usage")
        else:
            self.steps.append(f"• {event_type}: {compact(event)}")

    def render(self):
        header = "✅ Codex завершён" if self.finished else "⏳ Codex работает — печатает…"
        footer = ""
        if self.finished and self.usage is not None:
            footer = f"\n\nТокены: {format_usage(self.usage)}"

        available = MAX_MESSAGE_LEN - len(header) - len(footer) - 2
        visible = []
        if not self.steps:
            return header + footer
        for start in range(len(self.steps)):
            candidate = self.steps[start:]
            hidden = start
            lines = ([f"…{hidden} шагов выше опущено"] if hidden else []) + candidate
            if len("\n".join(lines)) <= available:
                visible = lines
                break
        else:
            # Even the newest single step is too large: retain its freshest tail.
            hidden = max(0, len(self.steps) - 1)
            marker = f"…{hidden} шагов выше опущено" if hidden else ""
            room = available - len(marker) - (1 if marker else 0)
            tail = "…" + self.steps[-1][-(room - 1):] if room > 1 else ""
            visible = ([marker] if marker else []) + ([tail] if tail else [])

        body = "\n".join(visible)
        return header + (f"\n\n{body}" if body else "") + footer

    def flush(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_request_at < EDIT_THROTTLE_S:
            return
        self.last_request_at = now
        text = self.render()
        try:
            if self.message_id is None:
                result = tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text})
                if result.get("ok"):
                    self.message_id = result["result"]["message_id"]
            else:
                tg_call(
                    "editMessageText",
                    {"chat_id": CHAT_ID, "message_id": self.message_id, "text": text},
                )
        except Exception as exc:
            # Telegram must never stop consumption of the producer's pipe.
            log(f"Telegram relay error: {exc}")


def main():
    relay = Relay()
    for line_number, raw_line in enumerate(sys.stdin, 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
            if not isinstance(event, dict):
                raise ValueError("top-level JSON value is not an object")
            relay.add_event(event)
        except Exception as exc:
            log(f"JSONL parse error on line {line_number}: {exc}; line skipped")
            continue
        relay.flush(force=event.get("type") == "turn.completed")


if __name__ == "__main__":
    main()
