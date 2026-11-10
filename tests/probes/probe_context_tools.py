"""Probe: the model proposes a fold, the proxy does it, the model's tool call never reaches the client.

    python tests/probes/probe_context_tools.py [REPO_ROOT]

Written from the behaviour description. A scripted model, when (and only when) it is offered tools,
asks to fold turns 2-4 of a long chat and then answers. With GUARDIAN_CONTEXT_TOOLS=1 the client must
see only the plain answer, the NEXT request the upstream receives must be smaller and must no longer
contain the folded turns' text, the folded originals must be in the sealed archive, and the archive
must verify. On a tree without the feature the model is never offered tools, nothing is folded and
the probe exits 1.
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
    "GUARDIAN_CONTEXT_TOOLS": "1", "GUARDIAN_NUM_CTX": "8000", "GUARDIAN_COMPACT_THRESHOLD": "0.85", "GUARDIAN_RESERVE_OUTPUT": "100",
    "GUARDIAN_MIN_SUMMARY_CHARS": "40", "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"), "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
    "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "v.json"), "GUARDIAN_RECEIPTS_PATH": os.path.join(tmp, "r.jsonl"),
    "GUARDIAN_VERSION_CHECK": "0", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_OLLAMA_CONTEXT": "0",
})
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

g = importlib.import_module("context_guardian")
import cg_archive  # noqa: E402

seen = []
asked = {"fold": False}


def reply(obj):
    raw = json.dumps(obj).encode()

    async def gen():
        yield raw
    return httpx.Response(200, headers={"content-type": "application/json"}, content=gen())


def handler(request):
    if not request.url.path.endswith("/chat/completions"):
        return httpx.Response(404, json={})
    body = json.loads(request.content)
    if "BEGIN_TRANSCRIPT" in json.dumps(body["messages"]):
        return reply({"choices": [{"message": {"role": "assistant", "content": "a short summary of the folded middle turns, long enough to be accepted"}, "finish_reason": "stop"}]})
    seen.append(body)
    offered = any((t.get("function") or {}).get("name", "").endswith("compress") for t in body.get("tools") or [])
    last_is_tool = body["messages"][-1].get("role") == "tool"
    if offered and not last_is_tool and not asked["fold"]:
        asked["fold"] = True
        name = next(t["function"]["name"] for t in body["tools"] if t["function"]["name"].endswith("compress") and "de" not in t["function"]["name"])
        msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps({"from_turn": 2, "to_turn": 4})}}]}
        return reply({"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]})
    return reply({"choices": [{"index": 0, "message": {"role": "assistant", "content": "the answer"}, "finish_reason": "stop"}]})


def history(n):
    out = []
    for i in range(1, n + 1):
        out.append({"role": "user", "content": "question %d MARK-U%d %s" % (i, i, "word " * 240)})
        out.append({"role": "assistant", "content": "answer %d MARK-A%d %s" % (i, i, "word " * 240)})
    return out


g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
client = TestClient(g.app, base_url="http://127.0.0.1:8786")


def post(n):
    return client.post("/v1/chat/completions", json={"model": "m", "messages": history(n)}, headers={"x-guardian-session": "probe"})


failed = 0


def check(name, ok):
    global failed
    print("%-5s %s" % ("ok" if ok else "FAIL", name))
    failed += 0 if ok else 1


r1 = post(7)
msg = r1.json()["choices"][0]["message"]
check("the client sees only the plain answer", msg.get("content") == "the answer" and not msg.get("tool_calls"))
check("nothing of the tool call is in the client's response text", "compress" not in r1.text and "tool_calls" not in r1.text)
n_before = len(seen)
post(8)
nxt = seen[-1]
text = " ".join(str(m.get("content")) for m in nxt["messages"])
check("the next request no longer carries the folded turns", "MARK-U3" not in text and "MARK-A4" not in text)
check("it still carries the first and the recent turns", "MARK-U1" in text and "MARK-U8" in text)
check("it is smaller than the unfolded history",
      len(text) < len(" ".join(str(m["content"]) for m in history(8))) * 0.8)
run_dir = Path(os.environ["GUARDIAN_SPAN_DIR"]) / g.RUN_ID if hasattr(g, "RUN_ID") else None
spans = sorted(run_dir.glob("0*.json")) if run_dir and run_dir.exists() else []
archived = "".join(p.read_text(encoding="utf-8") for p in spans)
check("the folded originals are in the archive", "MARK-U3" in archived and "MARK-A4" in archived)
check("the archive verifies", bool(spans) and cg_archive.verify_run(run_dir).ok)
print("probe_context_tools: %d failed" % failed)
sys.exit(1 if failed else 0)
