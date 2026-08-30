#!/usr/bin/env python3
"""Relay ``codex exec --json`` JSONL progress from stdin to Telegram."""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from telegram_format import escape_mdv2, fenced_code, mdv2_fenced_code, strip_mdv2


EDIT_THROTTLE_S = 1.3
MAX_MESSAGE_LEN = 4000  # Leave headroom below Telegram's 4096-char limit.
RICH_MAX_CHARS = 30000
HTTP_TIMEOUT_S = 20
THINKING_SPINNER_FRAMES = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"


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


def _rate_limited(result):
    return result.get("error_code") == 429


def item_label_and_blocks(item):
    item_type = item.get("type", "unknown")
    if item_type == "agent_message":
        return "💬 Ответ", item.get("text", ""), []
    if item_type == "reasoning":
        return "🧠 Размышление", item.get("text", item.get("summary", "")), []
    if item_type == "command_execution":
        command = item.get("command", "")
        exit_code = item.get("exit_code")
        output = item.get("aggregated_output")
        results = []
        if output not in (None, ""):
            results.append(("📤 Результат", str(output)))
        if exit_code is not None:
            try:
                succeeded = int(exit_code) == 0
            except (TypeError, ValueError):
                succeeded = False
            results.append(("✅ Код завершения" if succeeded else "❌ Код завершения", str(exit_code)))
        return "🔧 Bash", command, results
    if item_type == "file_change":
        content = item.get("path", item.get("changes", item))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return "📝 Изменение файла", content, []
    return "🔧 Инструмент", str(item_type).replace("_", " "), []


def render_process_item(item):
    label, content, results = item_label_and_blocks(item)
    lines = [f"{label}:", fenced_code(content)]
    for result_label, result_content in results:
        lines.extend((f"{result_label}:", fenced_code(result_content)))
    return "\n".join(lines)


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


def send_plain(text):
    return tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text[:MAX_MESSAGE_LEN]})


def edit_plain(message_id, text):
    return tg_call("editMessageText", {
        "chat_id": CHAT_ID, "message_id": message_id, "text": text[:MAX_MESSAGE_LEN],
    })


