# Context Guardian
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)

A tiny proxy that sits in front of any OpenAI-compatible LLM backend (Ollama, LiteLLM, Headroom, vLLM, LM Studio, and similar) and forces a conversation-history compaction *before* the context window fills up — instead of letting requests grow until the backend hard-errors and the session dies.

## Why this exists

Claude-Code-style coding CLIs (Claude Code itself, and OpenAI-compatible-backend tools like OpenClaude) ship with a built-in auto-compact feature. That feature depends on accurate, real-time token-usage accounting coming back from the API in the exact shape the CLI expects. Point one of these tools at a local model through an OpenAI-compatible bridge — Ollama's `/v1` endpoint, a LiteLLM proxy, a Headroom proxy — and that accounting is frequently missing, wrong, or shaped differently, so auto-compact silently never fires.

The visible symptom: the session just runs until the backend hard-rejects the request ("token limit reached"), you're forced to close and reopen, and there's no partial-compaction attempt in between — you just lose your place.

Context Guardian is a small, deliberately simple fallback for that specific gap. It estimates the running token count itself, and once a conversation crosses a configurable threshold, it asks the *same backend* to condense the older portion of the conversation into one summary message before forwarding the request onward. Recent messages are always kept verbatim. If the summarization call itself fails, Guardian fails **open** — it forwards the original, uncompacted request rather than risk silently dropping history.

## Where it sits in your stack

This is a new link in an existing chain, not a replacement for anything you already have:

```
Your CLI / agent (Claude Code, OpenClaude, etc.)
    -> Context Guardian        (this project)
    -> your existing OpenAI-compatible backend
       (Ollama directly, a LiteLLM proxy, Headroom, vLLM, ...)
```

Point your CLI's `OPENAI_BASE_URL` at Context Guardian instead of directly at your backend, and set `GUARDIAN_UPSTREAM_URL` to wherever your backend actually lives. Guardian is a pure passthrough for everything except `POST /v1/chat/completions`, which gets the compaction check — every other route, including streaming responses, is forwarded byte-for-byte, untouched.

## If you use MCP servers or an agentic CLI, read this

Guardian counts your `tools` array against the context budget. It did not
before 0.2.0, and that was a real bug — see the changelog.

Tool definitions are usually invisible in a way message history is not. You do
not type them, they do not scroll past, and your CLI's context display often
does not break them out. But they are in every single request. On the setup this
was developed against, seven MCP servers came to **28,689 tokens — 87.6% of a
32,768-token window** — before the first user message.

**Guardian cannot compact them.** It summarizes conversation history; tool
definitions are a fixed floor underneath it. So there are two different problems
and only one of them is Guardian's:

| Problem | What fixes it |
|---|---|
| Conversation history grows until the window fills | Guardian |
| Two thirds of the window is gone before you type | Loading fewer tools |

Guardian will now tell you which one you have. It logs a `tool_budget` event the
first time it sees a given tool payload, and warns outright when the tool
definitions alone meet or exceed the whole window:

```
[ContextGuardian] TOOL DEFINITIONS ALONE (2671) EXCEED THE ENTIRE CONTEXT
WINDOW (1000). Nothing this proxy does can fix that -- send fewer tools.
```

If you see that, no proxy setting will help you. Most MCP-capable CLIs let you
scope which servers load per session — Claude Code and OpenClaude both accept
`--mcp-config <file>` together with `--strict-mcp-config`, which makes that file
the only source of MCP servers for the session.

One consequence worth expecting: **after upgrading, Guardian compacts sooner and
more often.** It is measuring the whole request now instead of a fraction of it.
If that feels aggressive, the honest reading is that your window was already
this full and you could not see it.

## What this does *not* do

- **It doesn't replace or duplicate compression your backend already does** (e.g. Headroom, prompt caching). It forwards to your backend as-is once it's decided whether to compact first — the two are complementary, not competing.
- **It doesn't fix your CLI's own context-usage display.** Your CLI doesn't know this proxy exists, so its own token counter will drift from reality after a compaction happens. What matters is that the session keeps working instead of hard-stopping — a slightly-wrong displayed number afterward is an accepted tradeoff of doing this invisibly at the proxy layer, since the CLI itself usually isn't something you can modify.
- **It is not a tokenizer-accurate counter.** Token count is estimated from character length (~3.5 chars/token by default), not a real tokenizer, so it triggers a little early rather than late. Treat it as a safety-margin trigger, not a precise measurement.

## Install

