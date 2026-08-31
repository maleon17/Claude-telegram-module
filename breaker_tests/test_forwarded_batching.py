"""Regression tests for forwarded Telegram messages and prompt batching.

Run directly with:
    python3 breaker_tests/test_forwarded_batching.py
"""

import os
import sys
import tempfile
import time


_TMP = tempfile.mkdtemp(prefix="breaker_forward_batch_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(_TMP, "state.json")

sys.path.insert(0, os.path.expanduser("~/.claude-telegram-bridge"))
import bridge  # noqa: E402
import handlers  # noqa: E402
import runtime  # noqa: E402


def _reset_batch_state():
    with runtime.pending_batches_lock:
        runtime.pending_batches.clear()
        runtime.batch_timers.clear()
        runtime.pending_batch_generations.clear()
    handlers.busy_chats.clear()


def _wait_for_events(events, count):
    deadline = time.time() + 1
    while len(events) < count and time.time() < deadline:
        time.sleep(0.01)


def test_forwarded_rich_markdown_is_preserved():
    rich = {"markdown": "**Ответ из другого чата**\n\nПроверь это."}
    assert bridge.rich_message_to_markdown(rich) == rich["markdown"]

    msg = {
        "text": "Проверь это.",
        "forward_origin": {
            "type": "channel",
            "chat": {"title": "Источник", "username": "source_channel"},
        },
    }
    prompt = bridge._build_message_prompt(msg, msg["text"], "", "", "")
    assert prompt.startswith("[Пересланное сообщение от @source_channel]")
    assert "Проверь это." in prompt
    assert bridge._is_forwarded_message({"forward_sender_name": "Скрытый автор"})
    print("[1/4] forwarded text keeps source context")


def test_forwarded_rich_message_without_blocks_is_not_dropped():
    msg = {
        "forward_origin": {"type": "hidden_user", "sender_user_name": "Автор"},
        "rich_message": {"markdown": "Текст пересланного ответа"},
    }
    text = bridge.rich_message_to_markdown(msg["rich_message"])
    prompt = bridge._build_message_prompt(msg, text, "", "", "")
    assert prompt == "[Пересланное сообщение от Автор]\n\nТекст пересланного ответа"
    print("[2/4] forwarded rich markdown is delivered")


def test_idle_messages_are_one_prompt():
    _reset_batch_state()
    old_debounce = handlers.BATCH_DEBOUNCE_S
    events = []
    handlers.BATCH_DEBOUNCE_S = 0.03
    handlers.spawn_turn = lambda chat_id, prompt, state: events.append(
        ("spawn", chat_id, prompt)
    )
    handlers.dispatch_turn = lambda chat_id, prompt, state: events.append(
        ("dispatch", chat_id, prompt)
    )
    try:
        handlers.queue_prompt("idle", "первое", {})
        handlers.queue_prompt("idle", "второе", {})
        _wait_for_events(events, 1)
        assert events == [("spawn", "idle", "первое\n\n---\n\nвторое")]
        print("[3/4] idle burst is one fresh prompt")
    finally:
        handlers.BATCH_DEBOUNCE_S = old_debounce
        handlers.cancel_pending_batch("idle")


def test_busy_messages_are_one_injection():
    _reset_batch_state()
    old_debounce = handlers.BATCH_DEBOUNCE_S
    events = []
    handlers.BATCH_DEBOUNCE_S = 0.03
    handlers.spawn_turn = lambda chat_id, prompt, state: events.append(
        ("spawn", chat_id, prompt)
    )
    handlers.dispatch_turn = lambda chat_id, prompt, state: events.append(
        ("dispatch", chat_id, prompt)
    )
    handlers.busy_chats.add("busy")
    try:
        handlers.queue_prompt("busy", "раз", {})
        handlers.queue_prompt("busy", "два", {})
        _wait_for_events(events, 1)
        assert events == [("dispatch", "busy", "раз\n\n---\n\nдва")]
        print("[4/4] busy burst is one mid-turn injection")
    finally:
        handlers.BATCH_DEBOUNCE_S = old_debounce
        handlers.cancel_pending_batch("busy")
        handlers.busy_chats.clear()


if __name__ == "__main__":
    test_forwarded_rich_markdown_is_preserved()
    test_forwarded_rich_message_without_blocks_is_not_dropped()
    test_idle_messages_are_one_prompt()
    test_busy_messages_are_one_injection()
    print("\nCLOSED: forwarded content survives and rapid messages batch in both states.")
