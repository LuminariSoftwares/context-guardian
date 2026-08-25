"""
context_guardian.py
=====================
*** VERIFIED WORKING 2026-08-12 -- see "VERIFICATION RESULT" below. ***

Built in response to a real, repeated, first-hand symptom: OpenClaude
hitting "token limit reached", refusing to continue, and losing the whole
session -- with no compaction attempt in between. OpenClaude (confirmed to
be Gitlawb/openclaude, NOT Anthropic's Claude Code) does have a real
`/compact` command (confirmed via its own /help output), but it is a
manual, user-invoked action -- nothing found in its config or behavior
suggests it fires itself automatically as the context fills. That gap
between "the capability technically exists" and "it never runs unless you
remember to type it" is exactly what produced the original hard-stop
symptom. This script is the fallback TJ asked for: a small proxy that sits
in front of Headroom, watches the running token count itself, and forces a
compaction BEFORE the real backend ever has a chance to hard-error --
automating what `/compact` would do manually, so it can't be forgotten.

VERIFICATION RESULT (2026-08-12): Passthrough test passed after fixing a
real bug (see FIXED BUG note below). Forced low-threshold test
(NUM_CTX=2000, THRESHOLD=0.5) triggered a genuine compaction: 11 messages
-> 10, ~1851 -> ~1626 estimated tokens, logged to
logs/context_guardian_log.json, and the model's next response correctly
referenced content from the summarized portion -- proving the summary
carried real information through, not just a token-count reduction. Now
wired into luminari_launch.bat and start_openclaude.py in place of a
direct-to-Headroom connection.

FIXED BUG (2026-08-12): the original proxy() handler opened its httpx
client with `async with httpx.AsyncClient() as client:`, which closed the
client (and its connections) the moment the function returned -- but
StreamingResponse doesn't actually read the upstream body until AFTER the
function returns, so every single request through /v1/{path} failed with
a generic "Internal Server Error" (confirmed live: the passthrough test
failed this way before the fix). Replaced with one long-lived
httpx.AsyncClient created at app startup and reused for every request,
with each individual streamed response closed via a BackgroundTask once
Starlette finishes sending it, instead of tearing down the shared client.

FIXED BUG #2 (2026-08-12): the shared httpx.AsyncClient() above was
created with NO timeout override, so it silently inherited httpx's
default -- 5 seconds per read. Fine for a trivial "say hi" test, fatal for
a real task: gpt-oss:20b is a reasoning/"thinking" model, and any request
that spends more than 5 seconds thinking before the first output token
streams out trips httpx.ReadTimeout, which Guardian returns as a 500.
Confirmed live: asking OpenClaude to write a real script produced a
ReadTimeout, OpenClaude retried ~10 times over 4+ minutes hitting the same
5-second wall every attempt, then gave up with "Provider is temporarily
unavailable." Fixed by giving the shared client a generous, LLM-appropriate
timeout instead of the tiny generic-HTTP-API default.

WHERE THIS SITS IN THE CHAIN (this is a new link, not a replacement):
  OpenClaude (Claude Code)
      -> Context Guardian   (this script,  port 8786)   <-- NEW
      -> Headroom proxy     (already exists, port 8787)
      -> Ollama              (already exists, port 11434)

To wire it in: change OPENAI_BASE_URL in luminari_launch.bat and
start_openclaude.py from "http://localhost:8787/v1" to
"http://localhost:8786/v1". Headroom keeps doing exactly what it already
does (compression) -- this just adds a layer in front of it that watches
cumulative token usage and, before the real ceiling, rewrites the outgoing
request to fold older turns into a summary instead of letting the raw
request grow until Ollama rejects it outright.

WHAT THIS DOES NOT DO:
- It does not touch Headroom's own compression at all -- forwards to it
  as-is once it's decided whether to compact first.
- It does not make Claude Code's own "x/128k" counter accurate after a
  compaction happens -- Claude Code doesn't know this proxy exists, so its
  own token tally will drift from reality post-compaction. What matters is
  that the session keeps working instead of hard-stopping; the displayed
  counter being slightly wrong afterward is a known, accepted tradeoff of
  doing this invisibly at the proxy layer rather than inside Claude Code
  itself (which isn't something we can modify).
- It is NOT a tokenizer-accurate counter. Token count is estimated from
  character length (a conservative ~3.5 chars/token). It is NOT corrected
  against real `usage.total_tokens` / `prompt_eval_count` figures -- an
  earlier version of this file said it was; that was never implemented
  whenever a backend response actually includes them -- Ollama's OpenAI-
  compatible endpoint often does. Treat the estimate as a safety-margin
  trigger, not a precise count.

REQUIRED BEFORE RUNNING:
  pip install fastapi uvicorn httpx   (into luminari_env, not globally --
  matches the project's one-venv-per-app rule)

HOW TO TEST BEFORE TRUSTING IT (same discipline as scrape_etsy_trends.py):
  1. Start Ollama and Headroom as normal (start_ollama.py, start_headroom.py).
  2. Run this script directly:  python context_guardian.py
  3. Point a single manual request at it instead of the real OpenClaude:
       curl http://localhost:8786/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"gpt-oss:20b\",\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}]}"
     Confirm you get a normal response back (proves passthrough works)
     before testing compaction specifically.
  4. Check GET http://localhost:8786/guardian/stats for the running token
     estimate and compaction count.
  5. Force a compaction test: temporarily set GUARDIAN_NUM_CTX and
     GUARDIAN_COMPACT_THRESHOLD low (e.g. NUM_CTX=2000, THRESHOLD=0.5) and
     send a conversation with several long messages -- confirm a compaction
     log entry appears in logs/context_guardian_log.json and the request
     that actually reaches Headroom/Ollama is smaller than what was sent in.
  6. Only after that, point luminari_launch.bat's OPENAI_BASE_URL at this
     proxy and test with a real OpenClaude session.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# --- Configuration (env-overridable, sensible defaults for the current 16GB card) ---
GUARDIAN_PORT = int(os.environ.get("GUARDIAN_PORT", "8786"))
GUARDIAN_HOST = os.environ.get("GUARDIAN_HOST", "127.0.0.1")
UPSTREAM_URL = os.environ.get("GUARDIAN_UPSTREAM_URL", "http://localhost:8787/v1")  # Headroom, not Ollama directly
NUM_CTX = int(os.environ.get("GUARDIAN_NUM_CTX", "32768"))  # keep in sync with OLLAMA_CONTEXT_LENGTH
COMPACT_THRESHOLD = float(os.environ.get("GUARDIAN_COMPACT_THRESHOLD", "0.85"))
# RESERVE_OUTPUT (added 2026-08-21) -- the defect this fixes:
# COMPACT_THRESHOLD is a fraction of the WHOLE window, and the whole window has
# to hold the input AND everything the model generates. gpt-oss:20b is a
# REASONING model: its thinking tokens come out of the same budget as its reply.
# At 0.85 the proxy happily served requests occupying 27,853 of 32,768, leaving
# 4,915 tokens for think + tool-call + answer. Measured that night: 12 of 38
# requests arrived over the ceiling entirely (max 38,026) and post-compaction
# headroom fell as low as 6,017. The observed behaviour was a clean gradient --
# room to think -> correct tool call; squeezed -> fall back to Grep (a built-in,
# no schema to format); very squeezed -> Grep plus a fabricated answer.
# So: reserve an explicit output budget and compact against what is LEFT.
RESERVE_OUTPUT = int(os.environ.get("GUARDIAN_RESERVE_OUTPUT", "8192"))
KEEP_RECENT_MESSAGES = int(os.environ.get("GUARDIAN_KEEP_RECENT_MESSAGES", "8"))
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("GUARDIAN_CHARS_PER_TOKEN", "3.5"))
# ^ backported 2026-08-20 from the public repo (ClaudeRepos/context-guardian),
# which had this env override and this copy did not. The two files have drifted
# in BOTH directions -- see the roadmap note. Over-reads by ~14% vs tiktoken on
# a real MCP tool payload, which is the intended conservative direction.
# Count the `tools` array against the budget. Default ON, because it IS in the
# request and the model IS charged for it. Set GUARDIAN_COUNT_TOOLS=0 to get
# the old messages-only behaviour back if this ever needs bisecting.
COUNT_TOOLS = os.environ.get("GUARDIAN_COUNT_TOOLS", "1") not in ("0", "false", "False")

# Upstream timeouts. Backported from the public repo at 0.2.0, which made these
# env-tunable while this copy hardcoded them at the call site. Same defaults --
# httpx's own default is ~5s, which is a sane generic-HTTP number and a wrong
# one for a local model that thinks for minutes before its first byte.
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("GUARDIAN_UPSTREAM_TIMEOUT", "600.0"))
UPSTREAM_CONNECT_TIMEOUT_SECONDS = float(
    os.environ.get("GUARDIAN_UPSTREAM_CONNECT_TIMEOUT", "10.0"))

# Where compacted spans are evicted to. Compaction used to DESTROY the folded
# messages and keep a 500-character preview in the log; now the full span goes
# to disk first, so compaction is lossy in context but lossless on disk.
# Stolen from Continuous-Claude-v3's "compound, don't compact" -- minus its
# PostgreSQL + pgvector + daemon, which a single-user stack does not need.
SPAN_DIR = Path(os.environ.get(
    "GUARDIAN_SPAN_DIR",
    r"F:\AI\LuminariStudio\logs\guardian_spans"))
KEEP_SPANS = int(os.environ.get("GUARDIAN_KEEP_SPANS", "500"))
# How many of Guardian's OWN previous summaries stay in the window.
#
# Guardian's summary is inserted with role "system", and the old code kept
# EVERY system message forever -- so each compaction added a permanent summary
# that was itself never compacted. Over a long session the summaries crowd out
# the conversation they exist to make room for. Nobody noticed because it only
# shows up after several compactions in one session.
#
# Retired summaries are not discarded: they are folded into the next span, so
# their text is on disk and reachable through guardian_recall.py.
# In-window count settles at KEEP_SUMMARIES + 1 (the kept ones plus the new).
KEEP_SUMMARIES = int(os.environ.get("GUARDIAN_KEEP_SUMMARIES", "1"))
GUARDIAN_SUMMARY_MARKER = "[Context Guardian auto-compaction"
# One id per proxy process. Guardian cannot see OpenClaude's session id -- it
# only sees HTTP requests -- so this groups spans by proxy run, which is the
# closest honest approximation. Do not label it "session".
RUN_ID = time.strftime("%Y%m%d-%H%M%S")

LOG_PATH = Path(os.environ.get(
    "GUARDIAN_LOG_PATH",
    r"F:\AI\LuminariStudio\logs\context_guardian_log.json",
))

logging.basicConfig(level=logging.INFO, format="[ContextGuardian] %(message)s")
log = logging.getLogger("context_guardian")

app = FastAPI(title="Context Guardian")

# Running estimate of tokens "in flight" for the current conversation.
# This is intentionally process-local and best-effort -- it resets if the
# proxy restarts.
#
# CORRECTED 2026-08-20: an earlier comment here claimed this "self-corrects
# toward real numbers whenever a backend response includes actual usage
# figures". It does not, and never did -- no code reads usage.total_tokens or
# prompt_eval_count anywhere in this file. The claim was documentation of an
# intention, sitting in the place where a reader looks for a fact.
_state = {
    "last_known_total_tokens": 0,
    "compactions_performed": 0,
    "requests_seen": 0,
}

# One long-lived client for the life of the app, NOT a per-request
# "async with" client. A per-request client that gets closed when the
# request-handling function returns closes its connections before
# StreamingResponse ever gets a chance to actually read the streamed
# upstream body -- that was the bug that produced "Internal Server Error"
# on every single call through /v1/{path}, including the plain passthrough
# test. /guardian/stats worked because it's a separate route that never
# touches this client at all.
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _http_client
    # httpx's bare default (5s connect/read/write/pool) is sized for
    # ordinary REST APIs, not local LLM inference -- a "thinking" model can
    # easily sit silent for well over 5s before its first output token.
    # Connect stays tight (10s -- Headroom is local, if it's not answering
    # at all something's actually wrong and we want to know fast); read is
    # generous (10 min -- covers slow local generation on a 20B model
    # without hanging forever if something's genuinely stuck).
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS,
                              connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS)
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http_client is not None:
        await _http_client.aclose()


def estimate_tool_tokens(payload: Dict[str, Any]) -> int:
    """What the `tools` array costs.

    THIS IS THE BIT THAT WAS MISSING. Until 2026-08-20 this proxy estimated the
    conversation from `messages` alone and never looked at `tools` -- so it was
    blind to the single largest fixed cost in every request.

    Measured the same day with scripts/mcp_context_cost.py: the five MCP servers
    are 21,995 tokens, 67.1% of a 32,768 window, BEFORE the first user message.
    A guardian that starts counting at zero when the request already contains
    21,995 tokens does not fire late -- it fires at the wrong time entirely, and
    on the 8,192-ctx devstral path the tools alone are 2.7x the whole window.

    Counts `tools` and the legacy `functions` field, both by serialised length.
    """
    if not COUNT_TOOLS:
        return 0
    chars = 0
    for key in ("tools", "functions"):
        blob = payload.get(key)
        if blob:
            try:
                chars += len(json.dumps(blob, ensure_ascii=False))
            except (TypeError, ValueError):
                chars += len(str(blob))
    return int(chars / CHARS_PER_TOKEN_ESTIMATE)


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough, conservative token estimate from raw message text length.

    Messages only. `tools` is counted separately by estimate_tool_tokens() --
    kept apart on purpose so the log can show which half of the budget is
    conversation and which half is tool definitions you could simply not load.
    """
    total_chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Some OpenAI-style payloads use content blocks instead of a plain string.
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    total_chars += len(block["text"])
    return int(total_chars / CHARS_PER_TOKEN_ESTIMATE)


