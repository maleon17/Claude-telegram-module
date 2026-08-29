#!/usr/bin/env python3
"""Single-owner Telegram frontend for persistent Codex CLI conversations."""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


EDIT_THROTTLE_S = 1.3
MAX_MESSAGE_LEN = 4000
RICH_MAX_CHARS = 30000
HTTP_TIMEOUT_S = 20
IDLE_TIMEOUT_S = 300
TOTAL_TIMEOUT_S = 1800
PROCESS_WAIT_TIMEOUT_S = 5


def log(message):
    print(message, file=sys.stderr, flush=True)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"codex_bot.py: required environment variable {name} is not set")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
try:
    OWNER_ID = int(require_env("OWNER_ID"))
except ValueError as exc:
    raise SystemExit("codex_bot.py: OWNER_ID must be an integer") from exc

CODEX_CWD = os.environ.get("CODEX_CWD", "/home/mishin")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
STATE_FILE = Path(
    os.environ.get("CODEX_BOT_STATE_FILE", Path(__file__).with_name("codex_bot_state.json"))
).expanduser()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

state_lock = threading.Lock()
process_lock = threading.Lock()
telegram_lock = threading.Lock()
rate_limit_until = 0.0
current_process = None
busy = False


def _rate_limited(result):
    return result.get("error_code") == 429


def tg_call(method, params=None, timeout=HTTP_TIMEOUT_S):
    """Call Telegram once; never retry, or call at all during a known 429 ban."""
    global rate_limit_until
    with telegram_lock:
        now = time.monotonic()
        if now < rate_limit_until:
            remaining = max(1, int(rate_limit_until - now + 0.999))
            result = {
                "ok": False,
                "error_code": 429,
                "description": "locally suppressed during Telegram rate limit",
                "parameters": {"retry_after": remaining},
            }
            log(f"Telegram {method} suppressed: rate-limited for ~{remaining} more seconds")
            return result

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
            if _rate_limited(result):
                retry_after = (result.get("parameters") or {}).get("retry_after")
                try:
                    delay = max(1.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 1.0
                rate_limit_until = max(rate_limit_until, time.monotonic() + delay)
                log(
                    f"Telegram {method} not ok: 429 Too Many Requests; "
                    f"bot rate-limited for {retry_after} seconds: {result}"
                )
            else:
                log(f"Telegram {method} not ok: {result}")
        return result


def load_state():
    if not STATE_FILE.exists():
        return {"thread_id": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        thread_id = data.get("thread_id") if isinstance(data, dict) else None
        return {"thread_id": thread_id if isinstance(thread_id, str) else None}
    except Exception as exc:
        raise SystemExit(f"codex_bot.py: cannot read state file {STATE_FILE}: {exc}") from exc


state = load_state()


def save_thread_id(thread_id):
    with state_lock:
        state["thread_id"] = thread_id
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=STATE_FILE.name + ".", dir=STATE_FILE.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, STATE_FILE)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def compact(value, limit=1000):
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        return f"🧠 {compact(item.get('text', item.get('summary', item)))}"
    if item_type == "file_change":
        return f"📝 {compact(item.get('path', item.get('changes', item)))}"
    return f"• {item_type}: {compact(item)}"


def format_usage(usage):
    if not isinstance(usage, dict):
        return compact(usage, 300)
    parts = []
    for key, label in (("input_tokens", "in"), ("cached_input_tokens", "cached"),
                       ("output_tokens", "out"),
                       ("reasoning_output_tokens", "reasoning")):
        if key in usage:
            parts.append(f"{label}: {usage[key]}")
    return ", ".join(parts) if parts else compact(usage, 300)


def send_plain(chat_id, text):
    text = text or "(пусто)"
    last = None
    while text:
        part, text = text[:MAX_MESSAGE_LEN], text[MAX_MESSAGE_LEN:]
        last = tg_call("sendMessage", {"chat_id": chat_id, "text": part})
        if _rate_limited(last):
            break
    return last


def edit_plain(chat_id, message_id, text):
    return tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE_LEN]
    })


def send_rich(chat_id, markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    result = tg_call("sendRichMessage", {
        "chat_id": chat_id, "rich_message": {"markdown": text}
    })
    if not result.get("ok") and not _rate_limited(result):
        return send_plain(chat_id, markdown_text)
    return result


def edit_rich(chat_id, message_id, markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    result = tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "rich_message": {"markdown": text},
    })
    if not result.get("ok") and not _rate_limited(result):
        description = str(result.get("description", "")).lower()
        if "not modified" not in description:
            return edit_plain(chat_id, message_id, markdown_text)
    return result


