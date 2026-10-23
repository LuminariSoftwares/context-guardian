#!/usr/bin/env python3
"""A session that overflows its window, sent twice: straight to the backend,
and through Context Guardian.

    python examples/demo_overflow.py

The backend is a stub on 127.0.0.1 (standard library http.server, a free port)
that behaves like a local server with a fixed window: it counts a request's
tokens as characters / 3.5 and answers 400 "context length exceeded" when they
do not fit. The client is scripted: each turn it asks a question, gets a large
tool result, and resends its whole history, as an agentic CLI does.

The proxy is the real app (context_guardian.app), driven in-process; its calls
to the backend are real HTTP to 127.0.0.1. Nothing else is contacted, and the
span archive goes to a temporary directory removed on exit. The output is the
same on every run and every machine; tests/test_demo_overflow.py checks that
the README quotes it exactly.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WINDOW = 8192          # the stub backend's context window, in tokens
CHARS_PER_TOKEN = 3.5  # how the stub counts; the proxy's default estimate too
TURNS = 12

SUMMARY = ("Earlier turns: the user is fixing the retry logic in src/net.py; "
           "the assistant read net.py, config.py and the failing test, and the "
           "agreed fix is to cap retries at 5 with exponential backoff.")


def _tokens(messages):
    return int(sum(len(m.get("content") or "") for m in messages) / CHARS_PER_TOKEN)


def _turn(i):
    """What one turn adds: a question, a tool result, the assistant's reply."""
    line = "line %03d of the file the tool read for turn %d\n"
    return [
        {"role": "user", "content": "Turn %d: read the next file and tell me what calls retry()." % i},
        {"role": "tool", "content": "".join(line % (n, i) for n in range(80))},
        {"role": "assistant", "content": "In that file retry() is called from send() and from the reconnect loop. " * 4},
    ]


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        if any("BEGIN_TRANSCRIPT" in (m.get("content") or "") for m in messages):
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": SUMMARY},
                                          "finish_reason": "stop"}]})
            return
        used = _tokens(messages)
        if used > WINDOW:
            self._send(400, {"error": {"message": "context length exceeded: %d > %d tokens" % (used, WINDOW),
                                       "type": "invalid_request_error"}})
            return
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "ok"},
                                      "finish_reason": "stop"}]})

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


async def _session(cg, base):
    import httpx

    # trust_env=False: the stub is on 127.0.0.1, so no HTTP(S)_PROXY applies.
    direct = httpx.AsyncClient(base_url=base, trust_env=False)
    cg._http_client = httpx.AsyncClient(timeout=30, trust_env=False)
    proxy = httpx.AsyncClient(transport=httpx.ASGITransport(app=cg.app),
                              base_url="http://127.0.0.1:8786")
    history = [{"role": "system", "content": "You are a coding assistant."}]
    rows = []
    try:
        for i in range(1, TURNS + 1):
            history.extend(_turn(i)[:-1])
            body = {"model": "demo", "stream": False, "messages": history}
            a = await direct.post("/chat/completions", json=body)
            b = await proxy.post("/v1/chat/completions", json=body)
            rows.append((i, _tokens(history), a.status_code, b.status_code,
                         cg._state["compactions_performed"]))
            history.append(_turn(i)[-1])
    finally:
        await direct.aclose()
        await proxy.aclose()
        await cg._http_client.aclose()
    return rows


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % server.server_address[1]
    tmp = tempfile.mkdtemp(prefix="cg-demo-")
    try:
        # Set BEFORE import: the proxy reads its config at import time, and a
        # .env beside it must not change what this demo shows.
        os.environ.update({
            "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
            "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"),
            "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "version.json"),
            "GUARDIAN_VERSION_CHECK": "0",
            "GUARDIAN_UPSTREAM_URL": base,
            "GUARDIAN_NUM_CTX": str(WINDOW),
            "GUARDIAN_RESERVE_OUTPUT": "1024",
            "GUARDIAN_COMPACT_THRESHOLD": "0.85",
            "GUARDIAN_KEEP_RECENT_MESSAGES": "",
            "GUARDIAN_KEEP_RECENT_FRACTION": "0.20",
            "GUARDIAN_CHARS_PER_TOKEN": str(CHARS_PER_TOKEN),
            "GUARDIAN_OLLAMA_CONTEXT": "0",
            "GUARDIAN_DRY_RUN": "0",
            "GUARDIAN_INJECT_STREAM_OPTIONS": "0",
            "GUARDIAN_CALIBRATE": "0",
            "GUARDIAN_VERBOSE": "0",
        })
        sys.path.insert(0, str(ROOT))
        import context_guardian as cg
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("context_guardian").setLevel(logging.ERROR)

        rows = asyncio.run(_session(cg, base))
        spans = sum(1 for _ in Path(tmp, "spans").rglob("*.json"))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("stub backend on 127.0.0.1, %d-token window, one scripted session sent twice" % WINDOW)
    print("turn   tokens  direct to backend  through context-guardian")
    for i, used, a, b, c in rows:
        print("%4d  %7d  %-17s  %s" % (i, used, "ok" if a == 200 else "%d overflow" % a,
                                       ("ok" if b == 200 else "%d" % b) + (", compactions so far: %d" % c if c else "")))
    died = next((i for i, _, a, _, _ in rows if a != 200), None)
    answered = sum(1 for r in rows if r[3] == 200)
    print("direct: rejected from turn %s on. proxy: %d/%d turns answered, %d originals archived."
          % (died, answered, TURNS, spans))


if __name__ == "__main__":
    main()
