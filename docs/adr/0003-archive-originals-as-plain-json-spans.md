# 0003. Archive what compaction removes as plain JSON spans

**Status:** accepted. Introduced in 0.3.0 (`c6bc4b6`).

## Context

A summary loses detail by construction. Before 0.3.0 the evicted messages existed nowhere once compacted, so a
wrong summary was unrecoverable and a later question about an early detail could not be answered.

## Decision

Before a compaction takes effect, write the original messages and the summary to
`GUARDIAN_SPAN_DIR/<run_id>/<NNNN>.json`, one file per compaction, plain JSON. The summary the model receives names
the command that searches the archive (`context-guardian-recall`, 0.9.0). A failed write removes the claimed file
instead of leaving a half-written span (0.9.0) and never fails the request: the compaction proceeds and the
warning says that span is lost (0001). Readers search spans rather
than re-inlining them: `context-guardian-recall`, the MCP tools `search_spans` / `read_span`, and the DSH engine's
`recall` / `search`.

## Alternatives

- **SQLite or an embedded index**: faster search at scale, but a binary format the user cannot open in an editor,
  and a schema to migrate. The archive is written far more often than it is searched.
- **A vector store / semantic recall**: a new dependency and an embedding model on the hot path of a proxy that
  must stay small.
- **No archive, trust the summary**: the 0.2.0 failure.

## Consequences

- Human-readable, greppable, trivially backed up or deleted; one format across the proxy and the DSH engine (0004).
- Search is a linear scan over files: fine for a session's archive, slow for months of them. The proxy keeps the
  newest `GUARDIAN_KEEP_SPANS` (500) span files and prunes older ones.
- The archive holds full conversation text in clear on disk. By default it lives under `logs/`, which is
  git-ignored; anyone who can read that directory can read the conversations.
