# Changelog

## 0.1.0 — Initial release

- Core proxy: passthrough for all `/v1/*` routes, with a compaction check specifically on `POST /v1/chat/completions`.
- Character-length-based token estimation (tokenizer-free, conservative by default).
- Fail-open compaction: if the summarization call to the backend fails for any reason, the original request is forwarded unmodified rather than risking a destructive truncation.
- `GET /guardian/stats` endpoint for live visibility into estimated token usage and compaction count.
- JSON-lines compaction log (`GUARDIAN_LOG_PATH`) recording every compaction event with before/after message and token counts.
- Fixed during initial live verification: the proxy's HTTP client must be a single long-lived instance created at app startup, not a per-request `async with` client — a per-request client closes its connections before `StreamingResponse` gets a chance to actually read the streamed upstream body, which otherwise produces a generic `Internal Server Error` on every single request.
- Fixed during initial live verification: the shared HTTP client needs an explicit, generous timeout. The default 5-second read timeout most HTTP clients ship with is fine for ordinary REST APIs but fails hard against local "thinking"/reasoning models, which can go silent well past 5 seconds before their first output token.
