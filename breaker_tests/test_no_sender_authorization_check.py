"""Regression test for the owner-only Telegram action dispatch gate.

The action handlers deliberately keep their narrow business-method
signatures. Authorization belongs at the live tool-dispatch boundary,
where the requester's Telegram id is available and can be checked before a
handler touches the account.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load_claude_ask import load_real_claude_ask_module, make_bare_instance


OWNER_ID = 8480261623
TEST_BOT_ID = 8747608932


class FakeClient:
    async def get_me(self):
        return type("Me", (), {"id": OWNER_ID})()


async def _run_tool(mod, requester_id, chat_id="123"):
    instance = make_bare_instance(mod)
    instance._client = FakeClient()
    instance._owner_id_cache = None
    calls = []
    results = []

    async def contact_action(action, target):
        calls.append((action, target))
        return "executed"

    instance._contact_action = contact_action
    instance._fetch_pending_tool_call = lambda: {
        "request_id": "regression-request",
        "instance_id": "andrey",
        "chat_id": str(chat_id),
        "tool": "block_user",
        "args": {"target": "@target"},
        "requester_id": requester_id,
    }
    instance._post_tool_call_result = lambda request_id, result: results.append(
        (request_id, result)
    )
    await instance.tool_call_watcher()
    return calls, results


def main():
    mod = load_real_claude_ask_module()

    calls, results = asyncio.run(_run_tool(mod, requester_id=111))
    assert calls == [], "non-owner request reached a Telegram action"
    assert results[0][1].startswith(mod.INTERNAL_TOOL_RESULT_PREFIX)
    assert "Действие НЕ выполнено" in results[0][1]
    print("[1/2] non-owner Telegram action was denied before handler execution")

    calls, results = asyncio.run(
        _run_tool(mod, requester_id=TEST_BOT_ID, chat_id=OWNER_ID)
    )
    assert calls == [("block_user", "@target")]
    assert results == [("regression-request", "executed")]
    print("[2/2] dedicated test channel reached the handler in owner's DM")

    print("\nCLOSED: Telegram action dispatch is owner-only plus the scoped test channel.")


if __name__ == "__main__":
    main()