def is_guardian_summary(m: Dict[str, Any]) -> bool:
    """A summary this proxy inserted on an earlier turn.

    Detected by content prefix rather than a custom key: this payload is
    forwarded verbatim to an upstream OpenAI-compatible server, and a
    non-standard message key is a good way to get a 400 from something strict.
    """
    return (m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m["content"].lstrip().startswith(GUARDIAN_SUMMARY_MARKER))


def partition_messages(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Split a conversation into what is kept, retired, and summarised.

    Pure -- no HTTP, no clock, no globals beyond config. Split out of
    maybe_compact() precisely so it can be tested directly: the accumulation
    bug lived in three lines of list comprehension that could only be reached
    through an async function that needed a live backend to call.

    Returns keys: real_system, kept_summaries, retired_summaries,
    to_summarize, to_keep.
    """
    system_all = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    real_system = [m for m in system_all if not is_guardian_summary(m)]
    prior = [m for m in system_all if is_guardian_summary(m)]

    keep_n = max(0, KEEP_SUMMARIES)
    kept_summaries = prior[len(prior) - keep_n:] if keep_n else []
    retired_summaries = prior[:len(prior) - len(kept_summaries)]

    to_summarize = non_system[:-KEEP_RECENT_MESSAGES] if KEEP_RECENT_MESSAGES else list(non_system)
    to_keep = non_system[-KEEP_RECENT_MESSAGES:] if KEEP_RECENT_MESSAGES else []

    # Retired summaries lead the span so the archive reads chronologically.
    return {"real_system": real_system, "kept_summaries": kept_summaries,
            "retired_summaries": retired_summaries,
            "to_summarize": retired_summaries + to_summarize,
            "to_keep": to_keep}


def write_span(messages: List[Dict[str, Any]], summary: str,
               index: int) -> Optional[str]:
    """Persist the messages about to be folded away, BEFORE folding them.

    Called on the request path, so it must never raise. A failed span write
    means we lose the archive, which is bad; letting it raise would mean losing
    the request, which is worse. Returns the path written, or None -- and None
    is recorded in the compaction log so a missing span is visible rather than
    silently assumed to exist.
    """
    try:
        d = SPAN_DIR / RUN_ID
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{index:04d}.json"
        payload = {
            "run_id": RUN_ID,
            "index": index,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "num_ctx": NUM_CTX,
            "message_count": len(messages),
            "summary": summary,
            "messages": messages,
        }
        tmp = path.with_suffix(".part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        prune_spans()
        return str(path)
    except Exception as exc:                               # noqa: BLE001
        log.warning("could not write span %d: %s. The compaction still "
                    "proceeds; this span is LOST, not deferred.", index, exc)
        return None


def prune_spans() -> None:
    """Housekeeping only -- never allowed to break a request."""
    try:
        files = sorted(SPAN_DIR.glob("*/*.json"))
        for old_file in files[:-KEEP_SPANS] if len(files) > KEEP_SPANS else []:
            old_file.unlink(missing_ok=True)
    except Exception:                                      # noqa: BLE001
        pass


def log_event(event: Dict[str, Any]) -> None:
    """Append one JSON line to the compaction log. Never raises -- logging
    must not be able to break the actual request path."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:  # pragma: no cover -- logging must be best-effort
        log.warning("Failed to write log entry (continuing anyway): %s", exc)


async def summarize_older_messages(
    client: httpx.AsyncClient, model: str, older_messages: List[Dict[str, Any]]
) -> Optional[str]:
    """Ask the same backend to condense the older portion of the
    conversation. Returns None on any failure -- caller must fail open
    (forward the original, uncompacted request) rather than silently drop
    history on a broken summarization call."""
    condense_prompt = (
        "Condense the following conversation history into a concise but "
        "complete summary. Preserve: concrete facts and decisions made, "
        "file paths and code/config changes discussed, unresolved "
        "questions or TODOs, and any numbers/settings that were agreed on. "
        "Do not editorialize or add commentary -- just compress. Write it "
        "as plain prose, not a transcript.\n\n---\n\n"
    )
    transcript = "\n\n".join(
        f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
        for m in older_messages
        if isinstance(m.get("content", ""), str)
    )
    try:
        resp = await client.post(
            f"{UPSTREAM_URL}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": condense_prompt + transcript}],
                "stream": False,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        log.error("Summarization call failed, will fail OPEN (no compaction this turn): %s", exc)
        return None


