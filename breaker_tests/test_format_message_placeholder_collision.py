"""
BREAK: telegram_format.format_message()'s protect/restore placeholder
mechanism uses a predictable, low-entropy sentinel format
(`\\x00PH<counter>\\x00`, counter starting at 0 per call) with NO check
that the real message text doesn't already contain that exact sequence.
If it does, the final "restore placeholders in reverse order" step does a
blind str.replace() of that sentinel -- silently splicing in WHATEVER
OTHER protected fragment (a link, bold text, a code block...) happens to
share that placeholder index, in place of the attacker/tool-controlled
text that was actually there.

Where: ~/.claude-telegram-bridge/telegram_format.py, format_message():

    def _ph(value):
        key = f"\\x00PH{counter[0]}\\x00"
        counter[0] += 1
        placeholders[key] = value
        return key
    ...
    for key in reversed(list(placeholders.keys())):
        text = text.replace(key, placeholders[key])   # <-- blind replace,
                                                        #     matches ANY
                                                        #     occurrence,
                                                        #     not just the
                                                        #     one this call
                                                        #     itself inserted

Realistic trigger path: `format_message()` is called on Claude's own final
answer text (bridge.py's send_message/send_message_chunked/edit_message),
which routinely includes verbatim content Claude read from files/tool
output/web content -- content this bot does not control. A file containing
stray NUL bytes (binary content misread as text, or deliberately crafted
by whoever controls a document Claude is asked to summarize/quote) that
happens to contain the literal bytes `\\x00PH0\\x00` will have THAT exact
span silently replaced by an unrelated piece of the message's own
markdown -- corrupting/injecting content into what the user actually sees
in Telegram, with no error, warning, or indication anything went wrong.

This test calls the REAL, unmodified format_message() from the real
telegram_format.py.

Run: python3 test_format_message_placeholder_collision.py

UPDATE (post-fix): the placeholder key now embeds a random per-call nonce
(secrets.token_hex(8)), verified absent from the input before use -- so a
literal `\x00PH0\x00`-shaped span in real content can no longer collide
with the internal sentinel format at all. The original assertion
(`attacker_text in out`) doesn't actually work as a post-fix check: correct
MarkdownV2 escaping backslash-escapes the hyphens in attacker_text, so the
raw unescaped string is never a substring of correctly-processed output,
fixed or not. This version checks for the correctly ESCAPED form instead
(via the real escape_mdv2()), which is what should actually survive.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.claude-telegram-bridge"))
from telegram_format import escape_mdv2, format_message  # noqa: E402


def main():
    # A markdown bold construct earlier in the text claims placeholder
    # index 0 for "bold" first; the attacker/tool-controlled text later in
    # the very same message contains the literal sentinel the OLD internal
    # mechanism used for index 0 -- this must no longer matter post-fix.
    attacker_text = "irrelevant-content-that-should-survive-untouched"
    text = f"**bold**\x00PH0\x00{attacker_text}"

    out = format_message(text)
    print("input :", repr(text))
    print("output:", repr(out))

    # What SHOULD happen: the caller's literal bytes (even if they happen to
    # look like an internal sentinel) survive, correctly MarkdownV2-escaped
    # -- not silently swapped for unrelated content. Check for the properly
    # escaped form (real content is always escape_mdv2'd on this path),
    # since the raw unescaped string can never appear in valid output.
    expected = escape_mdv2(attacker_text)
    assert expected in out, (
        "attacker-controlled text vanished entirely -- replaced by "
        "unrelated internal placeholder content"
    )
    # The specific corruption this bug caused: the bold fragment's own
    # rendering ("*bold*") gets duplicated into where the attacker text
    # should be. Guard against that directly too.
    assert out.count("bold") <= 1, f"'bold' duplicated into attacker text's slot: {out!r}"


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nSTILL BROKEN: {e}")
        print(
            "The literal '\\x00PH0\\x00' span in the message text was "
            "replaced by an unrelated fragment (the bold text's own "
            "rendering) instead of being preserved -- content-integrity "
            "corruption via placeholder-sentinel collision."
        )
        raise SystemExit(1)
    else:
        print(
            "\nCLOSED: the attacker-controlled text survives (correctly "
            "escaped) instead of being replaced by an unrelated internal "
            "placeholder fragment."
        )
        raise SystemExit(0)
