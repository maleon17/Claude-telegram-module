import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

from runtime import (
    CHAT_PROC_IDLE_TIMEOUT_S, CLAUDE_BIN, EDIT_THROTTLE_S, STATE_FILE,
    THINKING_SPINNER_FRAMES, WORKDIR, busy_chats, chat_procs,
    chat_procs_lock, claude_env,
)
from state_store import (
    add_usage, clear_pending_prompt, get_pending_delegator, get_session,
    reset_cost_warning_baseline, set_pending_delegator, set_pending_prompt, set_session,
)
from telegram_api import (
    edit_rich, extract_existing_files, send_attachment, send_message, send_rich,
    tg_call,
)
from telegram_format import escape_mdv2, fenced_code, mdv2_fenced_code, strip_mdv2

def tool_call_line(name, tool_input):
    if name == "Bash":
        cmd = tool_input.get("command", "")[:500]
        return f"🔧 Bash:\n{fenced_code(cmd)}"
    if name in ("Read",):
        return f"📖 Read: `{tool_input.get('file_path', '')}`"
    if name in ("Write", "Edit"):
        return f"✏️ {name}: `{tool_input.get('file_path', '')}`"
    if name in ("WebSearch",):
        return f"🔍 WebSearch: `{tool_input.get('query', '')[:150]}`"
    if name in ("WebFetch",):
        return f"🌐 WebFetch: `{tool_input.get('url', '')}`"
    keys_preview = json.dumps(tool_input, ensure_ascii=False)[:300]
    return f"🔧 {name}:\n{fenced_code(keys_preview)}"


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
    return f"🔧 {name}", json.dumps(tool_input, ensure_ascii=False)


def _draft_clean(s, limit=200):
    s = strip_mdv2(s)
    s = s.replace("```", "").replace("`", "").replace("\\", "")
    return re.sub(r"\s+", " ", s).strip()[:limit]