async def maybe_compact(client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a (possibly rewritten) payload. Never returns something with
    LESS information than the original unless the summarization call
    actually succeeded -- see fail-open comment above."""
    messages = payload.get("messages", [])
    _state["requests_seen"] += 1

    message_tokens = estimate_tokens(messages)
    tool_tokens = estimate_tool_tokens(payload)
    estimated = message_tokens + tool_tokens
    _state["last_known_total_tokens"] = estimated
    _state["last_tool_tokens"] = tool_tokens
    _state["last_message_tokens"] = message_tokens
    # Compact against the budget the INPUT is actually allowed to occupy, which
    # is the window minus the space the model needs to think and answer. Take
    # whichever limit is stricter so lowering COMPACT_THRESHOLD still works.
    threshold_tokens = min(int(NUM_CTX * COMPACT_THRESHOLD),
                           max(1, NUM_CTX - RESERVE_OUTPUT))

    # Tool definitions are a FLOOR, not a thing compaction can reduce -- this
    # proxy can only summarise messages. Say so out loud the first time a
    # session's tool payload is seen, so a user staring at constant compaction
    # is looking at the real cause instead of blaming the summariser.
    if tool_tokens and tool_tokens != _state.get("logged_tool_tokens"):
        _state["logged_tool_tokens"] = tool_tokens
        share = tool_tokens / NUM_CTX if NUM_CTX else 0
        log.info("Tool definitions in this request: ~%d tokens (%.1f%% of %d). "
                 "Compaction CANNOT reduce this -- only loading fewer MCP "
                 "servers can.", tool_tokens, share * 100, NUM_CTX)
        log_event({"event": "tool_budget", "tool_tokens": tool_tokens,
                   "num_ctx": NUM_CTX, "share_of_ctx": round(share, 3),
                   "note": "fixed floor; not reducible by compaction"})
        usable = NUM_CTX - RESERVE_OUTPUT - tool_tokens
        log.info("Budget for conversation after tools and reserved output: "
                 "~%d tokens (window %d - reserve %d - tools %d).",
                 usable, NUM_CTX, RESERVE_OUTPUT, tool_tokens)
        log_event({"event": "budget", "num_ctx": NUM_CTX,
                   "reserve_output": RESERVE_OUTPUT, "tool_tokens": tool_tokens,
                   "usable_for_messages": usable})
        if usable < 4000:
            log.warning("ONLY ~%d TOKENS LEFT FOR THE CONVERSATION after tool "
                        "definitions (%d) and reserved output (%d). Expect the "
                        "model to skip MCP tools and fall back to built-ins. "
                        "Load fewer MCP servers -- compaction cannot help here.",
                        usable, tool_tokens, RESERVE_OUTPUT)
        if tool_tokens >= NUM_CTX:
            log.warning("TOOL DEFINITIONS ALONE (%d) EXCEED THE WHOLE CONTEXT "
                        "WINDOW (%d). Nothing this proxy does can fix that. "
                        "Launch with --strict-mcp-config and a smaller server "
                        "set.", tool_tokens, NUM_CTX)

    if estimated < threshold_tokens or len(messages) <= KEEP_RECENT_MESSAGES + 1:
        return payload

    log.info(
        "Estimated %d tokens >= threshold %d (%.0f%% of %d) -- compacting.",
        estimated, threshold_tokens, COMPACT_THRESHOLD * 100, NUM_CTX,
    )

    part = partition_messages(messages)
    system_msgs = part["real_system"] + part["kept_summaries"]
    to_summarize = part["to_summarize"]
    to_keep = part["to_keep"]

    if not to_summarize:
        return payload  # nothing meaningful to compact

    if part["retired_summaries"]:
        log.info("Retiring %d older Guardian summary/summaries into this span "
                 "(keeping %d in-window).", len(part["retired_summaries"]),
                 len(part["kept_summaries"]))

    summary = await summarize_older_messages(client, payload.get("model", ""), to_summarize)
    span_path = None
    if summary is None:
        # Fail open: forward the original request untouched rather than
        # guess at a destructive truncation.
        return payload

    # Evict to disk BEFORE the messages are replaced. If this ran after, a crash
    # between the two would lose the span with nothing to show it existed.
    span_path = write_span(to_summarize, summary, _state["compactions_performed"] + 1)

    # Name the span file IN the summary the model receives. This is the whole
    # difference between an archive and a recovery path: Guardian is a proxy and
    # cannot hand the model a tool, but every agentic client it fronts already
    # has file access. Telling it WHERE the detail went costs ~25 tokens and
    # turns "that context is gone" into "that context is one Read away".
    #
    # The pointer is only added when the span actually wrote. Pointing at a file
    # that does not exist would be worse than saying nothing -- it would send
    # the model looking, and teach it the pointer cannot be trusted.
    pointer = ""
    if span_path:
        # Tell it the SIZE, and tell it to search rather than read whole.
        # A span is by definition bigger than the window had room for -- an
        # unqualified "read that file" invites the model to reload the exact
        # payload compaction just evicted, blowing the window on the recovery.
        # The size figure is what lets it decide; the search hint is what keeps
        # it cheap.
        span_tokens = estimate_tokens(to_summarize)
        n_spans = _state["compactions_performed"] + 1
        # Name the DIRECTORY, not just this file. Older summaries are retired
        # from the window now, so their individual pointers go with them -- one
        # pointer at the whole archive is what keeps the earlier spans findable.
        pointer = (f"\n\n[Full text of those {len(to_summarize)} messages "
                   f"(~{span_tokens} tokens) saved at {span_path}. "
                   f"{n_spans} span(s) so far this session, all under "
                   f"{SPAN_DIR / RUN_ID}. They are LARGER than the room that "
                   f"was freed -- do not read them whole. Search instead: "
                   f"`python scripts/guardian_recall.py \"<term>\"`.]")

    summary_message = {
        "role": "system",
        "content": (
            "[Context Guardian auto-compaction — earlier conversation "
            f"({len(to_summarize)} messages) condensed to stay within the "
            "context window:]\n\n" + summary + pointer
        ),
    }

    new_messages = system_msgs + [summary_message] + to_keep
    new_payload = dict(payload)
    new_payload["messages"] = new_messages

    _state["compactions_performed"] += 1
    log_event({
        "event": "compaction",
        "messages_before": len(messages),
        "messages_after": len(new_messages),
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimate_tokens(new_messages) + tool_tokens,
        "message_tokens_before": message_tokens,
        "message_tokens_after": estimate_tokens(new_messages),
        "tool_tokens": tool_tokens,
        "span_file": span_path,
        "span_written": span_path is not None,
        "summary_preview": summary[:500],
    })
    log.info(
        "Compacted %d messages -> %d. Estimated tokens %d -> %d "
        "(of which %d is tool definitions, unchanged).",
        len(messages), len(new_messages), estimated,
        estimate_tokens(new_messages) + tool_tokens, tool_tokens,
    )
    return new_payload


@app.get("/guardian/stats")
async def stats():
    return JSONResponse({
        "num_ctx": NUM_CTX,
        "compact_threshold": COMPACT_THRESHOLD,
        "reserve_output": RESERVE_OUTPUT,
        "effective_input_budget": min(int(NUM_CTX * COMPACT_THRESHOLD),
                                      max(1, NUM_CTX - RESERVE_OUTPUT)),
        "keep_recent_messages": KEEP_RECENT_MESSAGES,
        "count_tools": COUNT_TOOLS,
        "upstream_timeout_seconds": UPSTREAM_TIMEOUT_SECONDS,
        "upstream_connect_timeout_seconds": UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        # Every knob that changes BEHAVIOUR belongs here. Checking whether a
        # running proxy has a given fix should be one curl, not an archaeology
        # dig through file mtimes -- which is exactly what it took on
        # 2026-08-21 to discover a process 51 minutes behind the source.
        "keep_summaries": KEEP_SUMMARIES,
        "keep_spans": KEEP_SPANS,
        "code_version": "0.3.0-tools+spans+bounded-summaries+env-timeouts",
        "run_id": RUN_ID,
        "span_dir": str(SPAN_DIR),
        "upstream": UPSTREAM_URL,
        **_state,
        "note": ("last_known_total_tokens now INCLUDES the tools array. "
                 "last_tool_tokens is the part compaction cannot touch."),
    })


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    """Generic passthrough for everything except chat/completions, which
    gets the compaction check. Streaming responses are forwarded byte-for-
    byte without buffering -- compaction only ever touches the OUTBOUND
    request, never the response stream."""
    body_bytes = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}

    client = _http_client
    assert client is not None, "http client not initialized -- startup event did not run"

    if path == "chat/completions" and request.method == "POST" and body_bytes:
        try:
            payload = json.loads(body_bytes)
        except json.JSONDecodeError:
            payload = None

        if payload is not None:
            payload = await maybe_compact(client, payload)
            body_bytes = json.dumps(payload).encode("utf-8")

    upstream_url = f"{UPSTREAM_URL}/{path}"

    req = client.build_request(
        request.method, upstream_url, content=body_bytes, headers=headers,
        params=dict(request.query_params),
    )
    upstream_resp = await client.send(req, stream=True)

    # BackgroundTask closes THIS response (not the shared client) once
    # Starlette has finished streaming it back to the caller -- releases
    # the connection back to the pool without tearing down the client
    # every other request depends on.
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower() != "content-length"},
        background=BackgroundTask(upstream_resp.aclose),
    )


if __name__ == "__main__":
    import uvicorn
    log.info(
        "Starting Context Guardian on port %d, forwarding to %s (num_ctx=%d, threshold=%.2f)",
        GUARDIAN_PORT, UPSTREAM_URL, NUM_CTX, COMPACT_THRESHOLD,
    )
    # BIND 127.0.0.1, NOT 0.0.0.0 (fixed 2026-08-22, bandit B104).
    # This proxy fronts Headroom -> Ollama with NO authentication. Bound to
    # 0.0.0.0 it accepted /v1/chat/completions from anyone on the LAN: free use
    # of this GPU, and a path straight to the local model. Verified before
    # changing it -- n8n runs NATIVE ("n8n Start.bat": port 5679, native), and
    # every caller of :8786 (OpenClaude, discord_approval_bot) uses localhost.
    # Nothing containerised needs it, so loopback costs nothing.
    # If a container ever does need it, set GUARDIAN_HOST deliberately rather
    # than reverting this line.
    uvicorn.run(app, host=GUARDIAN_HOST, port=GUARDIAN_PORT)
