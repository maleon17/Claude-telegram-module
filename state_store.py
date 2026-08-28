import glob
import json
import os
import subprocess
import time

from runtime import (
    CLAUDE_BIN, COST_WARNING_USD, OWNER_ID, PROJECTS_DIR, RESTART_SIGNAL_FILE,
    STATE_FILE, WORKDIR, claude_env, state_lock,
)
from telegram_api import send_message

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
        "cost_baseline_usd": 0.0,  # see reset_cost_warning_baseline()
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
    prev_cost = new_cost = baseline = None
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        sessions = entry.setdefault("sessions", {})
        usage = sessions.setdefault(session_id, _empty_usage())
        usage.setdefault("by_model", {})
        usage.setdefault("cost_baseline_usd", 0.0)

        usage["calls"] += 1
        # total_cost_usd on a "result" event is the CUMULATIVE cost of the
        # whole resumed session/process, not a per-turn delta -- confirmed
        # empirically 2026-08-22 by watching it across several turns on one
        # persistent stream-json process (it only ever grows by the new
        # turn's own cost, never resets). Summing it turn over turn -- the
        # old behavior, correct back when bridge.py spawned a genuinely
        # fresh `claude -p` per message -- massively overcounts once a
        # session runs many turns under the 2026-08-19 persistent-process
        # model (roughly an N-term arithmetic-series overcount for an
        # N-turn session). Assign, don't accumulate.
        prev_cost = usage["cost_usd"]
        baseline = usage["cost_baseline_usd"]
        new_cost = result_event.get("total_cost_usd")
        if new_cost is not None:
            usage["cost_usd"] = new_cost
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

        # Like total_cost_usd above, modelUsage[*].costUSD/inputTokens/
        # outputTokens are cumulative-for-the-session snapshots, not
        # per-turn deltas -- summing them via += across turns caused the
        # same N-term overcount (e.g. ~$200 shown for a model on a session
        # that actually cost ~$19 total). Assign the latest snapshot
        # instead of accumulating.
        model_usage = result_event.get("modelUsage")
        if model_usage:
            usage["by_model"] = {
                model_name: {
                    "cost_usd": mu.get("costUSD") or 0.0,
                    "input_tokens": mu.get("inputTokens") or 0,
                    "output_tokens": mu.get("outputTokens") or 0,
                }
                for model_name, mu in model_usage.items()
            }

        save_state(state)

    # cost_usd is cumulative for the session (see above), so comparing it
    # against a fixed baseline (0.0 until the first /compact, then reset
    # to whatever cost_usd was at that point -- see
    # reset_cost_warning_baseline()) fires exactly once per $100 accrued
    # since the last compaction, without a separate "already warned" flag.
    if new_cost is not None and prev_cost - baseline < COST_WARNING_USD <= new_cost - baseline:
        send_message(
            chat_id,
            f"⚠️ Стоимость этой сессии по API-эквиваленту выросла ещё на "
            f"${COST_WARNING_USD:.0f} (всего ~${new_cost:.2f}). "
            f"Есть смысл сделать /compact или начать заново через /new.",
        )


def reset_cost_warning_baseline(state, chat_id, session_id):
    """Called after a successful /compact so the $100 warning in
    add_usage() can fire again for cost accrued from this point forward --
    without this, cost_usd (cumulative for the whole session) stays above
    the threshold forever after the first warning, so a session that keeps
    compacting and growing again would never get warned a second time."""
    if not session_id:
        return
    with state_lock:
        entry = state.setdefault(str(chat_id), {})
        sessions = entry.setdefault("sessions", {})
        usage = sessions.setdefault(session_id, _empty_usage())
        usage["cost_baseline_usd"] = usage.get("cost_usd", 0.0)
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


def set_pending_restart(state, chat_id, message_id):
    # Not per-chat -- this whole bridge instance is restarting, tracked
    # once at the top level so the fresh process (after systemctl restart
    # kills this one) knows which message to edit to "done" on startup.
    with state_lock:
        state["_pending_restart"] = {"chat_id": chat_id, "message_id": message_id}
        save_state(state)


def pop_pending_restart(state):
    with state_lock:
        info = state.pop("_pending_restart", None)
        if info:
            save_state(state)
        return info


def request_restart(chat_id):
    # A dedicated file, not a key in `state` -- the running process holds
    # `state` purely in memory after its own startup load, so an external
    # write to state.json (e.g. from a one-off script simulating this call)
    # would never be seen until the process itself happened to reload it,
    # which it never does. This file is freshly checked from disk every
    # loop iteration instead, so an external trigger actually works.
    with open(RESTART_SIGNAL_FILE, "w") as f:
        json.dump({"chat_id": chat_id}, f)


def pop_restart_request():
    if not os.path.exists(RESTART_SIGNAL_FILE):
        return None
    try:
        with open(RESTART_SIGNAL_FILE) as f:
            info = json.load(f)
    except Exception:
        info = None
    try:
        os.remove(RESTART_SIGNAL_FILE)
    except FileNotFoundError:
        pass
    return info


def fetch_account_limits(config_dir=None):
    """Shell out to Claude Code's own /usage slash command for real account-level
    5-hour/weekly rate limit info. Runs as a standalone call (no --resume) so it
    doesn't pollute any conversation's session transcript -- and with
    --no-session-persistence so it doesn't create a throwaway session file of
    its own either (found live 2026-08-18: every /usage call was leaving a
    permanent 6-line junk session behind, 58 of them had piled up in
    ~/.claude/projects/-home-mishin/ alone before this was caught).

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
                "--no-session-persistence",
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

