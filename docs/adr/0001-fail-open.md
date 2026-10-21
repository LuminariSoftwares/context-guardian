# 0001. Fail open

**Status:** accepted. Introduced in 0.4.0 (`ccaf950`); extended to the whole rewrite step in 0.10.0.

## Context

The proxy sits on the only path between a coding CLI and its model. In 0.2.0 the summariser sometimes returned an
empty string; the caller's guard tested `if summary is None`, so the conversation was replaced by an empty summary
and, with no archive yet, destroyed. The maintainer's logs held 37 consecutive compactions with an empty
`summary_preview`. The symptom was an agent that suddenly behaved as if it had never been given a task.

## Decision

Compaction replaces messages only when a usable summary came back. A failed, empty or too-short summary
(`GUARDIAN_MIN_SUMMARY_CHARS`), a span the summariser could not read, or any exception anywhere in compaction or the
request rewrite forwards the request **as the client sent it**, as the client's own bytes rather than a
re-serialisation. The refusal is logged and counted. A failed span write never fails the request. The DSH engine
follows the same rule: when the LLM summary fails or cannot fit, a deterministic checkpoint replaces it (0004).

## Alternatives

- **Fail closed** (return an error to the client): the session stops at the moment it most needs to continue,
  which is the failure this project exists to remove.
- **Truncate oldest messages** when the summary fails: silent data loss, the bug above in another form.
- **Retry the summary**: the cut is deterministic, so a retry usually fails the same way; it only adds latency.

## Consequences

- The worst case is "no help", never "lost conversation": an over-long request still reaches the backend and may be
  rejected there, exactly as without the proxy.
- One deliberate exception to "nothing is lost": when the summary is good but the span write fails (disk full,
  permissions), the compaction still proceeds and that span's originals are not archived. The proxy logs a warning
  saying the span is lost; blocking the request on a disk error would turn a housekeeping failure into a dead session.
- Every guard needs a test with the value that actually occurs (`""`, not only `None`); `tests/test_guardian.py`
  carries them.
- Failures are quiet for the client, so they must be loud elsewhere: `/guardian/stats` counts rejected summaries
  and the log records each refusal.
