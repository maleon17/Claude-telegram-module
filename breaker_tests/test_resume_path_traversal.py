"""
BREAK: `/resume <arg>` in bridge.py does zero sanitization of `arg` before
handing it to glob.glob(), so ANY whitelisted chat (owner or non-owner
friend) can hijack an arbitrary Claude Code session file anywhere on disk
that the bridge process can read -- including another whitelisted user's
isolated per-account session directory, or (given the right relative/
absolute path) the owner's own default `~/.claude/projects/...` session,
by simply supplying an absolute path or a `../`-laden argument.

Where: ~/.claude-telegram-bridge/bridge.py, handle_command(), `cmd == "resume"`
branch (around line 1403):

    matches = glob.glob(os.path.join(pdir, f"{arg}*.jsonl"))
    ...
    sid = os.path.basename(matches[0])[:-6]
    set_session(state, chat_id, sid)

Two independent ways this breaks, both demonstrated below:
  1. `os.path.join(pdir, arg)` silently DISCARDS `pdir` entirely if `arg`
     is an absolute path (stdlib os.path.join semantics) -- so `/resume
     /any/absolute/path/` globs directly against the filesystem root, no
     traversal syntax needed at all.
  2. A relative `../../../...` arg walks pdir back out to any ancestor
     directory the process can read, same effect via classic traversal.

Once matched, `set_session()` makes THAT session id the attacker's own
chat's active session -- every subsequent message in the attacker's chat
is `--resume`d onto someone else's real Claude Code conversation (whatever
CLAUDE_CONFIG_DIR that session id lives under), reading/continuing their
history. This directly breaks the multi-tenant isolation the whole
`CLAUDE_CONFIG_DIR`-per-account feature exists for (see
BRIDGE_PROJECT_HANDOFF.md, "Multi-tenancy" section) -- `/resume` performs
no ownership check on the matched session at all.

This test imports the REAL, unmodified bridge.py and calls the REAL
handle_command() function directly. Only tg_call is monkeypatched (to avoid
any live network call to Telegram) and BRIDGE_STATE_FILE is pointed at a
throwaway temp file so nothing touches the real, live state.json.

Run: python3 test_resume_path_traversal.py

UPDATE (post-fix): `/resume` now resolves `arg` against `pdir` with
os.path.realpath() and requires the result to be an actual descendant of
`pdir` (os.path.commonpath) before globbing -- both the absolute-path and
the relative ../ variant below must now fail closed (no session hijack,
"не найдена" reply) instead of succeeding.
"""
import json
import os
import sys
import tempfile

# --- isolate from the real, live bridge instance before import ---------
_TMP = tempfile.mkdtemp(prefix="breaker_resume_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"  # fake, unrelated to the real OWNER_ID
os.environ["BRIDGE_STATE_FILE"] = os.path.join(_TMP, "state.json")

sys.path.insert(0, os.path.expanduser("~/.claude-telegram-bridge"))
import bridge  # noqa: E402  (must come after the env vars above)
import telegram_api  # noqa: E402
from runtime import account_dir  # noqa: E402
from state_store import get_session, get_workspace, projects_dir_for  # noqa: E402

_sent = []
bridge.tg_call = lambda method, params=None, timeout=20: (
    _sent.append((method, params)),
    {"ok": True, "result": {"message_id": 1}},
)[1]
telegram_api.tg_call = bridge.tg_call


def _make_victim_session(label):
    victim_dir = os.path.join(_TMP, label)
    os.makedirs(victim_dir, exist_ok=True)
    sid = f"{label}-1111-2222-3333-444444444444"
    with open(os.path.join(victim_dir, f"{sid}.jsonl"), "w") as f:
        f.write('{"type": "user", "content": "very private victim conversation"}\n')
    return victim_dir, sid


def test_absolute_path_hijack():
    """os.path.join(pdir, arg) drops pdir entirely when arg is absolute."""
    victim_dir, victim_sid = _make_victim_session("victim_absolute")
    attacker_chat_id = "222000333"
    state = {}

    handled = bridge.handle_command(attacker_chat_id, f"/resume {victim_dir}/", state)
    assert handled is True

    hijacked = get_session(state, attacker_chat_id)
    assert hijacked != victim_sid, f"STILL HIJACKED: expected block, got {hijacked!r}"
    assert hijacked is None, f"expected no session set, got {hijacked!r}"
    print("[1/2] absolute-path hijack blocked (no session set)")


def test_relative_traversal_hijack():
    """Classic ../../.. relative traversal out of the attacker's own
    isolated per-account projects dir into a sibling/ancestor directory."""
    victim_dir, victim_sid = _make_victim_session("victim_relative")
    attacker_chat_id = "222000444"
    state = {}

    # attacker's own isolated pdir: <ACCOUNTS_DIR>/<attacker_chat_id>/projects/-home-mishin
    # ACCOUNTS_DIR = dirname(STATE_FILE)/accounts = _TMP/accounts. In real
    # use this directory is created by Claude Code itself the first time
    # this chat_id runs any real turn (bridge.py's account_dir() only
    # pre-creates the outer accounts/<chat_id>/ level) -- pre-create it here
    # to model an attacker who has already sent at least one normal message,
    # which any real non-owner whitelisted user has by the time they'd
    # bother trying this.
    pdir = projects_dir_for(account_dir(attacker_chat_id), get_workspace(state, attacker_chat_id))
    os.makedirs(pdir, exist_ok=True)
    depth = len(os.path.relpath(pdir, "/").split(os.sep))
    rel_prefix = "../" * depth
    # Pure relative traversal: walk up to "/" then back down to victim_dir
    # via its own relative components -- no absolute-path shortcut involved
    # this time (that's test 1's mechanism; this test isolates the classic
    # "../../.." relative-traversal path instead).
    payload = f"{rel_prefix}{victim_dir.lstrip('/')}/"

    handled = bridge.handle_command(attacker_chat_id, f"/resume {payload}", state)
    assert handled is True

    hijacked = get_session(state, attacker_chat_id)
    assert hijacked != victim_sid, f"STILL HIJACKED: expected block, got {hijacked!r}"
    assert hijacked is None, f"expected no session set, got {hijacked!r}"
    print("[2/2] relative ../ traversal hijack blocked (no session set)")


def test_legitimate_prefix_still_works():
    """Functionality check: a normal same-directory prefix lookup (the
    actual intended UX -- short-id/prefix resume within one's own pdir)
    must still succeed after the fix."""
    attacker_chat_id = "222000555"
    state = {}
    pdir = projects_dir_for(account_dir(attacker_chat_id), get_workspace(state, attacker_chat_id))
    os.makedirs(pdir, exist_ok=True)
    sid = "abcd1234-1111-2222-3333-444444444444"
    with open(os.path.join(pdir, f"{sid}.jsonl"), "w") as f:
        f.write('{"type": "user", "content": "own legitimate session"}\n')

    handled = bridge.handle_command(attacker_chat_id, "/resume abcd1234", state)
    assert handled is True

    resumed = get_session(state, attacker_chat_id)
    assert resumed == sid, f"legitimate prefix resume broke: expected {sid!r}, got {resumed!r}"
    print("[3/3] legitimate same-directory prefix resume still works:", resumed)


if __name__ == "__main__":
    test_absolute_path_hijack()
    test_relative_traversal_hijack()
    test_legitimate_prefix_still_works()
    print(
        "\nCLOSED: /resume now rejects both the absolute-path and relative "
        "../ traversal hijack, while still resolving a legitimate same-"
        "directory session-id prefix."
    )
