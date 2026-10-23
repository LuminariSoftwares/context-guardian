#!/usr/bin/env python3
"""Does a summary that is extended turn after turn still remember the start?

With GUARDIAN_SUMMARY_MEMO on, a compaction that evicts more history than the
last one summarises (earlier summary + the new messages) instead of the whole
range again. Over many turns, facts from the first turns can erode. The test
suite cannot see that: its stub summariser returns a fixed text. This script
measures it against a real model.

It replays a scripted conversation through the real proxy, in this process,
against the OpenAI-compatible backend you name. The first 3 turns plant 5
distinctive facts; the rest grow the history until the proxy compacts. After
every compaction it checks which facts the summary still carries. It runs the
conversation twice: once with the memo on, once with GUARDIAN_SUMMARY_MEMO=0.

    python scripts/memo_drift_probe.py --upstream http://127.0.0.1:11434/v1 --model qwen3:8b
    python scripts/memo_drift_probe.py --upstream http://127.0.0.1:8000/v1 --model my-model --turns 40

Output: one line per compaction, then a table per run: facts kept (in the
last summary, and the fewest in any summary), summariser calls, and the
characters sent to the summariser.

Safe to run anywhere:
  - it talks only to --upstream (no update check, no proxy from the
    environment, window detection off, nothing else on the network);
  - everything the proxy writes (spans, log, receipts) goes to a temporary
    directory that is removed on exit; no .env and no GUARDIAN_* setting of
    yours is read;
  - it sends nothing but the scripted conversation below, so nothing of
    yours reaches the backend.

Expect 2 x --turns chat calls plus the summariser calls; on a local model that
is minutes, not seconds.
"""
import argparse
import asyncio
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

# Five facts no model knows already, each checked by a pattern loose enough to
# accept a paraphrase. All of them are invented.
FACTS = (
    ("codename", "The project's codename is BLUE HERON-47.", r"heron"),
    ("port", "The deploy server listens on port 9417.", r"9417"),
    ("reviewer", "The lead reviewer is Okonkwo Vasquez.", r"okonkwo|vasquez"),
    ("freeze", "The release freeze starts on the 14th of March.",
     r"14(th)?\s+(of\s+)?march|march\s+14"),
    ("config", "The settings file lives at /srv/quill/settings.toml.", r"quill|settings\.toml"),
)
PLANTED = ((0, 1), (2, 3), (4,))          # which facts turns 0, 1 and 2 carry

SYSTEM = "You are a concise senior engineer helping with a code review. Answer in at most three sentences."

# None: a real connection to --upstream. tests/test_memo_drift_probe.py sets a
# stub transport here to check the script's mechanics; that says nothing about
# summary quality, which only a real model can show.
UPSTREAM_TRANSPORT = None

MODULES = ("parser", "scheduler", "cache", "router", "billing", "uploader", "indexer", "auth", "metrics", "queue")


def user_turn(i):
    """Turn i's user message: facts in the first three, then a code excerpt
    to review, which is what grows the history."""
    if i < len(PLANTED):
        facts = " ".join(FACTS[k][1] for k in PLANTED[i])
        return ("Before we start, some project details to keep in mind for the whole session. %s "
                "Please confirm you have noted this." % facts)
    name = MODULES[i % len(MODULES)]
    lines = ["def %s_step_%d(items, limit=%d):" % (name, j, 10 + j) +
             "\n    out = [x for x in items if x.get('%s_%d') is not None]" % (name, j) +
             "\n    return out[:limit]" for j in range(18)]
    return ("Turn %d. Review this excerpt of %s.py and name the one change you would make first.\n\n%s"
            % (i, name, "\n\n".join(lines)))


def facts_in(text):
    low = (text or "").lower()
    return [name for name, _, pattern in FACTS if re.search(pattern, low)]


def _isolated_env(tmp, args, memo):
    """Every GUARDIAN_* setting this run uses, and none of the caller's."""
    for k in [k for k in os.environ if k.startswith("GUARDIAN_")]:
        del os.environ[k]
    os.environ.update({
        "GUARDIAN_UPSTREAM_URL": args.upstream.rstrip("/"),
        "GUARDIAN_DATA_DIR": tmp,
        "GUARDIAN_SPAN_DIR": os.path.join(tmp, "spans"),
        "GUARDIAN_LOG_PATH": os.path.join(tmp, "log.json"),
        "GUARDIAN_RECEIPTS_PATH": os.path.join(tmp, "receipts.jsonl"),
        "GUARDIAN_VERSION_CACHE": os.path.join(tmp, "version.json"),
        "GUARDIAN_VERSION_CHECK": "0",
        "GUARDIAN_NO_UPDATE_CHECK": "1",
        "GUARDIAN_SEAL_ANCHOR_PATH": "",
        "GUARDIAN_CAPTURE": "off",
        "GUARDIAN_NUM_CTX": str(args.num_ctx),
        "GUARDIAN_RESERVE_OUTPUT": str(args.max_tokens * 2),
        "GUARDIAN_COMPACT_THRESHOLD": "0.85",
        "GUARDIAN_KEEP_RECENT_MESSAGES": "",
        "GUARDIAN_DETECT_CONTEXT": "0",
        "GUARDIAN_OLLAMA_CONTEXT": "0",
        "GUARDIAN_CALIBRATE": "0",
        "GUARDIAN_INJECT_STREAM_OPTIONS": "0",
        "GUARDIAN_DRY_RUN": "0",
        "GUARDIAN_VERBOSE": "0",
        "GUARDIAN_SUMMARY_TIMEOUT": str(args.timeout),
        "GUARDIAN_SUMMARY_MEMO": "32" if memo else "0",
    })


