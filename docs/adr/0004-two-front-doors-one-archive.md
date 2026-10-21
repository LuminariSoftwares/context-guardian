# 0004. Two front doors, one archive format

**Status:** accepted. DSH engine since npm 0.1.0-alpha.2 (`592e41d`).

## Context

A proxy can only rewrite requests; it cannot give the model tools or see the agent's session. DeepSeek Harness
(DSH) has a documented compaction hook, and on a 32K local model its own summariser succeeded in 48 of 314 measured
attempts; the rest returned nothing or overflowed the window while summarising.

## Decision

Ship two independent implementations of the same idea:

- **Proxy** (`context_guardian.py`, Python, FastAPI + httpx): for any client that speaks OpenAI
  `/v1/chat/completions` (and, since 0.10.0, Anthropic `/v1/messages`).
- **DSH engine** (`engine.js`, Node, no runtime dependencies): a row in the agent preset. DSH's own summary first;
  when it fails, is empty or cannot fit, a **deterministic** checkpoint from the vendored, unmodified
  `dsh-compaction-instant` compiler (`vendor/compiler.js`, MIT), with `seq` pointers the model's `recall` tool reads.

Both write the same span format (0003), so the same tools read either archive.

## Alternatives

- **Proxy only**: works with DSH too, but cannot offer the model `recall` / `search` or use the session's own
  structure.
- **Engine only**: ties the project to one harness; most local-model users are not on DSH.
- **Fork the compiler**: easier to adapt, but loses the upstream fix path. Vendoring it unmodified, with its licence
  and md5 in THIRD_PARTY_NOTICES.md, keeps that path open.

## Consequences

- Two languages and two test suites (pytest; Node smoke tests), both in CI on three OSes.
- Behaviour must be kept equivalent by hand where the two overlap (chars/token 3.5, span format); the shared
  constant lives in `cg_recall.js` for the Node side.
