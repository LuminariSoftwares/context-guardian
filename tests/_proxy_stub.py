"""Shared helpers for the proxy tests: a fresh module and a recording stub upstream.

Not collected by pytest (no test_ prefix). Config is read at import time, so
every env var is set BEFORE the reload, and the knobs these tests flip are
cleared first so one test's setting cannot leak into the next reload.
"""
import importlib
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_CLEARED = ("GUARDIAN_DRY_RUN", "GUARDIAN_INJECT_STREAM_OPTIONS",
            "GUARDIAN_OLLAMA_CONTEXT", "GUARDIAN_UPSTREAM_URL",
            "GUARDIAN_KEEP_RECENT_MESSAGES", "GUARDIAN_RESERVE_OUTPUT")

SUMMARY = "condensed summary of the older turns, long enough to be accepted"


def load(monkeypatch, tmp_path, **env):
    for k in _CLEARED:
        monkeypatch.delenv(k, raising=False)
    base = {"GUARDIAN_NUM_CTX": "1000", "GUARDIAN_COMPACT_THRESHOLD": "0.5",
            "GUARDIAN_RESERVE_OUTPUT": "100", "GUARDIAN_MIN_SUMMARY_CHARS": "40",
            "GUARDIAN_LOG_PATH": str(tmp_path / "guardian_log.json"),
            "GUARDIAN_SPAN_DIR": str(tmp_path / "spans")}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


def wire(g, show=None):
    """Point the proxy at a stub. Records every request the stub receives.

    The summariser's call (its prompt carries BEGIN_TRANSCRIPT) gets SUMMARY
    back; every other chat call gets a two-frame SSE stream. `show` answers
    /api/show.
    """
    seen = {"chat": [], "show": []}

    def handler(request):
        if request.url.path.endswith("/api/show"):
            seen["show"].append(json.loads(request.content))
            if show is None:
                return httpx.Response(404, json={"error": "not found"})
            return show(request)
        body = request.content
        if b"BEGIN_TRANSCRIPT" in body:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": SUMMARY}, "finish_reason": "stop"}]})
        seen["chat"].append(body)

        async def _stream():
            yield b"data: one\n\n"
            yield b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def post(g, body: bytes):
    from fastapi.testclient import TestClient
    return TestClient(g.app).post("/v1/chat/completions", content=body,
                                  headers={"content-type": "application/json"})


def events(g, name):
    path = Path(g.LOG_PATH)
    if not path.exists():
        return []
    return [e for e in (json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines())
            if e.get("event") == name]
