"""P111 S3 (B3) acceptance -- a real usage endpoint and a status line. Written by the O5 overseer; DO NOT EDIT.

Why: README "What this does not do" (2): "It doesn't fix your CLI's own context-usage display ... its own token
counter will drift from reality after a compaction." The client cannot know what the proxy forwarded. This gives
it the proxy's own number to show, without patching the client.

Contract (plan P111 rev 1, decision 4 + C3; the details below pinned by O5):
- `GET /guardian/usage[?session=<id>]` -> 200 JSON with exactly the keys
  {"window", "used_after_compaction", "pct", "count_source", "model", "last_compaction_at"}, describing the LATEST
  chat request of that session:
    window                 the context window the proxy used for it (stats `last_num_ctx` for that request)
    used_after_compaction  the proxy's count of what it FORWARDED: the compaction event's `estimated_tokens_after`
                           when that request was compacted, else the request's own count (stats
                           `last_known_total_tokens` for that request)
    pct                    round(100.0 * used_after_compaction / window, 1)  (0.0 when window is 0)
    count_source           that request's count_source (tokenize | calibrated | estimate)
    model                  the request's "model"
    last_compaction_at     the `timestamp` of the session's latest compaction event, or null if it never compacted
- Session id: the `X-Guardian-Session` request header when present and non-empty; otherwise
  sha256((model + "\\n" + system + "\\n" + first_user).encode("utf-8")).hexdigest()[:16], where system is the
  content of the first role=="system" message and first_user the content of the first role=="user" message
  ("" when absent; a list content counts as its text parts joined by "\\n").
- Without `session`, the endpoint answers for the session of the most recent chat request. No request seen yet
  -> 404 {"error": "no session seen yet"}; an unknown id -> 404 {"error": "unknown session"}.
- `scripts/cg_statusline.py --url <base> [--session <id>] [--format claude|opencode|plain] [--timeout <s>]`
  (stdlib only) GETs <base>/guardian/usage (with ?session=<id> when given) and prints ONE line
  `ctx <round(pct)>% (<k(used)>/<k(window)>) <exact|est>`, exit 0. Here k(n) = str(n) below 1000, else
  ("%.1f" % (n / 1000)) with a trailing ".0" dropped, plus "k". "exact" iff count_source == "tokenize".
  Any failure (unreachable, timeout, non-200, bad JSON) prints `ctx ?` and exits 1: a status line must never
  hang or crash the client. `--format claude` reads and ignores stdin (Claude Code pipes JSON in); every format
  prints the same line.
- README: bullet (2) is gone; a "Show real usage in your client" section shows the Claude Code `statusLine`
  setting and an OpenCode snippet.
"""
import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from _proxy_stub import LOOPBACK, events, load, post, wire

ROOT = Path(__file__).resolve().parent.parent
STATUSLINE = ROOT / "scripts" / "cg_statusline.py"
KEYS = {"window", "used_after_compaction", "pct", "count_source", "model", "last_compaction_at"}


def proxy(monkeypatch, tmp_path, **env):
    base = {"GUARDIAN_NUM_CTX": "1000", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_KEEP_RECENT_MESSAGES": "2",
            "GUARDIAN_CALIBRATE": "0"}
    base.update(env)
    g = load(monkeypatch, tmp_path, **base)
    wire(g)
    return g


def client(g):
    from fastapi.testclient import TestClient
    return TestClient(g.app, base_url=LOOPBACK)


def chat(g, messages, model="m", session=None):
    if session is None:
        return post(g, json.dumps({"model": model, "messages": messages}).encode())
    return client(g).post("/v1/chat/completions", content=json.dumps({"model": model, "messages": messages}).encode(),
                          headers={"content-type": "application/json", "x-guardian-session": session})


def usage(g, session=None):
    return client(g).get("/guardian/usage", params={} if session is None else {"session": session})


def stats(g):
    return client(g).get("/guardian/stats").json()


def key(model, system, first_user):
    return hashlib.sha256((model + "\n" + system + "\n" + first_user).encode("utf-8")).hexdigest()[:16]


SYS = {"role": "system", "content": "You are terse."}


def short_turns(n, first="hello there"):
    out = [SYS, {"role": "user", "content": first}]
    for i in range(n):
        out.append({"role": "assistant" if i % 2 == 0 else "user", "content": "turn %02d about the plan" % i})
    return out


def long_turns(n=30, first="start the long task"):
    out = [SYS, {"role": "user", "content": first}]
    for i in range(n):
        out.append({"role": "assistant" if i % 2 == 0 else "user",
                    "content": "turn %02d " % i + "a fairly long sentence about the work so far " * 4})
    return out


