"""Probe: an empty upstream turn must reach the client as the retry's answer.

Written from the behaviour description, not from the implementation: a client asks one
question, the backend answers once with nothing and then with a real answer. With
GUARDIAN_EMPTY_RETRY=1 the client must receive the real answer after exactly two
upstream calls, streaming and not, and the nudge must not appear in the client's view.

    python tests/probes/probe_empty_turn_retry.py [REPO_ROOT]

Exit 0 when every check passes. On a tree without the feature it exits 1 (the client
is handed the empty turn).
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
tmp = tempfile.mkdtemp(prefix="cg-probe-")
os.environ.update({
    "GUARDIAN_EMPTY_RETRY": "1", "GUARDIAN_NUM_CTX": "100000",
    "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"), "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
    "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "v.json"), "GUARDIAN_RECEIPTS_PATH": os.path.join(tmp, "r.jsonl"),
    "GUARDIAN_VERSION_CHECK": "0", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_OLLAMA_CONTEXT": "0",
})
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

g = importlib.import_module("context_guardian")
ANSWER = "the capital of France is Paris"


def frames(text):
    def f(delta, fin=None):
        return b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}).encode() + b"\n\n"
    out = f({"role": "assistant", "content": ""})
    if text:
        out += f({"content": text})
    return out + f({}, "stop") + b"data: [DONE]\n\n"


def run(stream):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        text = ANSWER if len(calls) > 1 else ""
        if stream:
            body, ctype = frames(text), "text/event-stream"
        else:
            body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}).encode()
            ctype = "application/json"

        async def gen():                      # a response built from bytes cannot be streamed through the proxy
            yield body
        return httpx.Response(200, headers={"content-type": ctype}, content=gen())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    body = {"model": "m", "stream": stream, "messages": [{"role": "user", "content": "capital of France?"}]}
    r = TestClient(g.app, base_url="http://127.0.0.1:8786").post("/v1/chat/completions", json=body)
    return r, calls


failed = 0
for stream in (False, True):
    r, calls = run(stream)
    got = r.text
    nudge = json.dumps(calls[-1]["messages"][-1]["content"])[1:-1] if len(calls) > 1 else None
    checks = [
        ("client receives the real answer", ANSWER in got),
        ("exactly two upstream calls", len(calls) == 2),
        ("second call ends with a user message", len(calls) == 2 and calls[1]["messages"][-1]["role"] == "user"),
        ("second call keeps the original history", len(calls) == 2 and calls[1]["messages"][0] == calls[0]["messages"][0]),
        ("nudge text not shown to the client", nudge is None or nudge not in got),
    ]
    for name, ok in checks:
        print("%-5s %-10s %s" % ("ok" if ok else "FAIL", "stream" if stream else "plain", name))
        failed += 0 if ok else 1
print("probe_empty_turn_retry: %d failed" % failed)
sys.exit(1 if failed else 0)