def send_rich(markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    result = tg_call("sendRichMessage", {
        "chat_id": CHAT_ID, "rich_message": {"markdown": text},
    })
    if not result.get("ok") and not _rate_limited(result):
        return send_plain(markdown_text)
    return result


def edit_rich(message_id, markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    params = {
        "chat_id": CHAT_ID, "message_id": message_id,
        "rich_message": {"markdown": text},
    }
    result = tg_call("editMessageText", params)
    # Retry a transient edit limit on the same message before deliver()
    # considers creating a fallback message; otherwise the old Thinking
    # card and the final answer appear as two messages.
    if _rate_limited(result):
        retry_after = (result.get("parameters") or {}).get("retry_after")
        try:
            time.sleep(min(60.0, max(1.0, float(retry_after))))
        except (TypeError, ValueError):
            time.sleep(1.0)
        result = tg_call("editMessageText", params)
    if not result.get("ok") and not _rate_limited(result):
        description = str(result.get("description", "")).lower()
        if "not modified" not in description:
            return edit_plain(message_id, markdown_text)
    return result


class Relay:
    def __init__(self):
        self.items = []
        self.process_items = []
        self.draft_thought = None
        self.draft_tool = None
        self.message_id = None
        self.last_request_at = 0.0
        self.finished = False
        self.usage = None
        self.spinner_i = 0

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            pass
        elif event_type == "turn.started":
            pass
        elif event_type in ("item.started", "item.updated"):
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    text = item.get("text", item.get("summary", ""))
                    if text:
                        self.draft_thought = text
                else:
                    self.draft_tool = item
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.items.append(item)
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self.draft_thought = item.get("text", item.get("summary", ""))
                else:
                    self.draft_tool = item
                self.process_items.append(item)
        elif event_type == "turn.completed":
            self.finished = True
            self.usage = event.get("usage")

    def render(self):
        # Keep the thinking marker stable.  Editing a Braille spinner on
        # every flush only burns Telegram's edit budget and can turn a
        # transient rate limit into the duplicate-message path this relay
        # is explicitly meant to avoid.
        lines = []
        if self.draft_thought:
            lines.append(escape_mdv2(f"🤔 {self.draft_thought}"))
        if self.draft_tool:
            label, content, results = item_label_and_blocks(self.draft_tool)
            lines.append(escape_mdv2(f"{label}:"))
            if content:
                lines.append(mdv2_fenced_code(content))
            for result_label, result_content in results:
                lines.extend((escape_mdv2(f"{result_label}:"),
                              mdv2_fenced_code(result_content)))
        if lines:
            return "\n".join(lines)
        return "🤔 Думаю"

    def _send_or_edit_live(self, text):
        if self.message_id is None:
            result = tg_call("sendMessage", {
                "chat_id": CHAT_ID, "text": text, "parse_mode": "MarkdownV2",
            })
            if not result.get("ok") and not _rate_limited(result):
                result = tg_call("sendMessage", {
                    "chat_id": CHAT_ID,
                    "text": strip_mdv2(text).replace("```", ""),
                })
            if result.get("ok"):
                self.message_id = result["result"]["message_id"]
            return
        params = {
            "chat_id": CHAT_ID, "message_id": self.message_id,
            "text": text, "parse_mode": "MarkdownV2",
        }
        result = tg_call("editMessageText", params)
        if not result.get("ok") and not _rate_limited(result):
            description = str(result.get("description", "")).lower()
            if "not modified" not in description:
                params["text"] = strip_mdv2(text).replace("```", "")
                params.pop("parse_mode", None)
                tg_call("editMessageText", params)

    def flush(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_request_at < EDIT_THROTTLE_S:
            return
        self.last_request_at = now
        try:
            self._send_or_edit_live(self.render()[:MAX_MESSAGE_LEN])
        except Exception as exc:
            # Telegram must never stop consumption of the producer's pipe.
            log(f"Telegram relay error: {exc}")

    def deliver(self):
        final_index = next(
            (i for i in range(len(self.items) - 1, -1, -1)
             if self.items[i].get("type") == "agent_message"), None
        )
        final_text = self.items[final_index].get("text", "") if final_index is not None else ""
        process_items = list(self.process_items)
        if final_index is not None:
            final_item = self.items[final_index]
            for i in range(len(process_items) - 1, -1, -1):
                if process_items[i] is final_item:
                    del process_items[i]
                    break

        answer = final_text or "(нет ответа — смотри процесс выше)"
        if self.usage is not None:
            answer += f"\n\nТокены: {format_usage(self.usage)}"

        try:
            if process_items:
                process_steps = [render_process_item(item) for item in process_items]
                closing_reserve = 100
                visible = []
                used = 0
                for step in reversed(process_steps):
                    cost = len(step) + 1
                    if visible and used + cost > RICH_MAX_CHARS - closing_reserve:
                        break
                    visible.append(step[:RICH_MAX_CHARS - closing_reserve])
                    used += cost
                visible.reverse()
                hidden = len(process_steps) - len(visible)
                if hidden:
                    visible.insert(0, f"…и ещё {hidden} шагов выше…")
                body = "\n".join(visible)
                rich = (
                    f"<details><summary>🔧 Процесс ({len(process_steps)})</summary>\n"
                    f"{body}\n</details>"
                )
                if self.message_id is not None:
                    edit_rich(self.message_id, rich)
                else:
                    send_rich(rich)
                # A genuinely NEW message here is deliberate: an edit doesn't
                # push a Telegram notification, a fresh send does. Reverted
                # 2026-08-30 -- merging the process block and answer into one
                # edited card (same regression fixed on the bridge/codex-bot
                # side) silently killed the "delegated task is done"
                # notification for every run with tool calls.
                send_rich(answer)
            elif self.message_id is not None:
                result = edit_rich(self.message_id, answer)
                if not result or not result.get("ok"):
                    send_rich(answer)
            else:
                send_rich(answer)
        except Exception as exc:
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
        if event.get("type") == "turn.completed":
            relay.deliver()
        else:
            relay.flush()


if __name__ == "__main__":
    main()