# ---- the endpoint -------------------------------------------------------------------------------------------
def test_before_any_request_is_404(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    r = usage(g)
    assert r.status_code == 404 and r.json() == {"error": "no session seen yet"}


def test_shape_and_values_after_a_plain_request(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    assert chat(g, short_turns(2)).status_code == 200
    body = usage(g).json()
    st = stats(g)
    assert set(body) == KEYS
    assert body["model"] == "m" and body["last_compaction_at"] is None
    assert body["window"] == st["last_num_ctx"] == 1000
    assert body["used_after_compaction"] == st["last_known_total_tokens"] > 0
    assert body["count_source"] == st["count_source"] == "estimate"


def test_pct_math(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    chat(g, short_turns(4))
    b = usage(g).json()
    assert b["pct"] == round(100.0 * b["used_after_compaction"] / b["window"], 1)


def test_default_session_key_is_the_hash(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    chat(g, short_turns(2), model="m1")
    k = key("m1", "You are terse.", "hello there")
    r = usage(g, k)
    assert r.status_code == 200 and r.json()["model"] == "m1"
    assert usage(g, "0" * 16).status_code == 404 and usage(g, "0" * 16).json() == {"error": "unknown session"}


def test_later_turns_keep_the_session_and_update_it(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    k = key("m", "You are terse.", "hello there")
    chat(g, short_turns(2))
    first = usage(g, k).json()["used_after_compaction"]
    chat(g, short_turns(6))
    second = usage(g, k).json()["used_after_compaction"]
    assert second > first and second == stats(g)["last_known_total_tokens"]


def test_header_overrides_the_hash(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    chat(g, short_turns(2), session="my-session-1")
    assert usage(g, "my-session-1").status_code == 200
    assert usage(g, key("m", "You are terse.", "hello there")).status_code == 404


def test_sessions_are_separate_and_the_default_is_the_latest(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    chat(g, short_turns(2, first="first task"), model="a")
    chat(g, short_turns(6, first="second task"), model="b")
    ka, kb = key("a", "You are terse.", "first task"), key("b", "You are terse.", "second task")
    ua, ub = usage(g, ka).json(), usage(g, kb).json()
    assert ua["model"] == "a" and ub["model"] == "b" and ua["used_after_compaction"] < ub["used_after_compaction"]
    assert usage(g).json() == ub


def test_a_list_content_counts_as_its_text(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    msgs = [{"role": "system", "content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]},
            {"role": "user", "content": [{"type": "text", "text": "go"}]}]
    chat(g, msgs)
    assert usage(g, key("m", "A\nB", "go")).status_code == 200


def test_after_compaction_used_is_what_was_forwarded(monkeypatch, tmp_path):
    g = proxy(monkeypatch, tmp_path)
    assert chat(g, long_turns()).status_code == 200
    comp = events(g, "compaction")
    assert comp, "the long conversation should have compacted"
    b = usage(g, key("m", "You are terse.", "start the long task")).json()
    assert b["used_after_compaction"] == comp[-1]["estimated_tokens_after"] < comp[-1]["estimated_tokens_before"]
    assert b["last_compaction_at"] == comp[-1]["timestamp"]
    assert b["pct"] == round(100.0 * b["used_after_compaction"] / b["window"], 1)


# ---- the status line ----------------------------------------------------------------------------------------
class _Usage(BaseHTTPRequestHandler):
    body = {}
    status = 200
    paths = []

    def do_GET(self):
        type(self).paths.append(self.path)
        data = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def serve(body, status=200):
    handler = type("H", (_Usage,), {"body": body, "status": status, "paths": []})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


def line(*args, stdin=""):
    return subprocess.run([sys.executable, str(STATUSLINE), *args], input=stdin, capture_output=True, text=True,
                          timeout=30)


EXACT = {"window": 32000, "used_after_compaction": 13200, "pct": 41.2, "count_source": "tokenize", "model": "m",
         "last_compaction_at": None}
EST = {"window": 1000, "used_after_compaction": 950, "pct": 95.0, "count_source": "estimate", "model": "m",
       "last_compaction_at": None}


def test_statusline_prints_the_line():
    srv, _ = serve(EXACT)
    try:
        r = line("--url", "http://127.0.0.1:%d" % srv.server_address[1])
        assert (r.returncode, r.stdout.strip()) == (0, "ctx 41% (13.2k/32k) exact"), r.stdout + r.stderr
    finally:
        srv.shutdown()
        srv.server_close()
    srv, _ = serve(EST)
    try:
        r = line("--url", "http://127.0.0.1:%d" % srv.server_address[1], "--format", "plain")
        assert (r.returncode, r.stdout.strip()) == (0, "ctx 95% (950/1k) est")
    finally:
        srv.shutdown()
        srv.server_close()


def test_statusline_passes_the_session_and_formats_agree():
    srv, h = serve(EXACT)
    try:
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        a = line("--url", url, "--session", "abc123", "--format", "claude", stdin='{"session_id": "x"}')
        b = line("--url", url, "--format", "opencode")
        assert a.returncode == 0 and b.returncode == 0 and a.stdout == b.stdout
        assert h.paths[0] == "/guardian/usage?session=abc123" and h.paths[1] == "/guardian/usage"
    finally:
        srv.shutdown()
        srv.server_close()


def test_statusline_failures_print_ctx_question_and_exit_1():
    srv, _ = serve({"error": "no session seen yet"}, status=404)
    try:
        r = line("--url", "http://127.0.0.1:%d" % srv.server_address[1])
        assert (r.returncode, r.stdout.strip()) == (1, "ctx ?")
    finally:
        srv.shutdown()
        srv.server_close()
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = line("--url", "http://127.0.0.1:%d" % port, "--timeout", "2")
    assert (r.returncode, r.stdout.strip()) == (1, "ctx ?")


def test_readme_documents_the_status_line():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "It doesn't fix your CLI's own context-usage display" not in text
    assert "Show real usage in your client" in text and '"statusLine"' in text and "cg_statusline.py" in text
