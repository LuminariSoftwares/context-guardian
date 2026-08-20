# Changelog

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