def _load_proxy():
    # A .env beside the proxy or in a data directory must not change the run:
    # the proxy loads one through python-dotenv, so hand it a loader that loads
    # nothing. Then a fresh module, since config is read at import time.
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: False)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


def _contents(body):
    msgs = body.get("messages") or []
    return sum(len(m.get("content")) for m in msgs if isinstance(m, dict) and isinstance(m.get("content"), str))


async def run(args, memo):
    import httpx
    import logging

    label = "memo on" if memo else "memo off"
    tmp = tempfile.mkdtemp(prefix="cg-drift-")
    try:
        _isolated_env(tmp, args, memo)
        cg = _load_proxy()
        logging.getLogger("context_guardian").setLevel(logging.ERROR)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        seen = {"calls": 0, "input_chars": 0, "summary": None}

        async def watch(request):
            try:
                body = json.loads(request.content or b"{}")
            except ValueError:
                return
            text = json.dumps(body)
            if "BEGIN_TRANSCRIPT" in text:
                seen["calls"] += 1
                seen["input_chars"] += _contents(body)
                return
            for m in body.get("messages") or []:
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str) and c.startswith("[Context Guardian auto-compaction"):
                    seen["summary"] = c

        headers = {"authorization": "Bearer " + args.api_key} if args.api_key else {}
        cg._http_client = httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10.0),
                                            trust_env=False, event_hooks={"request": [watch]},
                                            transport=UPSTREAM_TRANSPORT)
        proxy = httpx.AsyncClient(transport=httpx.ASGITransport(app=cg.app), base_url="http://127.0.0.1:8786",
                                  timeout=httpx.Timeout(args.timeout * 2 + 30))
        history = [{"role": "system", "content": SYSTEM}]
        rows = []
        try:
            for i in range(args.turns):
                history.append({"role": "user", "content": user_turn(i)})
                before = (cg._state["compactions_performed"], cg._state["summaries_generated"],
                          cg._state["summaries_reused"], cg._state["summaries_extended"])
                resp = await proxy.post("/v1/chat/completions", headers=headers, json={
                    "model": args.model, "messages": history, "max_tokens": args.max_tokens,
                    "temperature": 0, "stream": False})
                if resp.status_code != 200:
                    print("[%s] turn %d: the backend answered %d: %s" % (label, i, resp.status_code, resp.text[:300]))
                    break
                reply = cg.extract_assistant_text(resp.json()) or ""
                history.append({"role": "assistant", "content": reply})
                if cg._state["compactions_performed"] > before[0]:
                    source = ("generated" if cg._state["summaries_generated"] > before[1] else
                              "reused" if cg._state["summaries_reused"] > before[2] else
                              "extended" if cg._state["summaries_extended"] > before[3] else "?")
                    kept = facts_in(seen["summary"])
                    rows.append(len(kept))
                    print("[%s] turn %2d  compaction %2d  %-9s  facts %d/%d  %s  summary %d chars"
                          % (label, i, cg._state["compactions_performed"], source, len(kept), len(FACTS),
                             "missing: " + ",".join(n for n, _, _ in FACTS if n not in kept) if len(kept) < len(FACTS)
                             else "all kept", len(seen["summary"] or "")))
        finally:
            await proxy.aclose()
            await cg._http_client.aclose()
        return {"run": label, "compactions": cg._state["compactions_performed"],
                "last": rows[-1] if rows else None, "fewest": min(rows) if rows else None,
                "calls": seen["calls"], "input_chars": seen["input_chars"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def table(results):
    def cell(v):
        return "-" if v is None else "%d/%d" % (v, len(FACTS))
    lines = ["| run | compactions | facts kept (last summary) | facts kept (fewest) | summariser calls "
             "| summariser input chars |", "|---|---:|---:|---:|---:|---:|"]
    for r in results:
        lines.append("| %s | %d | %s | %s | %d | %s |" % (r["run"], r["compactions"], cell(r["last"]),
                                                          cell(r["fewest"]), r["calls"], format(r["input_chars"], ",")))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", required=True,
                    help="your backend's OpenAI-compatible base URL, e.g. http://127.0.0.1:11434/v1")
    ap.add_argument("--model", required=True, help="the model name the backend serves")
    ap.add_argument("--turns", type=int, default=30, help="conversation turns per run (default 30)")
    ap.add_argument("--num-ctx", type=int, default=6144,
                    help="window the proxy budgets against; small, so compactions come early (default 6144)")
    ap.add_argument("--max-tokens", type=int, default=200, help="reply length cap per turn (default 200)")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per backend call (default 600)")
    ap.add_argument("--api-key", default="", help="sent as a bearer token, for a keyed server")
    ap.add_argument("--only", choices=("both", "on", "off"), default="both",
                    help="run the memo on, off, or both (default both)")
    args = ap.parse_args(argv)
    url = urlparse(args.upstream)
    if url.scheme not in ("http", "https") or not url.netloc:
        ap.error("--upstream must be an http:// or https:// URL")
    if args.turns < len(PLANTED) + 1:
        ap.error("--turns must be at least %d" % (len(PLANTED) + 1))

    results = []
    for memo in ((True, False) if args.only == "both" else ((args.only == "on"),)):
        results.append(asyncio.run(run(args, memo)))
    print()
    print(table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
