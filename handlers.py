import glob
import json
import os
import re
import subprocess
import threading
import time
import traceback

from runtime import (
    BATCH_DEBOUNCE_S, CLAUDE_BIN, OWNER_ID, SERVICE_NAME, WORKDIR, account_dir, batch_timers,
    busy_chats, claude_env, load_whitelist, pending_batches, pending_logins,
)
from state_store import (
    clear_session, fetch_account_limits, get_account_status, get_model,
    get_permission_mode, get_session, get_usage, get_workspace, list_sessions,
    projects_dir_for, request_restart, session_message_count, set_account_status,
    set_model, set_permission_mode, set_session, set_workspace,
)
from chat_process import _stop_chat_process, send_turn_to_chat_process
from telegram_api import send_message, send_typing, tg_call
from telegram_format import format_message

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
    ("compact", "Сжать контекст текущей сессии (экономит токены/деньги)"),
    ("usage", "Токены, стоимость и лимиты аккаунта"),
    ("model", "Модель: /model opus 4.7, /model sonnet, /model default"),
    ("mode", "Режим подтверждений: bypass/default/acceptEdits/plan"),
    ("workspace", "Рабочая директория для этой сессии"),
    ("approve", "Разрешить заблокированное действие (once/session)"),
    ("deny", "Отклонить заблокированное действие"),
    ("login", "Переподключить свой аккаунт Claude"),
    ("restart", "Перезапустить бота (только для владельца)"),
]


def handle_command(chat_id, text, state, offset=None):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().strip().lstrip("/.")
    arg = arg.strip()

    if cmd == "start":
        return True

    if cmd == "new":
        # Session id isn't part of a chat process's restart signature (see
        # _ensure_chat_process) -- has to be torn down explicitly here, or
        # the next message would land on the OLD live process/session
        # instead of picking up the cleared one. If a turn happens to be
        # active right now, this doubles as an implicit /stop -- treated
        # as reasonable given the user explicitly asked to start fresh.
        _stop_chat_process(chat_id)
        clear_session(state, chat_id)
        send_message(chat_id, "Начинаю новую сессию.")
        return True

    if cmd == "compact":
        # Real CLI slash command, sent as an ordinary prompt onto the
        # chat's persistent process -- see _chat_reader_loop's
        # "system"/"status" handling for how the result gets reported.
        spawn_turn(chat_id, "/compact", state)
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
        # arg is untrusted (whitelisted-chat-controlled), and os.path.join
        # silently discards pdir entirely if arg is absolute -- glob would
        # then search anywhere the process can read. Resolve both to real
        # paths and require the match to actually be a descendant of pdir
        # before globbing, closing both the absolute-path and the ../
        # traversal variant.
        real_pdir = os.path.realpath(pdir)
        candidate = os.path.realpath(os.path.join(real_pdir, arg))
        if os.path.commonpath([real_pdir, candidate]) != real_pdir:
            send_message(chat_id, f"Сессия {arg} не найдена.")
            return True
        matches = glob.glob(f"{candidate}*.jsonl")
        matches = [
            m for m in matches
            if os.path.commonpath([real_pdir, os.path.realpath(m)]) == real_pdir
        ]
        if not matches:
            send_message(chat_id, f"Сессия {arg} не найдена.")
            return True
        sid = os.path.basename(matches[0])[:-6]
        _stop_chat_process(chat_id)  # see /new -- same reason
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

    if cmd == "restart":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, "Перезапуск доступен только владельцу.")
            return True
        if not SERVICE_NAME:
            send_message(chat_id, "SERVICE_NAME не задан в systemd-юните — автоперезапуск недоступен.")
            return True
        # Don't restart immediately -- if a turn (possibly this very one) is
        # still in flight, killing the process now would cut it off mid-
        # answer. Just record the request; main()'s loop performs the
        # actual restart once busy_chats is empty, so it always happens
        # between turns, never in the middle of one.
        request_restart(chat_id)
        if busy_chats:
            send_message(
                chat_id,
                "🔁 Перезапуск запланирован — выполнится, как только текущие запросы завершатся.",
            )
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
    """dispatch_turn() now only ensures the chat's persistent process and
    writes the prompt to its stdin -- it returns almost immediately, long
    before the turn is actually done. So busy_chats is only cleared HERE
    on a failure that happens before/during that write (nothing will ever
    reach the reader thread to clear it in that case); on success,
    clearing it is _chat_reader_loop's job once it sees this turn's
    "result" event (or the process dying mid-turn)."""
    try:
        dispatch_turn(chat_id, prompt, state, force_permission_mode=force_permission_mode)
    except Exception:
        err = traceback.format_exc()[-1500:]
        print(err, flush=True)
        send_message(chat_id, f"Ошибка моста:\n```\n{err}\n```")
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
def queue_prompt(chat_id, prompt, state):
    if chat_id in busy_chats:
        # A live process for this chat is already mid-turn -- Claude Code's
        # own stream-json protocol natively accepts another user-message
        # event on the SAME open stdin and merges it into the ongoing turn
        # (see send_turn_to_chat_process's docstring -- it only ever WRITES,
        # it never asserts the process has to be idle first). This is
        # exactly how the owner's own mid-turn messages reached Claude all
        # through the 2026-08-28 session, surfaced to the model as a
        # mid-turn injection rather than a fresh turn -- no /stop needed, no
        # reason to bounce. Skips the batch debounce below on purpose: that
        # debounce exists to avoid spawning N separate FRESH turns for rapid
        # typing while idle, which doesn't apply here -- there's already
        # exactly one open turn to append to, so each message goes straight
        # through as its own distinct injection instead of getting merged
        # into one blob.
        dispatch_turn(chat_id, prompt, state)
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
    """Write one prompt onto chat_id's persistent process. Non-blocking --
    see send_turn_to_chat_process's docstring. Delivery (final answer,
    attachments, denial handling via /approve|/approve session|/deny) all
    happens later, asynchronously, in _chat_reader_loop / _deliver_turn_result."""
    send_typing(chat_id)

    model = get_model(state, chat_id)
    permission_mode = force_permission_mode or get_permission_mode(state, chat_id)
    workspace = get_workspace(state, chat_id)
    config_dir = account_dir(chat_id)

    send_turn_to_chat_process(chat_id, prompt, state, model, permission_mode, workspace, config_dir)


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