class TurnView:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.items = []
        self.steps = []
        self.message_id = None
        self.last_edit_at = 0.0
        self.usage = None
        self.completed = False

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                save_thread_id(thread_id)
            self.steps.append(f"🧵 Поток запущен: {thread_id or 'без id'}")
        elif event_type == "turn.started":
            self.steps.append("▶️ Выполнение начато")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.items.append(item)
                self.steps.append(format_item(item))
            else:
                self.steps.append(f"• item.completed: {compact(item)}")
        elif event_type == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
        else:
            self.steps.append(f"• {event_type}: {compact(event)}")

    def live_text(self):
        header = "⏳ Codex работает..."
        available = MAX_MESSAGE_LEN - len(header) - 2
        for start in range(len(self.steps) + 1):
            visible = self.steps[start:]
            if start:
                visible = [f"…{start} шагов выше опущено"] + visible
            body = "\n".join(visible)
            if len(body) <= available:
                return header + (f"\n\n{body}" if body else "")
        return header

    def flush(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_edit_at < EDIT_THROTTLE_S:
            return
        self.last_edit_at = now
        if self.message_id is None:
            result = send_plain(self.chat_id, self.live_text())
            if result and result.get("ok"):
                self.message_id = result["result"]["message_id"]
        else:
            edit_plain(self.chat_id, self.message_id, self.live_text())

    def deliver(self, stopped=False, error=None):
        final_index = next(
            (i for i in range(len(self.items) - 1, -1, -1)
             if self.items[i].get("type") == "agent_message"), None
        )
        final_text = self.items[final_index].get("text", "") if final_index is not None else ""

        process_steps = list(self.steps)
        if final_index is not None:
            final_rendered = format_item(self.items[final_index])
            for i in range(len(process_steps) - 1, -1, -1):
                if process_steps[i] == final_rendered:
                    del process_steps[i]
                    break

        if stopped:
            answer = "⏹ Выполнение остановлено."
        elif error:
            answer = f"⚠️ Ошибка Codex: {error}"
        else:
            answer = final_text or "(нет ответа — смотри процесс выше)"
        if self.usage is not None:
            answer += f"\n\nТокены: {format_usage(self.usage)}"

        if process_steps:
            closing_reserve = 100
            visible = []
            used = 0
            for step in reversed(process_steps):
                cost = len(step) + 1
                if visible and used + cost > RICH_MAX_CHARS - closing_reserve:
                    break
                visible.append(step[: RICH_MAX_CHARS - closing_reserve])
                used += cost
            visible.reverse()
            hidden = len(process_steps) - len(visible)
            if hidden:
                visible.insert(0, f"…и ещё {hidden} шагов выше…")
            body = "\n".join(visible)
            rich = f"<details><summary>🔧 Процесс ({len(process_steps)})</summary>\n{body}\n</details>"
            if self.message_id is not None:
                edit_rich(self.chat_id, self.message_id, rich)
            else:
                send_rich(self.chat_id, rich)
            send_rich(self.chat_id, answer)
        elif self.message_id is not None:
            edit_rich(self.chat_id, self.message_id, answer)
        else:
            send_rich(self.chat_id, answer)


def build_codex_command(prompt, thread_id):
    base = ["codex", "exec", "--json", "--sandbox", CODEX_SANDBOX,
            "--skip-git-repo-check", "--cd", CODEX_CWD]
    return base + (["resume", thread_id, prompt] if thread_id else [prompt])


def run_turn(chat_id, prompt, thread_id):
    global busy, current_process
    view = TurnView(chat_id)
    stderr_lines = []
    return_code = None
    try:
        command = build_codex_command(prompt, thread_id)
        log(f"Starting Codex thread={thread_id or 'new'} cwd={CODEX_CWD}")
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, start_new_session=True,
        )
        with process_lock:
            current_process = proc

        def drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]

        stdout_queue = queue.Queue()

        def read_stdout():
            try:
                for line in proc.stdout:
                    stdout_queue.put(line)
            finally:
                stdout_queue.put(None)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()
        view.flush(force=True)
        started_at = time.monotonic()
        last_output_at = started_at
        line_number = 0
        timeout_error = None
        while True:
            now = time.monotonic()
            idle_remaining = IDLE_TIMEOUT_S - (now - last_output_at)
            total_remaining = TOTAL_TIMEOUT_S - (now - started_at)
            if idle_remaining <= 0:
                timeout_error = (
                    f"Codex завис и был принудительно остановлен "
                    f"(таймаут {IDLE_TIMEOUT_S / 60:g} минут)"
                )
            elif total_remaining <= 0:
                timeout_error = (
                    f"Codex завис и был принудительно остановлен "
                    f"(таймаут {TOTAL_TIMEOUT_S / 60:g} минут)"
                )
            if timeout_error:
                log(timeout_error)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    return_code = proc.wait(timeout=PROCESS_WAIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    log(f"Codex process {proc.pid} did not exit after SIGKILL")
                    return_code = -signal.SIGKILL
                break

            try:
                raw_line = stdout_queue.get(
                    timeout=min(1.0, idle_remaining, total_remaining)
                )
            except queue.Empty:
                if proc.poll() is not None:
                    # The direct child can exit while a descendant still holds
                    # the inherited stdout pipe open. Reap the remaining group.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return_code = proc.returncode
                    break
                continue
            if raw_line is None:
                if proc.poll() is not None:
                    return_code = proc.returncode
                    break
                continue
            last_output_at = time.monotonic()
            line_number += 1
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
                if not isinstance(event, dict):
                    raise ValueError("top-level JSON value is not an object")
                view.add_event(event)
                view.flush(force=event.get("type") == "turn.completed")
            except Exception as exc:
                log(f"JSONL parse error on line {line_number}: {exc}; line skipped")
        if return_code is None:
            return_code = proc.wait()
        stderr_thread.join(timeout=1)
        stderr_text = "".join(stderr_lines).strip()
        if stderr_text:
            log(f"Codex stderr: {stderr_text[-4000:]}")
        stopped = return_code < 0 and timeout_error is None
        error = None
        if timeout_error:
            error = timeout_error
        elif return_code != 0 and not stopped:
            error = compact(stderr_text or f"codex exited with status {return_code}", 1000)
        elif not view.completed and not stopped:
            error = "поток Codex завершился без события turn.completed"
        view.deliver(stopped=stopped, error=error)
    except Exception as exc:
        log(f"Codex worker failed: {exc}")
        try:
            view.deliver(error=compact(str(exc), 1000))
        except Exception as delivery_exc:
            log(f"Could not report worker error: {delivery_exc}")
    finally:
        with process_lock:
            current_process = None
            busy = False


def handle_command(chat_id, command):
    global busy
    command = command.split()[0].split("@", 1)[0].lower()
    if command == "/new":
        save_thread_id(None)
        send_plain(chat_id, "🆕 Текущий Codex-тред сброшен. Следующее сообщение начнёт новый.")
        return True
    if command == "/stop":
        with process_lock:
            proc = current_process
            running = busy and proc is not None and proc.poll() is None
            if running:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    running = False
        if running:
            send_plain(chat_id, "⏹ Останавливаю текущее выполнение Codex.")
        else:
            send_plain(chat_id, "Сейчас нечего останавливать.")
        return True
    return command.startswith("/")


def handle_message(message):
    global busy
    chat_id = message.get("chat", {}).get("id")
    if chat_id != OWNER_ID:
        return
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()
    if text.startswith("/"):
        handle_command(chat_id, text)
        return
    with process_lock:
        if busy:
            already_busy = True
        else:
            busy = True
            already_busy = False
    if already_busy:
        send_plain(chat_id, "Codex ещё работает над предыдущим запросом, подожди или используй /stop")
        return
    with state_lock:
        thread_id = state.get("thread_id")
    threading.Thread(target=run_turn, args=(chat_id, text, thread_id), daemon=True).start()


def register_commands():
    tg_call("setMyCommands", {"commands": [
        {"command": "new", "description": "Сбросить текущий Codex-тред"},
        {"command": "stop", "description": "Остановить текущее выполнение"},
    ]})


def main():
    offset = None
    register_commands()
    log(f"Codex Telegram bot started; owner={OWNER_ID}, cwd={CODEX_CWD}")
    while True:
        params = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = tg_call("getUpdates", params, timeout=40)
        if not result.get("ok"):
            time.sleep(1)
            continue
        for update in result.get("result", []):
            try:
                offset = max(offset or 0, update["update_id"] + 1)
                message = update.get("message")
                if isinstance(message, dict):
                    handle_message(message)
            except Exception as exc:
                log(f"Unexpected update handler error: {exc}")
                chat_id = (update.get("message") or {}).get("chat", {}).get("id")
                if chat_id == OWNER_ID:
                    send_plain(chat_id, "⚠️ Ошибка моста. Подробности записаны в лог.")


if __name__ == "__main__":
    main()