```bash
git clone https://github.com/LuminariSoftwares/context-guardian.git
cd context-guardian
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then run `python configure.py` (see [Configure](#configure) below) before starting Guardian for the first time.

## Configure

Run the interactive setup script instead of hand-editing a config file — it asks you a handful of questions about your specific hardware/backend (most importantly, your model's real context window) and writes the answers to `.env` for you:

```bash
python configure.py
```

Every question has a sensible default shown in `[brackets]` — press Enter to accept it. You can re-run `configure.py` any time to change your answers, or just edit `.env` directly afterward.

**The one setting that actually matters per-person is `GUARDIAN_NUM_CTX`.** This project was originally built and tested on a 16GB card (RTX 4070 Ti Super) running a model configured for a 32K context window — that number is specific to that hardware, not a universal default. Your correct value depends entirely on your own GPU/VRAM budget and which model you're running, so `configure.py` asks for it explicitly rather than silently assuming everyone's setup looks the same. If you're not sure what your real number is:

- **Ollama:** run `ollama ps` while your model is loaded — the `CONTEXT` column shows the live value actually in use (not necessarily the model's theoretical max).
- **LM Studio / vLLM / other servers:** check whatever context-length setting you configured when loading the model — Guardian has no way to auto-discover this, so it needs to match what you actually set.
- **If you're unsure or haven't set one explicitly:** start conservative (the `configure.py` default of 32768 is a reasonable, widely-safe starting point on a single consumer GPU) and raise it later once you've confirmed your backend can actually sustain it without running out of VRAM.

Setting this too high means Guardian won't compact soon enough and your backend can still hard-error before Guardian steps in. Setting it too low just means Guardian compacts a bit more often than strictly necessary — safe, just not optimal.

If you'd rather skip the wizard, copy `.env.example` to `.env` and edit it by hand:

```bash
cp .env.example .env
```

| Variable | Default | What it does |
|---|---|---|
| `GUARDIAN_PORT` | `8786` | Port Guardian itself listens on |
| `GUARDIAN_UPSTREAM_URL` | `http://localhost:11434/v1` | The OpenAI-compatible backend Guardian forwards to |
| `GUARDIAN_NUM_CTX` | `32768` | Your model's real context window, in tokens — keep this in sync with your actual backend/model config |
| `GUARDIAN_COMPACT_THRESHOLD` | `0.85` | Fraction of `GUARDIAN_NUM_CTX` at which compaction triggers |
| `GUARDIAN_KEEP_RECENT_MESSAGES` | `8` | Most-recent messages always kept verbatim, never summarized |
| `GUARDIAN_CHARS_PER_TOKEN` | `3.5` | Characters-per-token used for the estimate |
| `GUARDIAN_COUNT_TOOLS` | `1` | Count the `tools` array against the budget. Set `0` for pre-0.2.0 messages-only behaviour |
| `GUARDIAN_UPSTREAM_TIMEOUT` | `600` | Seconds to wait for the upstream backend to respond |
| `GUARDIAN_UPSTREAM_CONNECT_TIMEOUT` | `10` | Seconds to wait for the upstream connection itself |
| `GUARDIAN_LOG_PATH` | `<repo>/logs/context_guardian_log.json` | Where compaction events are logged (JSON lines) |
| `GUARDIAN_HOST` | `127.0.0.1` | Interface Guardian binds. **Leave this alone unless you know what you are doing** — Guardian fronts your backend with no authentication |
| `GUARDIAN_RESERVE_OUTPUT` | `8192` | Tokens held back for the model's *output*. The window has to hold the reply and (for reasoning models) the thinking too, so compaction triggers against what is LEFT. If this is ever ≥ `GUARDIAN_NUM_CTX` it is clamped to half the window and logged — fix the config |
| `GUARDIAN_SPAN_DIR` | `<repo>/logs/guardian_spans` | Where evicted messages are archived before folding. This is what makes compaction lossless on disk |
| `GUARDIAN_KEEP_SPANS` | `500` | How many span files to keep. `0` keeps none |
| `GUARDIAN_KEEP_SUMMARIES` | `1` | How many of Guardian's own previous summaries stay in the window. Retired ones are folded into the next span, not discarded |
| `GUARDIAN_MIN_SUMMARY_CHARS` | `40` | A summary shorter than this is treated as a FAILED summarisation and nothing is evicted. See 0.4.0 in the changelog for why this exists |
| `GUARDIAN_MIN_TRANSCRIPT_CHARS` | `80` | If the messages being evicted render to less than this, Guardian refuses to summarise rather than summarising nothing |
| `GUARDIAN_TOOL_ARG_CHARS` | `300` | How much of a tool call's arguments reaches the summariser. The full text is in the span |
| `GUARDIAN_SUMMARY_REASONING_EFFORT` | unset | Passed as `reasoning_effort` on the summarisation call only. Non-standard, so off by default; `low` roughly halved summarisation latency on gpt-oss |

**A note on timeouts:** local "thinking"/reasoning models can go silent for a long time before their first output token. If you see `500` errors appear only on real (non-trivial) requests after a long pause, raise `GUARDIAN_UPSTREAM_TIMEOUT` before assuming something is broken — the default 5-second timeout most HTTP clients ship with is sized for ordinary REST APIs, not local LLM inference, which is exactly the bug this project's own commit history caught during development.

## Run

```bash
python context_guardian.py
```

Then point your CLI's `OPENAI_BASE_URL` at `http://localhost:8786/v1` (or whatever port you configured).

## Testing before you trust it with a real session

1. Start your real backend (Ollama, LiteLLM, Headroom, whatever you use) the way you normally would.
2. Start Guardian: `python context_guardian.py`
3. Send one manual request at it instead of your real CLI, to confirm plain passthrough works before testing compaction specifically:
   ```bash
   curl http://localhost:8786/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<your-model>","messages":[{"role":"user","content":"say hi"}]}'
   ```
4. Check `GET http://localhost:8786/guardian/stats` for the running token estimate and compaction count.
5. Force a compaction test: temporarily set `GUARDIAN_NUM_CTX` and `GUARDIAN_COMPACT_THRESHOLD` low (e.g. `NUM_CTX=2000`, `THRESHOLD=0.5`), then send a conversation with several long messages. Confirm a compaction log entry appears at `GUARDIAN_LOG_PATH` and the request that actually reaches your backend is smaller than what was sent in.
6. Only after that, point your CLI's `OPENAI_BASE_URL` at Guardian and test with a real session.

## Running multiple models with different context windows

Guardian's `GUARDIAN_NUM_CTX` is fixed for the lifetime of one running instance. If you switch between models with meaningfully different context windows, either:

- run a second Guardian instance on a different `GUARDIAN_PORT` with its own `GUARDIAN_NUM_CTX`, or
- keep one instance and accept that its threshold is tuned to whichever model has the smaller/more-constrained window (safer than the alternative, since it just means Guardian compacts a bit earlier than strictly necessary for the larger-window model).

## Development / running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT — see [LICENSE](LICENSE).
