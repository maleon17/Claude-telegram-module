"""
BREAK: cmd_queue.py's GET /download endpoint is an unauthenticated arbitrary
local file reader -- `path` is taken straight from the query string and
handed to open()/os.path.isfile() with NO restriction to any upload/temp
directory, and NO auth of any kind.

Where: ~/.hermes/scripts/cmd_queue.py, Queue.do_GET(), the `/download`
branch:

    filepath = params.get("path", [None])[0]
    if filepath and os.path.isfile(filepath):
        with open(filepath, "rb") as f:
            data = f.read()
        ...
        self.wfile.write(data)

Per BRIDGE_PROJECT_HANDOFF.md, `/download` is one of the routes proxied
through the PUBLIC Tailscale Funnel (`https://lightrag.tail4a204c.ts.net/download`
-> 127.0.0.1:9104 -> funnel_ask_router.py -> cmd_queue.py:9092/download),
reachable from the open internet by anyone with the URL, with no token/auth
check anywhere in this handler. Any absolute path readable by the `mishin`
user process -- SSH keys, the jarvis-ask Fernet session keys
(.session_key_<instance>), state.json (contains the live bot token),
/etc/passwd, etc -- can be exfiltrated with a single GET request.

Safety note: this test does NOT touch the real, live cmd_queue.py service
(confirmed running on 127.0.0.1:9092 during this assessment -- see
`ss -ltnp` in the session transcript). Instead it loads the REAL Queue
handler class straight from the actual cmd_queue.py source on disk (via
exec, with only the final unconditional `serve_forever()` call at module
scope stripped out in-memory so import doesn't try to rebind the live
port) and serves it on an OS-assigned ephemeral loopback port, in-process,
for the duration of this test only. Every line of handler logic exercised
below (do_GET, the /download branch, _json) is the real, unmodified,
shipped code -- nothing here is a reimplementation.

Run: python3 test_cmd_queue_download_arbitrary_read.py

UPDATE (post-fix): /download is now scoped to /tmp (os.path.realpath()
resolved, then required to be /tmp itself or a descendant of it -- this
matches the existing convention /upload already uses, which saves every
upload flat into /tmp, and claude_watcher.py's WORKDIR for SEND_FILE, which
is also /tmp). This test now verifies the fix directly: files outside /tmp
(session-encryption keys, state.json, /etc/passwd, a traversal attempt) are
rejected, while a legitimate file the app itself would plausibly write
under /tmp is still servable.
"""
import http.client
import os
import threading
import time
import uuid

REAL_CMD_QUEUE_PATH = os.path.expanduser("~/.hermes/scripts/cmd_queue.py")


def _load_real_queue_handler():
    with open(REAL_CMD_QUEUE_PATH) as f:
        source = f.read()

    # The real module now guards its server start with
    # `if __name__ == "__main__":`, so executing it under a test-only module
    # name imports the actual Queue handler without binding the live 9092
    # port. Every line of handler logic remains the real shipped code.
    ns = {"__name__": "cmd_queue_under_test"}
    exec(compile(source, REAL_CMD_QUEUE_PATH, "exec"), ns)
    return ns["Queue"], ns["ThreadingHTTPServer"]


def main():
    Queue, ThreadingHTTPServer = _load_real_queue_handler()

    # Ephemeral loopback port -- NOT the live service's 9092.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Queue)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def download(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/download?path={path}")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return body

    try:
        n = 0
        checks = 5

        # Real sensitive files named explicitly in BRIDGE_SECURITY_FIXES_TODO.md
        # as the live impact of this bug -- none of them live under /tmp.
        n += 1
        body = download("/etc/passwd")
        assert body != b"" and b"root:" not in body, f"/etc/passwd leaked: {body!r}"
        print(f"[{n}/{checks}] /etc/passwd -> blocked ({body!r})")

        session_key = os.path.expanduser(
            "~/.claude-telegram-bridge/jarvis-ask/.session_key_andrey"
        )
        if os.path.exists(session_key):
            n += 1
            body = download(session_key)
            assert b"error" in body, f"session key leaked: {body!r}"
            print(f"[{n}/{checks}] jarvis-ask session key -> blocked ({body!r})")
        else:
            checks -= 1

        state_file = os.path.expanduser("~/.claude-telegram-bridge/state.json")
        if os.path.exists(state_file):
            n += 1
            body = download(state_file)
            assert b"error" in body, f"state.json (bot token) leaked: {body!r}"
            print(f"[{n}/{checks}] state.json (bot token) -> blocked ({body!r})")
        else:
            checks -= 1

        # Traversal attempt escaping /tmp via ../ must resolve (realpath)
        # and still be rejected, not just string-prefix-matched.
        n += 1
        body = download("/tmp/../etc/passwd")
        assert b"root:" not in body, f"traversal escaped /tmp scoping: {body!r}"
        print(f"[{n}/{checks}] /tmp/../etc/passwd (traversal) -> blocked ({body!r})")

        # Functionality check: a legitimate file under /tmp -- the one
        # directory /upload's save_path and SEND_FILE's WORKDIR actually use
        # -- must still be servable, or the fix broke real functionality.
        n += 1
        marker = f"LEGIT-{uuid.uuid4().hex}"
        legit_path = "/tmp/breaker_cmdq_legit_check.txt"
        with open(legit_path, "w") as f:
            f.write(marker)
        try:
            body = download(legit_path)
            assert body.decode() == marker, f"legit /tmp download broke: {body!r}"
            print(f"[{n}/{checks}] legitimate /tmp file -> still served correctly")
        finally:
            os.remove(legit_path)

        print(
            "\nCLOSED: cmd_queue.py's /download endpoint now rejects anything "
            "outside /tmp (realpath-resolved, so ../ and symlink escapes are "
            "also rejected), matching /upload's and SEND_FILE's own existing "
            "/tmp convention -- while still serving legitimate /tmp files."
        )
    finally:
        server.shutdown()
        t.join(timeout=5)


if __name__ == "__main__":
    main()
