"""
BREAK: register_trigger's `chat` targeting bypasses ANY authorization check
and ANY "existing dialog" restriction for numeric chat ids.

Where: ~/.claude-telegram-bridge/jarvis-ask/claude_ask.py
  - ClaudeAsk._resolve_any_chat_target()  (line ~1330)
  - ClaudeAsk._register_trigger_action()  (line ~1474)
exposed as the `register_trigger` MCP tool in mcp_telegram_tools.py, callable
by Claude on behalf of ANY user who can type `.ask` in ANY whitelisted chat
(register_trigger has no owner/admin/sender check anywhere in the call
chain -- see claude_ask.py, no `is_owner`/`OWNER_ID` guard exists for it).

The code's own docstring for _resolve_any_chat_target claims: "Same
existing-dialog-only, exact-match discipline as everywhere else in this
file." That claim is FALSE for the numeric-id shortcut:

    if target.lstrip("-").isdigit():
        return int(target)

This returns immediately, with NO call to self._client / iter_dialogs /
get_entity -- i.e. no check whatsoever that the account is even in that
chat, let alone that the REQUESTING chat has any relationship to it.

Impact: any user who can invoke `.ask` in ANY whitelisted chat (per the
handoff doc, jarvis-ask is used by "the owner and their friends" -- this is
not owner-only) can ask Claude to "register a trigger that deletes/replies/
auto-agents on messages in chat <some other numeric id>", and it will be
silently created and persisted, honoring an attacker-chosen chat_id that has
nothing to do with the chat the request came from. This breaks the module's
own stated invariant and the multi-party trust model of a shared-instance
bot (see BRIDGE_PROJECT_HANDOFF.md: "used for real by the owner and their
friends... his father's separate instance").

This test loads the REAL, unmodified claude_ask.py from disk (via
_load_claude_ask.py's importlib shim -- see that file for why the shim is
needed) and calls the real, unmodified _resolve_any_chat_target and
_register_trigger_action methods directly. No network I/O, no real
Telegram/herokutl session -- self._client/self.db are minimal in-memory
fakes so the real code path can run standalone.

Run: python3 test_register_trigger_cross_chat_bypass.py

UPDATE (post-fix): _resolve_any_chat_target's numeric-id branch no longer
returns immediately -- it now runs through the SAME iter_dialogs loop as
the username/name branches, and only resolves if the id actually matches
an existing dialog's entity id. A never-seen numeric id now correctly
returns None (same as a never-seen username/name already did), and
register_trigger_action fails closed instead of persisting.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load_claude_ask import load_real_claude_ask_module, make_bare_instance


class FakeDB:
    """Minimal stand-in for Hikka's self.db -- a nested dict keyed by
    (module, key), exactly matching the real .get(module, key, default) /
    .set(module, key, value) contract claude_ask.py relies on."""

    def __init__(self):
        self._store = {}

    def get(self, module, key, default=None):
        return self._store.get((module, key), default)

    def set(self, module, key, value):
        self._store[(module, key)] = value


class FakeClient:
    """Simulates a Telethon/herokutl client that has NEVER seen the
    attacker-chosen chat id -- get_entity raises, exactly what the real
    library does for an unresolvable/never-cached id (per claude_ask.py's
    own comment: "get_entity on an id it has never seen ... raises")."""

    async def get_entity(self, _peer):
        raise ValueError("Cannot find any entity corresponding to the specified id")

    async def iter_dialogs(self):
        return
        yield  # pragma: no cover - makes this an async generator


class _FakeEntity:
    def __init__(self, id_, title=""):
        self.id = id_
        self.title = title
        self.username = ""


class _FakeDialog:
    def __init__(self, entity, is_user=False):
        self.entity = entity
        self.is_user = is_user


class FakeClientWithRealDialog:
    """Simulates a client that DOES have the target chat as an existing
    dialog -- used to confirm the fix doesn't break the legitimate case."""

    def __init__(self, dialog_entity):
        self._dialogs = [_FakeDialog(dialog_entity, is_user=False)]

    async def get_entity(self, _peer):
        return self._dialogs[0].entity

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d


def main():
    mod = load_real_claude_ask_module()
    inst = make_bare_instance(mod)
    inst.db = FakeDB()
    inst._client = FakeClient()

    class FakeInline:
        class bot:
            @staticmethod
            async def send_message(*a, **kw):
                raise RuntimeError("bot not a participant (simulated)")

    inst.inline = FakeInline()

    REQUESTING_CHAT_ID = 111  # some unrelated group an attacker is in
    ATTACKER_CHOSEN_TARGET = "987654321999"  # a chat id the requester picked

    # 1) The resolver itself: no dialog membership, no ownership link to the
    #    requesting chat -- must now fail closed (None), same as an unknown
    #    username/name already did before this fix.
    resolved = asyncio.run(
        inst._resolve_any_chat_target(ATTACKER_CHOSEN_TARGET, chat_id=REQUESTING_CHAT_ID)
    )
    assert resolved is None, (
        f"STILL BYPASSED: expected None for a never-seen numeric id, got {resolved!r}"
    )
    print("[1/3] never-seen numeric chat id -> resolver returns None (blocked)")

    # 2) End to end: register_trigger_action must refuse, not persist.
    result = asyncio.run(
        inst._register_trigger_action(
            chat_arg=ATTACKER_CHOSEN_TARGET,
            specs=[{"kind": "keyword", "value": ["evidence"], "action": "delete"}],
            chat_id=REQUESTING_CHAT_ID,
        )
    )
    triggers = inst._get_triggers()
    assert ATTACKER_CHOSEN_TARGET not in triggers, (
        f"STILL BYPASSED: trigger persisted under attacker-chosen id, "
        f"got keys={list(triggers.keys())}"
    )
    print("[2/3] register_trigger_action refused to persist:", result)

    # 3) Functionality check: a numeric id that IS an existing dialog must
    #    still resolve correctly -- the fix must not break legitimate use.
    real_entity = _FakeEntity(int(ATTACKER_CHOSEN_TARGET), title="A real existing group")
    inst2 = make_bare_instance(mod)
    inst2.db = FakeDB()
    inst2._client = FakeClientWithRealDialog(real_entity)
    inst2.inline = FakeInline()
    resolved2 = asyncio.run(
        inst2._resolve_any_chat_target(ATTACKER_CHOSEN_TARGET, chat_id=REQUESTING_CHAT_ID)
    )
    assert resolved2 == int(ATTACKER_CHOSEN_TARGET), (
        f"legitimate existing-dialog numeric id broke: got {resolved2!r}"
    )
    print("[3/3] numeric id that IS an existing dialog -> still resolves correctly")

    print(
        "\nCLOSED: the numeric-id branch of _resolve_any_chat_target now "
        "enforces the same existing-dialog-only discipline the code's own "
        "docstring always claimed for it."
    )


if __name__ == "__main__":
    main()