def write_last_turn(chat_id, text):
    """Signal a completed turn's final text via a plain file instead of
    Telegram's getUpdates -- this process already owns that bot token's
    getUpdates stream exclusively (only one consumer can ever see a given
    update), so an external script polling the same API would just starve.
    bridge_exec.py polls this file instead."""
    path = os.path.join(os.path.dirname(STATE_FILE), f"last_turn_{chat_id}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"text": text, "ts": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _format_turn_footer(state, chat_id, ts):
    """Токены on every turn; a delegation-only note on a turn that
    actually came through bridge_exec.py (not a per-message thing, per
    the owner directly), symmetric to codex-telegram-bot's identical fix
    in bot.py's run_turn finalize. prior_session_id is the OWNER's own
    session from before this delegated call touched anything (empty
    string "" if there wasn't one) -- so they can return to their own
    conversation, separate from `session_id` below, which is whatever
    this delegated task itself ended up using."""
    parts = []
    usage = ts.get("last_usage")
    if usage:
        token_parts = []
        for key, label in (("input_tokens", "in"), ("cache_read_input_tokens", "cached"),
                            ("output_tokens", "out")):
            if key in usage:
                token_parts.append(f"{label}: {usage[key]}")
        if token_parts:
            parts.append(f"Токены: {', '.join(token_parts)}")
    session_id = ts.get("current_session_id")
    prior_session_id = get_pending_delegator(state, chat_id)
    if prior_session_id is not None and session_id:
        if prior_session_id:
            parts.append(
                f"Твой session id (до делегации): `{prior_session_id[:8]}`. "
                f"Продолжить делегированную: `/resume {session_id[:8]}`"
            )
        else:
            parts.append(f"Продолжить делегированную сессию: `/resume {session_id[:8]}`")
        set_pending_delegator(state, chat_id, None)
    return "\n\n".join(parts)


def _new_turn_accumulator(state, chat_id):
    """Fresh per-turn accumulator for _chat_reader_loop. One of these is
    live at a time per chat process; reset right after each delivered
    "result" event so state from one turn never bleeds into the next."""
    return {
        "log_lines": [],
        "current_session_id": get_session(state, chat_id),
        "last_draft_edit": 0.0,
        "final_text": None,
        "last_usage": None,
        "last_text_log_index": None,
        "last_text_log_raw": None,
        "denials": [],
        "written_files": [],
        "compact_outcome": None,
        "compact_done_event": None,
        # Draft rendering state, two levels:
        # - draft_thought: the current "thinking"/text block, plain (no
        #   code formatting). Stays as an anchor across however many tool
        #   calls happen under it.
        # - draft_cmd / draft_res: the current tool call + its result,
        #   each their own code block, overwriting the previous pair the
        #   same way as before -- but scoped UNDER the current thought.
        "draft_thought": None,
        "draft_cmd_label": None,
        "draft_cmd": None,
        "draft_res_blocks": [],  # list of (label, content)
        # message_id of the live progress message this turn (see
        # _flush_draft) -- None until the first flush sends it.
        "progress_msg_id": None,
        "progress_attempted": False,
        # Braille-spinner frame index (see THINKING_SPINNER_FRAMES) --
        # advances on every actual send/edit, purely cosmetic "still alive"
        # signal now that we no longer get Telegram's own native draft
        # animation for free.
        "spinner_i": 0,
        "progress_lock": threading.Lock(),
    }


def _flush_draft(chat_id, ts, force=False):
    # Show a static 🤔 prefix, but keep the useful tool/result snapshot.  The
    # old spinner edited the message continuously; updates now happen only
    # when real stream data arrives and are globally throttled per turn.
    lines = []
    if ts["draft_thought"]:
        lines.append(escape_mdv2(ts["draft_thought"]))
    if ts["draft_cmd"]:
        lines.append(escape_mdv2(f"{ts['draft_cmd_label']}:"))
        lines.append(mdv2_fenced_code(ts['draft_cmd']))
        for label, content in ts["draft_res_blocks"]:
            lines.append(escape_mdv2(f"{label}:"))
            lines.append(mdv2_fenced_code(content))
    body = "\n".join(lines) if lines else "Думаю"
    text = f"🤔 {body}"

    with ts["progress_lock"]:
        now = time.time()
        if (
            ts["progress_msg_id"] is not None
            and now - ts["last_draft_edit"] < EDIT_THROTTLE_S
        ):
            return
        if ts["progress_msg_id"] is None:
            if ts["progress_attempted"]:
                return
            ts["progress_attempted"] = True
            r = tg_call(
                "sendMessage",
                {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            )
            if not r.get("ok") and r.get("error_code") != 429:
                r = tg_call("sendMessage", {"chat_id": chat_id, "text": text})
            if r.get("ok"):
                ts["progress_msg_id"] = r["result"]["message_id"]
        else:
            params = {
                "chat_id": chat_id, "message_id": ts["progress_msg_id"],
                "text": text, "parse_mode": "MarkdownV2",
            }
            r = tg_call("editMessageText", params)
            if not r.get("ok"):
                desc = str(r.get("description", "")).lower()
                if "not modified" not in desc and r.get("error_code") != 429:
                    params["text"] = strip_mdv2(text).replace("```", "")
                    params.pop("parse_mode", None)
                    tg_call("editMessageText", params)
        ts["last_draft_edit"] = now


def _compact_draft_watchdog(chat_id, ts):
    """Refreshes the "🗜 Сжимаю контекст..." progress message every 15s
    with an updated elapsed-time counter for as long as a real compaction
    is running (a real persisted message -- see _flush_draft -- doesn't
    expire on its own the way the old sendMessageDraft did, so this is now
    a live counter rather than something keeping the message from vanishing).
    Exits as soon as ts["compact_done_event"] is set (compact_result
    arrived) or, as a belt-and-suspenders cap, after 20 minutes."""
    start = time.time()
    done = ts["compact_done_event"]
    while not done.wait(timeout=15):
        elapsed = int(time.time() - start)
        ts["draft_thought"] = f"🗜 Сжимаю контекст сессии... ({elapsed}с, это может занять несколько минут)"
        _flush_draft(chat_id, ts, force=True)
        if elapsed > 1200:
            return


def _deliver_turn_result(chat_id, state, ts, prompt, proc, stopped=False):
    """Everything that used to happen after run_claude() returned, plus
    dispatch_turn()'s own tail (attachments, denial handling) -- merged
    here because in the persistent-process model EVERY turn, whether we
    explicitly wrote it to stdin or it's a spontaneous reply to a
    background task finishing on its own, is delivered from
    _chat_reader_loop, not from whoever originally called dispatch_turn."""
    log_lines = ts["log_lines"]
    final_text = ts["final_text"]

    # The last assistant text block is what becomes the final answer
    # (echoed in the "result" event) -- drop it from the process log so
    # it isn't duplicated between the collapsible process block and the
    # answer.
    if (
        ts["last_text_log_index"] is not None
        and final_text is not None
        and ts["last_text_log_raw"] is not None
        and ts["last_text_log_raw"].strip() == final_text.strip()
        and 0 <= ts["last_text_log_index"] < len(log_lines)
    ):
        del log_lines[ts["last_text_log_index"]]

    process_rich_text = None
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
        process_rich_text = (
            f"<details><summary>🔧 Процесс ({len(log_lines)})</summary>\n"
            f"{body}\n</details>"
        )

    if ts["current_session_id"]:
        with chat_procs_lock:
            is_current_proc = chat_procs.get(chat_id, {}).get("proc") is proc
        # A /new or /resume may already have detached this process while
        # its reader was draining the old stdout. That stale result must
        # not restore the session the command just replaced.
        if is_current_proc:
            set_session(state, chat_id, ts["current_session_id"])

    if stopped:
        # This path fires whenever the persistent process ends with a turn
        # still pending -- an explicit /stop, but also /new, /resume, a
        # /model|/mode|/workspace change, a /restart, or a genuine crash of
        # the underlying `claude` process (confirmed live 2026-08-24: a
        # long-running /compact on a large session can end this way with
        # no exception or stderr output anywhere). "по /stop" would lie in
        # every case except the first, so keep the wording cause-agnostic.
        text_out = "⏹ Ход прерван — процесс завершился, не дождавшись ответа."
    elif ts.get("compact_outcome"):
        text_out = ts["compact_outcome"]
    else:
        text_out = final_text if final_text is not None else "(нет ответа — смотри процесс выше)"

    progress_became_process_block = False
    if process_rich_text:
        if ts["progress_msg_id"] is not None:
            # Turn the live-progress message itself INTO the collapsible
            # process-log block, in place, instead of deleting it and
            # sending a separate new message -- one fewer message in the
            # chat, and the transition reads as "this IS what it was
            # building the whole time" rather than a swap.
            edit_rich(chat_id, ts["progress_msg_id"], process_rich_text)
            progress_became_process_block = True
        else:
            send_rich(chat_id, process_rich_text)

    final_payload = text_out or "(пусто)"
    if not stopped:
        footer = _format_turn_footer(state, chat_id, ts)
        if footer:
            final_payload = f"{final_payload}\n\n{footer}"
        write_last_turn(chat_id, final_payload)
    if ts["progress_msg_id"] is not None and not progress_became_process_block:
        # Either there was no tool-call content at all (a plain
        # conversational turn), or there was but it already got absorbed
        # into the process block above -- either way, the live-progress
        # message is still sitting there unused. Turn IT into the final
        # answer, in place, instead of deleting it and sending a fresh
        # message (caught live 2026-08-28: that delete+resend was visibly
        # flickering -- the answer would flash as the progress message,
        # vanish, then reappear as a "new" one a beat later).
        edit_rich(chat_id, ts["progress_msg_id"], final_payload)
    else:
        # A genuinely NEW message here is deliberate, not incidental: an
        # edit does not push a Telegram notification, a fresh sendMessage
        # does. Reverted 2026-08-30 -- a since-uncommitted change merged
        # the process block and the answer into one edited card to save a
        # message, which silently killed the "your answer is ready"
        # notification for every tool-using turn (the user only ever got
        # pinged by the initial "🤔 Думаю" placeholder, then nothing).
        send_rich(chat_id, final_payload)

    if not stopped:
        attachments = list(ts["written_files"])
        for p in extract_existing_files(text_out or ""):
            if p not in attachments:
                attachments.append(p)
        for path in attachments:
            if os.path.isfile(path):
                send_attachment(chat_id, path)

    if ts["denials"] and prompt:
        set_pending_prompt(state, chat_id, prompt)
        lines = ["🚫 **Заблокировано** (нужно разрешение):"]
        for d in ts["denials"][:10]:
            lines.append(f"`{d.get('tool_name', '?')}`  {json.dumps(d.get('tool_input', {}), ensure_ascii=False)[:150]}")
        lines.append("")
        lines.append("/approve — повторить один раз с bypass")
        lines.append("/approve session — включить bypass насовсем для этой сессии")
        lines.append("/deny — оставить как есть")
        send_message(chat_id, "\n".join(lines))
    else:
        clear_pending_prompt(state, chat_id)


def _spinner_ticker_loop(chat_id, record, proc):
    """Keeps the live-progress message's spinner frame (see
    THINKING_SPINNER_FRAMES/_flush_draft) visibly ticking at a fixed ~1.5s
    cadence for as long as this chat is busy. _flush_draft on its own only
    fires on a real stream event (new thought/tool call/result) -- without
    this, a long gap between events (waiting on a slow tool, a backgrounded
    task, etc.) would leave the spinner frozen on one frame, looking dead
    even though work is genuinely still happening. Tied to this specific
    process's lifetime, same idea as _chat_proc_idle_reaper_loop -- exits
    as soon as this process is no longer the live one for chat_id (respawned,
    stopped, or the chat itself is idle) rather than running forever."""
    while True:
        time.sleep(1.5)
        with chat_procs_lock:
            if chat_procs.get(chat_id, {}).get("proc") is not proc:
                return
        if proc.poll() is not None:
            return
        if chat_id not in busy_chats:
            continue
        ts = record.get("ts")
        if ts is not None and ts.get("progress_msg_id") is not None:
            try:
                _flush_draft(chat_id, ts, force=True)
            except Exception:
                pass


def _chat_reader_loop(chat_id, state, record):
    """Runs for the whole lifetime of one chat's persistent `claude`
    process -- started once by _start_chat_process, not per-turn. Parses
    every stdout line for as long as the process lives, across however
    many turns arrive on its stdin over time. A "result" event marks a
    turn's end regardless of whether it was explicitly requested (a
    prompt we just wrote to stdin) or spontaneous (a backgrounded Bash/
    Agent task finishing on its own -- confirmed live 2026-08-18 that
    this arrives on the same open stdout with zero new stdin input
    needed, which is what makes the old WAKEUP_SIGNAL_DIR workaround
    obsolete for chats running under this model)."""
    proc = record["proc"]
    ts = _new_turn_accumulator(state, chat_id)
    record["ts"] = ts  # see _spinner_ticker_loop -- needs the CURRENT ts

    try:
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                d = json.loads(raw_line)
            except Exception:
                # Not silently discarded on purpose -- a non-JSON line on
                # stdout is often the CLI's own plain-text error (e.g. "No
                # conversation found with session ID: ...") right before it
                # exits. Swallowing it here (as the old spawn-per-message
                # code did too) makes a real crash indistinguishable from
                # normal stream noise -- caught live 2026-08-19 debugging
                # exactly that.
                print(f"chat process ({chat_id}) non-JSON stdout: {raw_line[:500]!r}", flush=True)
                continue
            record["last_activity"] = time.time()
            # Any real stdout activity means the process is doing SOMETHING
            # right now, whether this is an explicit turn spawn_turn() already
            # marked busy, or a spontaneous continuation (a backgrounded Bash/
            # Agent task finishing on its own, per the "result" comment below)
            # that spawn_turn() was never called for -- busy_chats had no
            # code path re-adding a chat in that second case, so /stop (and
            # /status) reported "nothing running" while a spontaneous turn
            # was visibly still producing tool calls/text in Telegram (caught
            # live 2026-08-28, three consecutive false "Сейчас ничего не
            # выполняется" during exactly that window). Idempotent add here,
            # single source of truth for "clear" stays the "result" event
            # below -- this only widens WHEN busy gets set, not when it's
            # cleared.
            busy_chats.add(chat_id)

            t = d.get("type")

            if t == "system" and d.get("subtype") == "init":
                ts["current_session_id"] = d.get("session_id") or ts["current_session_id"]
                continue

            if t == "system" and d.get("subtype") == "status":
                # /compact (sent like any other prompt, see handle_command)
                # is a real CLI slash command, not text the model sees --
                # confirmed live 2026-08-22. It reports through this status
                # channel instead of an assistant text block, so the normal
                # "result" event for that turn carries an empty final_text
                # -- surface something useful instead of "(пусто)".
                if d.get("status") == "compacting":
                    ts["draft_thought"] = "🗜 Сжимаю контекст сессии..."
                    ts["draft_cmd_label"] = None
                    ts["draft_cmd"] = None
                    ts["draft_res_blocks"] = []
                    _flush_draft(chat_id, ts, force=True)
                    # Real compaction on a large session takes MINUTES
                    # (measured ~5 min on a ~400K-token session, 2026-08-24)
                    # with zero intermediate stream events -- nothing else
                    # would touch the progress message again before
                    # compact_result finally arrives, so without a periodic
                    # re-flush the elapsed-time counter in it would just sit
                    # frozen for minutes, looking like a hang even though
                    # the message itself (a real, persisted one -- see
                    # _flush_draft) stays visible fine on its own.
                    ts["compact_done_event"] = threading.Event()
                    threading.Thread(
                        target=_compact_draft_watchdog,
                        args=(chat_id, ts),
                        daemon=True,
                    ).start()
                elif "compact_result" in d:
                    if ts["compact_done_event"]:
                        ts["compact_done_event"].set()
                    if d.get("compact_result") == "failed":
                        err = d.get("compact_error") or "неизвестная ошибка"
                        ts["compact_outcome"] = f"🗜 Не удалось сжать контекст: {err}"
                    else:
                        ts["compact_outcome"] = "🗜 Контекст сессии сжат."
                        reset_cost_warning_baseline(state, chat_id, ts["current_session_id"])
                continue

            if t == "stream_event":
                ev = d.get("event", {})
                if ev.get("type") == "content_block_delta":
                    _flush_draft(chat_id, ts)
                continue

            if t == "assistant":
                content = d.get("message", {}).get("content", [])
                # CLI stream-json content is normally a list of typed blocks,
                # but can be a plain string (seen after /compact) -- nothing
                # to extract from that case, so treat it as empty.
                if isinstance(content, str):
                    content = []
                for block in content:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        ts["log_lines"].append(tool_call_line(name, tool_input))
                        label, content_ = draft_tool_label_and_content(name, tool_input)
                        ts["draft_cmd_label"] = label
                        ts["draft_cmd"] = _draft_clean(content_)
                        ts["draft_res_blocks"] = []
                        _flush_draft(chat_id, ts, force=True)
                        if name == "Write":
                            fp = tool_input.get("file_path")
                            if fp and not os.path.abspath(os.path.expanduser(fp)).startswith(
                                os.path.expanduser("~/.claude/") + os.sep
                            ):
                                ts["written_files"].append(fp)
                    elif block.get("type") in ("text", "thinking"):
                        text = block.get("text") or block.get("thinking") or ""
                        if text.strip():
                            if block.get("type") == "text":
                                ts["log_lines"].append(f"💬 {text}")
                                ts["last_text_log_index"] = len(ts["log_lines"]) - 1
                                ts["last_text_log_raw"] = text
                            ts["draft_thought"] = _draft_clean(text, limit=300)
                            ts["draft_cmd_label"] = None
                            ts["draft_cmd"] = None
                            ts["draft_res_blocks"] = []
                            _flush_draft(chat_id, ts, force=True)
                _flush_draft(chat_id, ts)
                continue

            if t == "user":
                content = d.get("message", {}).get("content", [])
                # see the "assistant" branch above for why this guard exists
                if isinstance(content, str):
                    content = []
                for block in content:
                    if block.get("type") == "tool_result":
                        is_error = block.get("is_error", False)
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
                                log_parts.append(f"✅ StdOut:\n{fenced_code(preview)}")
                                res_blocks.append(("✅ StdOut", _draft_clean(preview)))
                            if stderr_text:
                                preview = stderr_text[:400]
                                log_parts.append(f"❌ StdErr:\n{fenced_code(preview)}")
                                res_blocks.append(("❌ StdErr", _draft_clean(preview)))
                            if not log_parts:
                                icon = "❌" if is_error else "✅"
                                log_parts.append(f"{icon} (пусто)")
                            ts["log_lines"].append("\n".join(log_parts))
                            ts["draft_res_blocks"] = res_blocks
                        else:
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = " ".join(
                                    c.get("text", "") for c in result_content if isinstance(c, dict)
                                )
                            result_content = str(result_content)

                            exit_match = (
                                re.match(r"^Exit code (\d+)\n?(.*)$", result_content, re.DOTALL)
                                if is_error else None
                            )
                            log_parts = []
                            res_blocks = []
                            if exit_match:
                                exit_code, output = exit_match.group(1), exit_match.group(2).strip()
                                if output:
                                    preview = output[:400]
                                    log_parts.append(f"✅ StdOut:\n{fenced_code(preview)}")
                                    res_blocks.append(("✅ StdOut", _draft_clean(preview)))
                                log_parts.append(f"❌ StdErr:\n{fenced_code(f'Exit code {exit_code}')}")
                                res_blocks.append(("❌ StdErr", f"Exit code {exit_code}"))
                            else:
                                icon = "❌" if is_error else "✅"
                                label = f"{icon} {'Ошибка' if is_error else 'Результат'}"
                                preview = result_content.strip()[:400]
                                log_parts.append(f"{label}:\n{fenced_code(preview)}")
                                res_blocks.append((label, _draft_clean(preview)))
                            ts["log_lines"].append("\n".join(log_parts))
                            ts["draft_res_blocks"] = res_blocks
                        _flush_draft(chat_id, ts, force=True)
                continue

            if t == "result":
                ts["current_session_id"] = d.get("session_id") or ts["current_session_id"]
                ts["final_text"] = d.get("result", "")
                ts["last_usage"] = d.get("usage") or {}
                ts["denials"].extend(d.get("permission_denials") or [])
                add_usage(state, chat_id, ts["current_session_id"], d)
                with record["write_lock"]:
                    prompt = record["original_prompt"]
                    record["original_prompt"] = None
                try:
                    _deliver_turn_result(chat_id, state, ts, prompt, proc, stopped=False)
                except Exception:
                    print(traceback.format_exc()[-1500:], flush=True)
                with chat_procs_lock:
                    if chat_procs.get(chat_id, {}).get("proc") is proc:
                        busy_chats.discard(chat_id)
                ts = _new_turn_accumulator(state, chat_id)
                record["ts"] = ts
                continue
    except Exception:
        print(f"chat_reader error ({chat_id}):\n" + traceback.format_exc()[-1500:], flush=True)
    finally:
        with record["write_lock"]:
            original_prompt = record["original_prompt"]
            record["original_prompt"] = None
        if original_prompt is not None:
            # Process ended (killed via /stop, torn down for a settings
            # change, or crashed) while a turn was genuinely in flight --
            # delivers the same "⏹ Остановлено" UX the old spawn-per-
            # message model gave on /stop. If there was no pending turn
            # (e.g. the idle reaper stopped a genuinely idle process),
            # there's nothing to report.
            try:
                _deliver_turn_result(chat_id, state, ts, original_prompt, proc, stopped=True)
            except Exception:
                print(traceback.format_exc()[-1500:], flush=True)
        with chat_procs_lock:
            if chat_procs.get(chat_id, {}).get("proc") is proc:
                chat_procs.pop(chat_id, None)
                busy_chats.discard(chat_id)
        if proc.poll() is None:
            proc.kill()


def _chat_process_signature(model, permission_mode, workspace, config_dir):
    return (model, permission_mode, workspace, config_dir)


def _start_chat_process(chat_id, model, permission_mode, workspace, config_dir, session_id, state):
    args = [
        CLAUDE_BIN,
        "-p",
        "--input-format=stream-json",
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
        env=claude_env(config_dir, chat_id),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    record = {
        "proc": proc,
        "signature": _chat_process_signature(model, permission_mode, workspace, config_dir),
        "last_activity": time.time(),
        "original_prompt": None,
        "write_lock": threading.Lock(),
    }
    with chat_procs_lock:
        chat_procs[chat_id] = record
    threading.Thread(target=_chat_reader_loop, args=(chat_id, state, record), daemon=True).start()
    return record


def _stop_chat_process(chat_id):
    """Tears down chat_id's persistent process, if any. Safe to call any
    time, including when there's no live process (no-op) or mid-turn
    (the reader thread's own finally-block delivers the "⏹ Остановлено"
    message once it notices the stdout stream ended)."""
    with chat_procs_lock:
        record = chat_procs.pop(chat_id, None)
        if record:
            busy_chats.discard(chat_id)
    if not record:
        return
    proc = record["proc"]
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _ensure_chat_process(chat_id, model, permission_mode, workspace, config_dir, state):
    """Returns a live process record for chat_id, starting or restarting
    one if needed. A restart is needed if there's no process yet, the
    previous one died, or /model, /mode, or /workspace changed since it
    was started (those are CLI flags, fixed for a process's lifetime --
    changing them means killing and respawning with --resume onto the
    same session so only the flags change, not the conversation)."""
    wanted_sig = _chat_process_signature(model, permission_mode, workspace, config_dir)
    with chat_procs_lock:
        record = chat_procs.get(chat_id)
    if record and record["proc"].poll() is None and record["signature"] == wanted_sig:
        return record
    if record:
        _stop_chat_process(chat_id)
    session_id = get_session(state, chat_id)
    return _start_chat_process(chat_id, model, permission_mode, workspace, config_dir, session_id, state)


def send_turn_to_chat_process(
    chat_id, prompt, state, model=None, permission_mode=None, workspace=None, config_dir=None,
):
    """Non-blocking: ensures chat_id's persistent process is up (spawning
    or respawning it if needed) and writes `prompt` onto its stdin as one
    stream-json user-message event. Delivery of the eventual reply --
    progress draft, final answer, attachments, denial handling -- all
    happens asynchronously in that chat's _chat_reader_loop thread, not
    here; this returns as soon as the write itself succeeds.

    permission_mode: None (or "bypass") -> --dangerously-skip-permissions
    (current default, unchanged). Any other value -> --permission-mode <mode>,
    which can produce real permission_denials in the result event.
    """
    record = _ensure_chat_process(chat_id, model, permission_mode, workspace, config_dir, state)
    msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
    with record["write_lock"]:
        # Mid-turn user messages are injected into the live turn too, but
        # denial/retry and interrupted-turn UX must stay tied to the prompt
        # that STARTED that turn, not whichever injection happened last.
        if record["original_prompt"] is None:
            record["original_prompt"] = prompt
        record["last_activity"] = time.time()
        record["proc"].stdin.write(json.dumps(msg) + "\n")
        record["proc"].stdin.flush()


def _chat_proc_idle_reaper_loop():
    """Reaps a chat's persistent process after CHAT_PROC_IDLE_TIMEOUT_S of
    inactivity -- hygiene against unbounded long-idle MCP connections/fds,
    not a cost-saving measure (RAM is not the constraint on this host, see
    chat_procs comment). Only ever touches a chat that's currently NOT
    mid-turn, so it can't race a real turn -- if a message arrives right
    after a chat gets reaped, that chat's next _ensure_chat_process call
    just respawns it, same as after any other restart."""
    while True:
        time.sleep(300)
        now = time.time()
        with chat_procs_lock:
            stale = [
                cid for cid, rec in chat_procs.items()
                if cid not in busy_chats
                and now - rec.get("last_activity", now) > CHAT_PROC_IDLE_TIMEOUT_S
            ]
        for cid in stale:
            _stop_chat_process(cid)


def _shutdown_chat_processes(*_args):
    """SIGTERM handler (systemd sends this on `systemctl restart`). Plain
    subprocess children do NOT get signalled automatically when their
    parent dies -- without this they'd become orphaned but keep running,
    leaking `claude` processes across every restart and potentially
    fighting a freshly started bridge.py over the same --resume session
    id. Best-effort and bounded (_stop_chat_process itself caps at a 5s
    wait per process before kill -9) so a restart can't hang forever on
    a stuck child."""
    with chat_procs_lock:
        chat_ids = list(chat_procs.keys())
    for cid in chat_ids:
        _stop_chat_process(cid)
    sys.exit(0)
