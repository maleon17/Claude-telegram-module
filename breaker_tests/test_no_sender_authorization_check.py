"""
BREAK: none of the 17 real Telegram-action MCP tool handlers in
claude_ask.py (send_message, block_user/unblock_user, delete_messages,
invite_to_group, leave_chat, create_group, etc.) receive or check WHO is
asking. The only scoping available anywhere in the call chain is "which
chat did the .ask request come from" (CHAT_ID) -- there is no
owner-only / admin-only / sender-identity gate anywhere for any
account-mutating action.

This matters because, per BRIDGE_PROJECT_HANDOFF.md, `.ask` is "already
being used for real by the owner and their friends" in shared groups (a
live group, chat_id 8569489601, is named explicitly), and the doc notes
separately that sibling userbot commands (`.terminal`/`.lm`) "fire
regardless of sender (any account in the authorized chat)" -- i.e. this
is a userbot where commands are not restricted to the account owner's own
messages by default. Combined with zero identity checks in the action
handlers themselves, this means: any person who can put a message in a
chat where the owner's userbot is present can ask Claude, in plain
language, to block/unblock a contact, delete other people's messages,
leave a group, invite someone, send a message as the account, etc. -- and
it happens immediately, for real, with no confirmation step and no way
for the code to have refused even if it wanted to (the information needed
to refuse was never collected).

This test proves the structural half of that claim directly against the
REAL, unmodified claude_ask.py: `_contact_action` (the real handler behind
the `block_user`/`unblock_user`/`add_contact`/`remove_contact` MCP tools)
executes the real BlockRequest unconditionally, with a function signature
that has no `sender_id`/`chat_id`/requester parameter of any kind --
proving no authorization decision is structurally possible here, not just
"unlikely because nobody would ask". Same absence of any identity
parameter, verified below, holds for `_delete_messages_action` and
`_leave_chat_action`.

(Whether non-owner senders can literally trigger `.ask` in this specific
Heroku/Hikka deployment is a framework/config fact this test can't
exercise directly without the real framework installed -- flagged as
"confirmed structurally, framework-dependent for real-world blast radius"
in the report, not overclaimed as fully live-verified.)

Run: python3 test_no_sender_authorization_check.py
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load_claude_ask import load_real_claude_ask_module, make_bare_instance


class RecordingClient:
    """Fakes just enough of the Telethon/herokutl client surface for
    _contact_action to run its real body end to end."""

    def __init__(self, known_entity):
        self._known_entity = known_entity
        self.calls = []

    async def iter_dialogs(self):
        class D:
            def __init__(self, ent):
                self.entity = ent
                self.is_user = True
        yield D(self._known_entity)

    async def __call__(self, request):
        # Records the real TL request object the real handler builds
        # (BlockRequest(id=<entity>)) -- proves the real action fires.
        self.calls.append(request)
        return True


class FakeEntity:
    id = 424242
    first_name = "Target"
    last_name = "Person"
    username = "target_person"


def main():
    mod = load_real_claude_ask_module()

    # 1) Structural proof: no requester/sender/identity parameter exists on
    #    any of these three representative destructive-action handlers.
    for name in ("_contact_action", "_delete_messages_action", "_leave_chat_action"):
        sig = inspect.signature(getattr(mod.ClaudeAsk, name))
        params = list(sig.parameters)
        print(f"{name}{sig}")
        assert "sender_id" not in params
        assert "requester" not in params
        assert "from_id" not in params
        assert "is_owner" not in params
    print("[1/2] CONFIRMED: zero identity/authorization parameters exist on "
          "any of these real destructive-action handlers.\n")

    # 2) Behavioral proof: _contact_action("block_user", ...) really issues
    #    the real BlockRequest, unconditionally, given only a target -- no
    #    caller-identity gate anywhere in the body either.
    inst = make_bare_instance(mod)
    entity = FakeEntity()
    inst._client = RecordingClient(entity)

    class FakeInline:
        class bot:
            @staticmethod
            async def send_message(*a, **kw):
                raise RuntimeError("simulated: bot not a participant")

    inst.inline = FakeInline()
    inst.db = type("DB", (), {"get": lambda *a, **kw: None, "set": lambda *a, **kw: None})()

    result = asyncio.run(inst._contact_action("block_user", "@target_person"))
    print("_contact_action result:", result)

    assert inst._client.calls, "expected the real BlockRequest to have fired"
    fired = inst._client.calls[0]
    assert type(fired).__name__ == "BlockRequest"
    print(
        "[2/2] CONFIRMED: block_user executed for real (BlockRequest fired) "
        "purely from a target string -- nothing about who asked was ever "
        "consulted or even available to consult."
    )


if __name__ == "__main__":
    main()
