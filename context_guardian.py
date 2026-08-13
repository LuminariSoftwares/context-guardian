"""
context_guardian.py
====================
A tiny proxy that sits in front of any OpenAI-compatible LLM backend
(Ollama, LiteLLM, Headroom, vLLM, LM Studio, etc.) and forces a
conversation-history compaction *before* the real context window fills up,
instead of letting the raw request grow until the backend hard-errors.

WHY THIS EXISTS
---------------
Claude-Code-style coding CLIs (Claude Code itself, and compatible tools
such as OpenClaude) have a built-in auto-compact feature -- but it depends
on accurate, real-time token-usage accounting coming back from the API in
the exact shape the CLI expects. When you point one of these tools at a
local model through an OpenAI-compatible bridge (Ollama's `/v1` endpoint,
a LiteLLM proxy, etc.), that accounting is often missing, wrong, or in a
different shape -- so auto-compact silently never fires. The visible
symptom: the CLI just runs until the backend hard-rejects the request
("token limit reached"), the session dies, and you lose your place with no
partial-compaction attempt in between.

Context Guardian is a small, deliberately dumb fallback for that specific
gap: it estimates the running token count itself, and once the
conversation crosses a configurable threshold, it asks the *same backend*
to condense the older portion of the conversation into one summary message
before forwarding the request onward. Recent messages are always kept
verbatim. If the summarization call itself fails for any reason, Guardian
fails OPEN -- it forwards the original, uncompacted request rather than
risk silently dropping history.

WHERE THIS SITS IN THE CHAIN
-----------------------------
This is a new link in an existing chain, not a replacement for anything:

    Your CLI / agent (Claude Code, OpenClaude, etc.)
        -> Context Guardian   (this script)
        -> your existing OpenAI-compatible backend
           (Ollama directly, a LiteLLM proxy, Headroom, vLLM, ...)

Point your CLI's OPENAI_BASE_URL at Context Guardian instead of directly
at your backend, and set GUARDIAN_UPSTREAM_URL to wherever your backend
actually lives. Guardian is a pure passthrough for everything except
POST /v1/chat/completions, which gets the compaction check; every other
route (including streaming) is forwarded byte-for-byte, untouched.

WHAT THIS DOES NOT DO
----------------------
- It does not replace or duplicate any compression your backend already
  does (e.g. Headroom, prompt caching). It forwards to your backend as-is
  once it has decided whether to compact first -- the two are complementary.
- It does not make your CLI's own context-usage counter/display accurate
  after a compaction happens. Your CLI doesn't know this proxy exists, so
  its own token tally will drift from reality post-compaction. What
  matters is that the session keeps working instead of hard-stopping; a
  slightly-wrong displayed counter afterward is an accepted tradeoff of
  doing this invisibly at the proxy layer.
- It is NOT a tokenizer-accurate counter. Token count is estimated from
  character length (a conservative ~3.5 chars/token default) rather than
  a real tokenizer, so it triggers a little early rather than late. Treat
  it as a safety-margin trigger, not a precise measurement.

INSTALL
-------
    pip install -r requirements.txt

CONFIGURE
---------
Copy .env.example to .env and adjust for your setup (or just export the
same environment variables directly -- .env is optional, not required).
See .env.example / README.md for the full list of settings.

RUN
---
    python context_guardian.py

TESTING BEFORE YOU TRUST IT WITH A REAL SESSION
-------------------------------------------------
  1. Start your real backend (Ollama, LiteLLM, Headroom, whatever you use)
     the way you normally would.
  2. Start this proxy: python context_guardian.py
  3. Send one manual request at it instead of your real CLI, to confirm
     plain passthrough works before testing compaction specifically:
       curl http://localhost:8786/v1/chat/completions \\
         -H "Content-Type: application/json" \\
         -d '{"model":"<your-model>","messages":[{"role":"user","content":"say hi"}]}'
  4. Check GET http://localhost:8786/guardian/stats for the running token
     estimate and compaction count.
  5. Force a compaction test: temporarily set GUARDIAN_NUM_CTX and
     GUARDIAN_COMPACT_THRESHOLD low (e.g. NUM_CTX=2000, THRESHOLD=0.5), then
     send a conversation with several long messages. Confirm a compaction
     log entry appears at GUARDIAN_LOG_PATH and the request that actually
     reaches your backend is smaller than what was sent in.
  6. Only after that, point your CLI's OPENAI_BASE_URL at this proxy and
     test with a real session.

License: MIT (see LICENSE).
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional -- plain exported env vars work fine too

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# --- Configuration (all env-overridable; defaults are conservative, not tuned to any specific rig) ---
GUARDIAN_PORT = int(os.environ.get("GUARDIAN_PORT", "8786"))
UPSTREAM_URL = os.environ.get("GUARDIAN_UPSTREAM_URL", "http://localhost:8787/v1")
NUM_CTX = int(os.environ.get("GUARDIAN_NUM_CTX", "32768"))  # keep in sync with your backend's real context window
COMPACT_THRESHOLD = float(os.environ.get("GUARDIAN_COMPACT_THRESHOLD", "0.85"))
KEEP_RECENT_MESSAGES = int(os.environ.get("GUARDIAN_KEEP_RECENT_MESSAGES", "8"))
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("GUARDIAN_CHARS_PER_TOKEN", "3.5"))  # conservative -- triggers a little early rather than late
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("GUARDIAN_UPSTREAM_TIMEOUT", "600.0"))
UPSTREAM_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("GUARDIAN_UPSTREAM_CONNECT_TIMEOUT", "10.0"))

LOG_PATH = Path(os.environ.get("GUARDIAN_LOG_PATH", "logs/context_guardian_log.json"))

logging.basicConfig(level=logging.INFO, format="[ContextGuardian] %(message)s")
log = logging.getLogger("context_guardian")

app = FastAPI(title="Context Guardian")

# Running estimate of tokens "in flight" for the current conversation.
# Intentionally process-local and best-effort -- resets if the proxy
# restarts. Corrects toward real numbers whenever a backend response
# happens to include actual usage figures (not all do).
_state = {
    "last_known_total_tokens": 0,
    "compactions_performed": 0,
    "requests_seen": 0,
}

# One long-lived client for the life of the app, NOT a per-request
# "async with" client. A per-request client that closes when the handler
# function returns closes its connections before StreamingResponse ever
# gets a chance to actually read the streamed upstream body -- that
# produces a generic "Internal Server Error" on every call through
# /v1/{path}. Keep this client alive for the app's lifetime instead, and
# clean up each individual response via BackgroundTask.
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _http_client
    # httpx's bare default (5s connect/read/write/pool) is sized for
    # ordinary REST APIs, not local LLM inference -- a "thinking" or
    # reasoning model can easily sit silent for well over 5s before its
    # first output token. Connect stays tight by default (something is
    # actually wrong if a local backend doesn't even accept the
    # connection quickly); read is generous by default to cover slow
    # local generation without hanging forever if something's genuinely
    # stuck. Tune both via GUARDIAN_UPSTREAM_TIMEOUT /
    # GUARDIAN_UPSTREAM_CONNECT_TIMEOUT for your own hardware and models.
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS)
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http_client is not None:
        await _http_client.aclose()


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough, conservative token estimate from raw message text length."""
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

    estimated = estimate_tokens(messages)
    _state["last_known_total_tokens"] = estimated
    threshold_tokens = int(NUM_CTX * COMPACT_THRESHOLD)

    if estimated < threshold_tokens or len(messages) <= KEEP_RECENT_MESSAGES + 1:
        return payload

    log.info(
        "Estimated %d tokens >= threshold %d (%.0f%% of %d) -- compacting.",
        estimated, threshold_tokens, COMPACT_THRESHOLD * 100, NUM_CTX,
    )

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= KEEP_RECENT_MESSAGES:
        return payload  # nothing meaningful to compact

    to_summarize = non_system[: -KEEP_RECENT_MESSAGES]
    to_keep = non_system[-KEEP_RECENT_MESSAGES:]

    summary = await summarize_older_messages(client, payload.get("model", ""), to_summarize)
    if summary is None:
        # Fail open: forward the original request untouched rather than
        # guess at a destructive truncation.
        return payload

    summary_message = {
        "role": "system",
        "content": (
            "[Context Guardian auto-compaction — earlier conversation "
            f"({len(to_summarize)} messages) condensed to stay within the "
            "context window:]\n\n" + summary
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
        "estimated_tokens_after": estimate_tokens(new_messages),
        "summary_preview": summary[:500],
    })
    log.info(
        "Compacted %d messages -> %d. Estimated tokens %d -> %d.",
        len(messages), len(new_messages), estimated, estimate_tokens(new_messages),
    )
    return new_payload


@app.get("/guardian/stats")
async def stats():
    return JSONResponse({
        "num_ctx": NUM_CTX,
        "compact_threshold": COMPACT_THRESHOLD,
        "keep_recent_messages": KEEP_RECENT_MESSAGES,
        "upstream": UPSTREAM_URL,
        **_state,
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
    uvicorn.run(app, host="0.0.0.0", port=GUARDIAN_PORT)
