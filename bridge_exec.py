#!/usr/bin/env python3
"""Drive the live Claude bridge ("Claudій Jarvisович") without going
through Telegram at all -- symmetric to codex-telegram-bot's own copy of
this script.

A bot can never see its own outgoing messages via getUpdates -- Telegram
simply does not deliver them back to the sender, confirmed live
2026-09-01, not something fixable at the code level (and no other identity
can inject into a private 1:1 chat either -- it only ever has its two real
participants). Since bridge.py is our own code, the real fix is to skip
Telegram for this leg entirely: this script writes a request file that
bridge.py's _external_request_watcher_loop() polls and feeds straight into
queue_prompt() -- the exact same entry point a real incoming Telegram
message reaches. Real formatting, real /resume, real mid-turn steering
(the owner typing into the same chat while this runs still gets genuinely
steered in) all come from the actual product for free -- only the INPUT
side bypasses Telegram now.

Usage:
    bridge_exec.py [--workspace PATH] [--resume ID] [--chat-id ID]
                    [--timeout SECONDS] PROMPT...

Set DELEGATOR_SESSION_ID (or CLAUDE_CODE_SESSION_ID, in case Claude ever
calls this instead of Codex) to whatever identifies YOUR own session, if
you want the "Session id: X / resume Y" footer -- it only appears on a
delegated turn, never on an ordinary one (X is your session, Y is the one
this bridge just used, to /resume this specific conversation later).

Prints the final answer to stdout and exits 0, or prints an error to
stderr and exits 1 on timeout/failure.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OWNER_ID = 8480261623


def external_request_path():
    return os.environ.get(
        "BRIDGE_EXTERNAL_REQUEST_FILE", os.path.join(ROOT, "external_request.json"),
    )


def last_turn_path(chat_id):
    # Mirrors chat_process.py's write_last_turn():
    # os.path.join(os.path.dirname(STATE_FILE), f"last_turn_{chat_id}.json").
    state_file = os.environ.get(
        "BRIDGE_STATE_FILE", os.path.join(ROOT, "state.json"),
    )
    return os.path.join(os.path.dirname(state_file), f"last_turn_{chat_id}.json")


def poll_until_done(chat_id, baseline_ts, timeout_s, poll_interval=2):
    """Poll the last-turn signal FILE chat_process.py writes on every
    completed turn, not Telegram's getUpdates -- bridge.py already owns
    that bot token's getUpdates stream exclusively (only one consumer ever
    sees a given update), so a second independent poller there would just
    starve forever. See chat_process.py's write_last_turn()."""
    path = last_turn_path(chat_id)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = None
        if data and data.get("ts", 0) > baseline_ts:
            return data["text"]
        time.sleep(poll_interval)
    raise TimeoutError(f'No completed turn signalled via {path} within {timeout_s}s.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="switch workspace before the prompt")
    parser.add_argument("--resume", help="resume this session id before the prompt")
    parser.add_argument("--chat-id", type=int, help="override the target chat (default: OWNER_ID)")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("prompt", nargs="+")
    args = parser.parse_args()

    chat_id = args.chat_id or int(os.environ.get("OWNER_ID") or DEFAULT_OWNER_ID)

    request_path = external_request_path()
    if os.path.exists(request_path):
        print(f"{request_path} already has an unconsumed request -- "
              f"bridge.py hasn't picked it up yet, or it's stuck. Not overwriting.",
              file=sys.stderr)
        sys.exit(1)

    # Baseline BEFORE writing the request -- only a last_turn file written
    # strictly after this counts as ours, not a stale prior turn's.
    try:
        with open(last_turn_path(chat_id), encoding="utf-8") as f:
            baseline_ts = json.load(f).get("ts", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        baseline_ts = 0

    request = {"chat_id": chat_id, "text": " ".join(args.prompt)}
    if args.workspace:
        request["workspace"] = args.workspace
    if args.resume:
        request["resume_session_id"] = args.resume
    delegator_session_id = os.environ.get("DELEGATOR_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if delegator_session_id:
        request["delegator_session_id"] = delegator_session_id

    tmp = request_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(request, f)
    os.replace(tmp, request_path)

    try:
        final_text = poll_until_done(chat_id, baseline_ts, args.timeout)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    print(final_text)


if __name__ == "__main__":
    main()
