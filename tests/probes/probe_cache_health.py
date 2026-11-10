"""Probe: the cache-health ledger must report how much of the previous request's prefix a
compaction kept, and that number must match an independent count taken from the requests the
upstream actually received.

    python tests/probes/probe_cache_health.py [REPO_ROOT]

A 12-turn session grows until the proxy compacts. The stub upstream records every request.
With GUARDIAN_CACHE_HEALTH=1 the proxy's `/guardian/stats` must carry a `cache_health` block
whose last compaction row agrees (within 10 points) with the share of characters the stub saw
stay identical at the front between the two requests around that compaction. Exit 0 = all
checks pass; on a tree without the feature it exits 1 (no `cache_health` in the stats).
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
    "GUARDIAN_CACHE_HEALTH": "1", "GUARDIAN_NUM_CTX": "1000", "GUARDIAN_COMPACT_THRESHOLD": "0.5",
    "GUARDIAN_RESERVE_OUTPUT": "100", "GUARDIAN_MIN_SUMMARY_CHARS": "40",
    "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"), "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
    "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "v.json"), "GUARDIAN_RECEIPTS_PATH": os.path.join(tmp, "r.jsonl"),
    "GUARDIAN_VERSION_CHECK": "0", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_OLLAMA_CONTEXT": "0",
})
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

g = importlib.import_module("context_guardian")
seen = []


def reply(obj):
    body = json.dumps(obj).encode()

    async def gen():
        yield body
    return httpx.Response(200, headers={"content-type": "application/json"}, content=gen())


def handler(request):
    if not request.url.path.endswith("/chat/completions"):
        return httpx.Response(404, json={})
    if b"BEGIN_TRANSCRIPT" in request.content:
        return reply({"choices": [{"message": {"role": "assistant", "content": "a condensed summary of the older turns, long enough to be accepted"}, "finish_reason": "stop"}]})
    seen.append(json.loads(request.content))
    return reply({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 400}}})


g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
client = TestClient(g.app, base_url="http://127.0.0.1:8786")
hist = [{"role": "system", "content": "You are a careful assistant. " * 5}]
for i in range(12):
    hist += [{"role": "user", "content": "question %d %s" % (i, "pad " * 60)},
             {"role": "assistant", "content": "answer %d %s" % (i, "pad " * 60)}]
    client.post("/v1/chat/completions", json={"model": "m", "messages": list(hist)},
                headers={"x-guardian-session": "probe"})

stats = client.get("/guardian/stats").json()
block = stats.get("cache_health")
failed = 0


def check(name, ok):
    global failed
    print("%-5s %s" % ("ok" if ok else "FAIL", name))
    failed += 0 if ok else 1


check("stats carries a cache_health block", isinstance(block, dict) and block.get("enabled") is True)
rows = ((block or {}).get("latest_session") or {}).get("rows") or []
check("at least one real compaction was measured", bool(rows))
if rows:
    row = rows[-1]
    i = row["seq"] - 1
    a, b = seen[i - 1]["messages"], seen[i]["messages"]
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += len(json.dumps(x))
    expected = 100.0 * shared / sum(len(json.dumps(m)) for m in a)
    check("reported share (%.1f%%) matches the requests the upstream saw (%.1f%%)" % (row["shared_pct"], expected),
          abs(row["shared_pct"] - expected) <= 10)
    check("the line says it in plain English", "% of the prefix" in (block.get("line") or ""))
    check("cached-token counts from the upstream show up as a hit rate", "cache hit rate" in (block.get("line") or ""))
print("probe_cache_health: %d failed" % failed)
sys.exit(1 if failed else 0)
