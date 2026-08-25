# Changelog

## 0.3.0 - Compaction stops being destruction

Compaction used to DESTROY the messages it folded away. The summary replaced
them and all that survived was a 500-character preview in the log. If the
summary dropped the one detail you needed, it was gone.

Now the full span is written to disk before folding. **Compaction is lossy in
context and lossless on disk.**

- `write_span()` archives the messages about to be folded, atomically
  (`.part` then `os.replace`), to `GUARDIAN_SPAN_DIR`. It is called on the
  request path, so it returns `None` on failure rather than raising -- losing
  the archive is bad, losing the user's request is worse -- and that `None` is
  recorded, so a missing span is visible rather than silently assumed.
- `prune_spans()` keeps `GUARDIAN_KEEP_SPANS` (default 500).
- `RESERVE_OUTPUT` (default 8192). `GUARDIAN_COMPACT_THRESHOLD` is a fraction
  of the WHOLE window, and the window holds the input *and* everything the
  model generates. At 0.85, requests were served occupying 27,853 of 32,768,
  leaving ~4,900 tokens for think + tool call + answer. Measured: 12 of 38
  requests arrived over the ceiling entirely (max 38,026), and the behaviour
  degraded on a clean gradient -- room to think produced a correct tool call;
  squeezed fell back to a built-in with no schema to format; very squeezed
  produced that plus a fabricated answer. Guardian now compacts against what is
  LEFT after a reserved output budget.
- `KEEP_SUMMARIES` (default 1). Guardian's summaries are inserted as `system`
  messages, and the old code kept every system message forever -- so each
  compaction added a permanent summary that was itself never compacted. Over a
  long session the summaries crowded out the conversation they existed to make
  room for. Retired summaries are folded into the next span, not discarded.
- `partition_messages()` and `is_guardian_summary()` extracted as pure
  functions. The accumulation bug lived in three lines inside an async function
  that needed a live backend to reach, which is why it survived; it is now
  directly testable.
- `GUARDIAN_UPSTREAM_TIMEOUT` / `GUARDIAN_UPSTREAM_CONNECT_TIMEOUT` (600s /
  10s). httpx's own default is ~5s -- a sane generic-HTTP number and a wrong one
  for a local model that thinks for minutes before its first byte.
- `GET /guardian/stats` reports the span config and the upstream timeouts.

### Note on this release

0.2.0 was tagged and then development continued in a working copy outside this
repository, so for several days **the version that ran was not the version in
git** -- and span archiving, the feature above, existed only in the copy that
was never committed. This release is that copy, promoted. The repository is
authoritative again.

The 0.2.0 test suite passes against this code unmodified (13 tests), and
`tests/test_spans.py` adds 12 covering the behaviour above.

## 0.2.0 — Count the tool definitions

**Bug fix, and it is the reason to upgrade.** Guardian estimated the request
from `messages` alone and never looked at `tools`. For a plain chat client that
is harmless — there is no `tools` array. For the clients Guardian was actually
written for (agentic CLIs, and anything wired to MCP servers) the tool
definitions are frequently the single largest item in the request, and they are
re-sent on every turn.

Measured on the setup this was developed against: five MCP servers came to
**28,689 tokens — 87.6% of a 32,768-token window** — before the first user
message. Guardian counted that as zero. It was not firing late; it was
measuring against the wrong ceiling on every request. On an 8,192-token window
the same tool payload was **2.7x the entire context**, a situation no amount of
compaction can rescue.

- `estimate_tool_tokens()` counts `tools` and the legacy `functions` field and
  adds them to the budget. Set `GUARDIAN_COUNT_TOOLS=0` for pre-0.2.0
  behaviour.
- Guardian now logs a `tool_budget` event the first time it sees a given tool
  payload, and logs a **warning** when the tool definitions alone meet or
  exceed the whole context window.
- Compaction log entries now separate `message_tokens_before` /
  `message_tokens_after` from `tool_tokens`, so the part of the budget
  compaction can move is distinguishable from the part it cannot.
- `GET /guardian/stats` exposes `last_tool_tokens` and `count_tools`.

**Expect Guardian to compact sooner and more often after upgrading.** That is
the correct behaviour, not a regression — it is now measuring the whole request
instead of a fraction of it.

**Guardian still cannot reduce the tool cost.** It summarizes messages; tool
definitions are a fixed floor on every request. If you are spending most of your
window on tool definitions, the fix is sending fewer tools — for MCP clients,
loading fewer servers. Many CLIs support this directly; Claude Code and
OpenClaude, for example, take `--mcp-config <file>` plus `--strict-mcp-config`.

## 0.1.0 — Initial release

- Core proxy: passthrough for all `/v1/*` routes, with a compaction check specifically on `POST /v1/chat/completions`.
- Character-length-based token estimation (tokenizer-free, conservative by default).
- Fail-open compaction: if the summarization call to the backend fails for any reason, the original request is forwarded unmodified rather than risking a destructive truncation.
- `GET /guardian/stats` endpoint for live visibility into estimated token usage and compaction count.
- JSON-lines compaction log (`GUARDIAN_LOG_PATH`) recording every compaction event with before/after message and token counts.
- Fixed during initial live verification: the proxy's HTTP client must be a single long-lived instance created at app startup, not a per-request `async with` client — a per-request client closes its connections before `StreamingResponse` gets a chance to actually read the streamed upstream body, which otherwise produces a generic `Internal Server Error` on every single request.
- Fixed during initial live verification: the shared HTTP client needs an explicit, generous timeout. The default 5-second read timeout most HTTP clients ship with is fine for ordinary REST APIs but fails hard against local "thinking"/reasoning models, which can go silent well past 5 seconds before their first output token.
