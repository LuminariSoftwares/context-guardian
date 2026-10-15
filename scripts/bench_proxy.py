#!/usr/bin/env python3
"""Re-runnable proxy benchmark: a scripted agent conversation through the real
proxy app, against an in-process stub upstream.

    python scripts/bench_proxy.py --turns 40 --seed 1

Prints ONE line:

    turns=N tokens_before=B tokens_after=A compactions=C p50_added_ms=X p95_added_ms=Y

  tokens_before  estimated tokens the client sent, summed over every turn (an
                 agentic client resends the whole conversation each time)
  tokens_after   estimated tokens that reached the upstream after compaction
  compactions    compactions the proxy performed
  added_ms       wall time through the proxy minus the time the stub spent on
                 the forwarded request. The stub summarises instantly, so this
                 is the proxy's own overhead, not a real model's summarisation
                 time.

Same --turns and --seed give the same token figures; latency varies by machine.
Nothing touches the network or the repository: spans and the log go to a
temporary directory that is removed on exit.
"""
import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WORDS = ("parser config test build cache token window model reply tool file path "
         "error retry budget span summary queue worker index schema module "
         "request stream offset buffer handler").split()

SUMMARY = ("Summary of the earlier turns: the user asked for a parser fix, the "
           "assistant read the config and test files, ran the build twice, and "
           "agreed to keep the cache settings. Open item: the retry budget. "
           ) * 3


def _text(rng, lo, hi):
    n = rng.randint(lo, hi)
    out, size = [], 0
    while size < n:
        w = rng.choice(WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)[:n]


def _turn(rng, i):
    """The messages one turn adds: a user ask, sometimes a tool round-trip,
    then the assistant's reply."""
    msgs = [{"role": "user", "content": _text(rng, 200, 1500)}]
    if rng.random() < 0.5:
        call_id = "call_%d" % i
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "read_file",
                         "arguments": json.dumps({"path": "src/%s.py" % rng.choice(WORDS)})}}]})
        msgs.append({"role": "tool", "tool_call_id": call_id,
                     "content": _text(rng, 2000, 12000)})
    msgs.append({"role": "assistant", "content": _text(rng, 300, 3000)})
    return msgs


def _percentile(values, pct):
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered))) - 1))
    return ordered[rank]


async def _run(cg, turns, seed):
    import httpx

    rng = random.Random(seed)
    stub_seconds = []
    received_tokens = []

    async def upstream(request):
        started = time.perf_counter()
        body = request.content
        if b"BEGIN_TRANSCRIPT" in body:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": SUMMARY}, "finish_reason": "stop"}]})
        received_tokens.append(cg.estimate_tokens(json.loads(body)["messages"]))
        reply = json.dumps({"choices": [{"message": {"role": "assistant",
                                                     "content": "ok"},
                                         "finish_reason": "stop"}]}).encode()

        async def _stream():
            yield reply
        stub_seconds.append(time.perf_counter() - started)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=_stream())

    cg._http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy = httpx.AsyncClient(transport=httpx.ASGITransport(app=cg.app),
                              base_url="http://proxy")
    history = [{"role": "system", "content": "You are a coding assistant."}]
    sent_tokens, added_ms = [], []
    try:
        for i in range(turns):
            history.extend(_turn(rng, i)[:-1])
            sent_tokens.append(cg.estimate_tokens(history))
            before = len(stub_seconds)
            started = time.perf_counter()
            resp = await proxy.post("/v1/chat/completions", json={
                "model": "bench", "stream": False, "messages": history})
            wall = time.perf_counter() - started
            if resp.status_code != 200:
                raise SystemExit("turn %d: proxy answered %d" % (i, resp.status_code))
            stub = stub_seconds[before] if len(stub_seconds) > before else 0.0
            added_ms.append((wall - stub) * 1000.0)
            # The client keeps its OWN full history; it never sees the compaction.
            history.append({"role": "assistant", "content": _text(rng, 300, 3000)})
    finally:
        await proxy.aclose()
        await cg._http_client.aclose()
    return sent_tokens, received_tokens, added_ms


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--num-ctx", type=int, default=16384,
                    help="window the proxy budgets against (default 16384)")
    args = ap.parse_args(argv)
    if args.turns < 1:
        ap.error("--turns must be at least 1")

    tmp = tempfile.mkdtemp(prefix="cg-bench-")
    try:
        # Set BEFORE import: the proxy reads its config at import time, and a
        # .env beside it must not change what this benchmark measures.
        os.environ.update({
            "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
            "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"),
            "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "version.json"),
            "GUARDIAN_UPSTREAM_URL": "http://stub.invalid/v1",
            "GUARDIAN_NUM_CTX": str(args.num_ctx),
            "GUARDIAN_RESERVE_OUTPUT": str(args.num_ctx // 8),
            "GUARDIAN_COMPACT_THRESHOLD": "0.85",
            "GUARDIAN_KEEP_RECENT_MESSAGES": "",
            "GUARDIAN_KEEP_RECENT_FRACTION": "0.20",
            "GUARDIAN_CHARS_PER_TOKEN": "3.5",
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

        sent, received, added = asyncio.run(_run(cg, args.turns, args.seed))
        print("turns=%d tokens_before=%d tokens_after=%d compactions=%d "
              "p50_added_ms=%.2f p95_added_ms=%.2f"
              % (args.turns, sum(sent), sum(received),
                 cg._state["compactions_performed"],
                 _percentile(added, 50), _percentile(added, 95)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
