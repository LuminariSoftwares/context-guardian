# 0009. Model-driven context tools: the model proposes a fold, the proxy validates it and does the summarising

**Status:** accepted with this change (ships off). Item C of the 2026-11 feature batch. Opt-in, off by default.

## Context

Today the proxy alone decides when to compact and what is evicted: everything older than a keep-recent window, all
at once, when the request crosses a budget (0005). A model working a long task often knows better which part of the
history is finished. We want to let it say so, as an opt-in mode, without giving up what the proxy guarantees:
nothing is lost (0003, 0007), the request never fails because of it (0001), and a small local model that misuses a
tool cannot hurt the session or write instructions into its own future context.

Facts about the code this builds on (read, not assumed):
- The proxy is stateless per request: the client resends its whole history and compaction is re-derived each time.
  Memory is in-process only (summary memo, the 64-session usage table).
- `maybe_compact` keeps system messages, one `[Context Guardian auto-compaction` summary message, and the newest
  messages; `_split_system` hoists every `system` message to the front, retires older proxy summaries into the next
  span, and treats only messages that start with that marker as proxy summaries.
- Summaries are produced by the **fenced** summariser (`summarize_older_messages`, `_fence_safe`), memoised by hash
  tuples (`_memo_summary`), archived as a sealed, indexed span (`_apply_memo` -> `write_span` -> `seal_file`), and
  carry the user's explicit "remember this" sentences verbatim (`build_pins`).
- `cg_seal.js` and `cg_anchors.js` are the DSH engine's JavaScript half; the proxy's half of the same archive format
  is Python, and that is what this reuses. The MCP server stays read-only (0006): **no MCP write tool is added.**

## Decision

Scope: `/v1/chat/completions` only. The Anthropic `/v1/messages` route is not covered (its tool-result and streaming
shapes differ); the flag is ignored there and the docs say so. `GUARDIAN_DRY_RUN` disables the mode.

**The model never writes text that enters the history.** `compress` takes a range of turns, nothing else. The
summary of that range is made by the proxy's own fenced summariser, exactly as for a normal compaction, and placed in
the standard summary message. This removes the largest risk of the first draft of this record: a model-authored
string gaining permanent system authority.

**Flag and gate.** `GUARDIAN_CONTEXT_TOOLS` (default `0`). Tools are offered only on a request whose estimate has reached
`GUARDIAN_CONTEXT_TOOLS_MIN_PCT` (default 50) percent of the compaction budget (`0` = always), so a short chat keeps
its streaming and pays no tool-definition tokens. Not offered when: the client's `tool_choice` is anything but
absent/`auto`, `response_format` is set, `n > 1`, the request is a dry run, or the client already defines a tool whose
name starts `guardian_` (all four or none; the proxy's names are `guardian_context_status`, `guardian_search_context`,
`guardian_compress`, `guardian_decompress`).

**Turns and visibility.** A *turn* is a `user` message and the non-user messages after it up to the next `user` message
(`system` messages are not turns); folding whole turns keeps the history alternating and keeps every assistant tool call
with its results. Turns are numbered over the history the client sent. The model is shown the *forwarded* view, so each
turn has a state computed from it: `visible` (all its messages are in the forwarded request), `protected` (the first turn,
the task; the last `max(GUARDIAN_CONTEXT_TOOLS_PROTECT_TURNS (2), turns inside the keep-recent window)`; or any turn
partly shown), `folded f<k>` (a fold applies), or `summarised` (automatic compaction already evicted it; it cannot be
folded). Only `visible` turns can be folded.

**Tools** (appended to `tools`, counted in the budget):
- `guardian_context_status()`: usage, window, budget, the folds, and the `visible` ranges that can be folded, as explicit
  ranges (`T4-T9 ~5,400 tok`) plus a capped per-turn list (about 3,000 characters).
- `guardian_search_context(query)`: case-insensitive keyword search over the visible messages and the summaries in the
  forwarded view; returns turn numbers and snippets, capped. Read-only. (Searching the archive stays with the MCP tools
  and `context-guardian-recall`; the archive is not session-scoped, so it is not exposed to a per-session tool.)
- `guardian_compress(from_turn, to_turn)`: **propose** a fold.
- `guardian_decompress(fold_id)`: cancel a fold.

**Model proposes, proxy validates.** A `compress` call is accepted only if: the arguments parse as two integers; both
turns exist and every turn in the range is `visible`; at most one fold is
accepted per client request and the session has fewer than `GUARDIAN_CONTEXT_TOOLS_MAX_FOLDS` (4) live folds. The range must
be at least 1,500 estimated tokens (a fold writes a pointer and a summary; a smaller range would not shrink), and a fold is
applied only if the resulting summary message is smaller than the turns it replaces. An accepted call does **nothing else**:
it records a *pending fold* (turn range, the hash of each folded message, and the hash chain of the *turns* before the range;
system messages are excluded from the chain, so a dynamic system prompt cannot break it) in a request-local list. Fold ids
(`f1`, `f2`...) are allocated from the session store synchronously (no `await`), so concurrent loops never mint the same id. A rejected call returns its reason as the tool result so the
model can correct itself inside the call cap. `decompress` accepts a live or pending fold id and records a *pending cancellation*; like a fold, it takes effect only when the
loop commits. Nothing is written to disk and no summariser is called inside the tool loop.

**Committing and applying.** Pending folds are committed to the per-session store (in memory, LRU 64 sessions) only when
the loop finishes with a final response for the client. On the **next** request of that session, before normal compaction,
each live fold is located in the history the client sent: the folded messages' hashes must match *at the position where
they were recorded*, with the same hash chain before them (so a repeated identical run elsewhere cannot match), and the
protection rules are re-checked against the current turn count. A fold that does not match is dropped and counted (the
client edited or compacted its own history); decompressing it later returns "not in the history any more, the originals
are span X: `read_span`". A matched fold's turns are removed, and the live folds are presented as **one** standard summary message placed directly after
the system messages, exactly where the proxy's own summary sits, never mid-history (strict chat templates reject a non-leading
system message). The first time a fold is applied the proxy summarises its turns with the existing machinery (`_memo_summary`
with the fenced summariser, `_apply_memo` with a fold marker in the span, `build_pins`), under its own deadline
(`GUARDIAN_CONTEXT_TOOLS_FOLD_TIMEOUT`, 60 s), and **stores the summary, span name and pointer in the fold entry**, so later
requests rebuild the message from the entry: no summariser call and no new span per request, whatever the summary memo does
(it may be off or evicted). The message is built by a small builder of its own (one message for several folds; it keeps the compaction marker prefix
and the exact pinned-facts header, so `extract_pins` and the summary tests still see it as a proxy summary).
**Folds never coexist with threshold compaction.** Each request first estimates the *unfolded* history; if that already
reaches the compaction threshold, ordinary compaction owns the history: the live folds are marked superseded and dropped (their
spans stay, and the originals are summarised by the normal fenced path as usual), and nothing from the fold is carried into the
request. So there is never a second summary message next to the compaction summary, and a fold is not a floor that nothing can
shrink. A fold therefore only ever *delays* compaction: it lowers a request that is between the gate and the threshold. If the summariser fails or exceeds its
deadline, the fold is dropped and the request goes on without it (ADR 0001). `decompress` removes the fold, so the next request carries the originals the client resent,
byte for byte. Normal threshold compaction still runs afterwards as the backstop.

**The tool loop.** A request with tools offered is sent upstream non-streaming (`stream: false`, `stream_options` removed,
`accept-encoding` dropped from the proxy's own loop requests so the body is plain JSON). If every tool call in the reply is a
proxy tool, they are executed and the assistant message and tool results are appended to a request-local list, and the
request is sent again; at most `GUARDIAN_CONTEXT_TOOLS_MAX_ROUNDS` (3) rounds that end in proxy tool calls and `GUARDIAN_CONTEXT_TOOLS_MAX_CALLS` (6)
calls in all. Each round is an ordinary upstream request with the normal upstream timeout, including the round that turns
out to carry the final answer (the proxy cannot know in advance which that is), so a slow local model is not cut off; the
`GUARDIAN_CONTEXT_TOOLS_TIMEOUT` (90 s) budget covers only the rounds that ended in proxy tool calls plus tool execution, and
is checked before another round starts. Exceeding any cap, a reply that mixes proxy and client tool
calls, a reply that is not a parseable chat completion, or any upstream error (on the first round too; a 400/413 still reaches the
existing rejection bookkeeping on the normal path) discards the loop
(nothing was committed) and the request goes through the **normal path**: the request the proxy would have forwarded with
the flag off. The reply with no proxy tool call is the final response, returned as JSON, or re-framed as a short SSE
stream (one content/reasoning/tool-call chunk, one finish chunk, usage if the client asked, `[DONE]`) when the client
streams. **The model's tool calls and their results never appear in the client-visible response**; the loop-local messages
are discarded. Known limits: while tools are offered the answer is not streamed token by token; the `usage` the client sees on the final round
counts the loop's extra messages and the four tool definitions; once the raw history is over the budget everything outside
the keep-recent window is already `summarised`, so the foldable turns are those the model is still shown (the mode helps
before the window is full, and is of little use after); a model that writes a tool
call as plain text in its answer shows that text to the client; the loop's per-round usage is not fed to calibration.

**Fail open, said precisely.** "Normal path" means the request as the proxy would forward it with `GUARDIAN_CONTEXT_TOOLS=0`
(the client's bytes plus ordinary compaction); the feature adds nothing to it. Side effects of a failed loop: none that
persist: a loop commits at its end or not at all, spans are written only when a fold is applied on a later request, and a
client disconnect (`CancelledError`) leaves the commit step unreached. A failed loop costs the extra upstream calls it
made. Every proxy-tool execution runs under the loop deadline.

**Audit.** Every proxy-tool call, accepted or rejected, is logged as a `context_tool` event (`tool`, `ok`, `reason`,
`turns`, `est_tokens`, `fold`), every fold application (with the span name), orphan drop and cancellation as `context_fold`,
and a fold's span carries `{"fold": "f<k>", "model_requested": true}` (`_apply_memo` gains an optional `extra` passed to
`write_span`), all counted in
`/guardian/stats` (`context_tools`) and visible on `/guardian/events`. The applied fold's span is the durable record of
what was folded (subject to `GUARDIAN_KEEP_SPANS` pruning, like every span).

## Files

`cg_context_tools.py` (new: turn model, visibility, validation, pending/live fold bookkeeping, tool schemas and executors,
SSE re-framing; pure, the proxy supplies the summariser and span writer); `context_guardian.py` (gate and fold application in
`rewrite_chat_payload`, the loop in `proxy()`, stats); README, CHANGELOG, `llms.txt`; packaging lists.

## Failure modes

Small models: bad or stringified arguments (rejected with a reason), calling every turn (round/call/fold caps), folding
what they still need (the protected turns, `decompress`), proposing ranges that overlap (rejected), proposing the same range
twice (rejected: pending or live), invented turn numbers (rejected), mixed tool calls (loop discarded). Prompt injection:
tool results are the proxy's own text; the only text from the model that reaches a future request is its normal assistant
reply; a fold's summary comes from the fenced summariser. Concurrent requests of one session: store updates are synchronous
(no `await` inside), so a fold is committed whole or not at all; validation reads the store at execution time. Session id
collisions: folds are keyed by `usage_session_id` (the `X-Guardian-Session` header, else a hash of model, first system and first
user message), so a client whose system prompt changes every request never matches a fold (send the header); a fold only applies where its position-anchored hashes match. Restart or LRU
eviction: folds vanish and the history is simply unfolded (logged).

## Tests (written first)

Pure: turn segmentation (tool calls with results, leading non-user messages, system excluded); visibility from a forwarded
view; every validation rule with an accepting and a rejecting case; stringified/malformed arguments; fold location is
position-anchored (an identical run elsewhere does not match; protection re-checked); `context_status` cap and explicit
ranges; search scopes; SSE re-framing parses back, and a re-framed tool-call reply (index, id, name, arguments in one delta) is accepted by the OpenAI Python SDK's stream parser (run once as evidence; the SDK is not a dependency). Proxy (real app, scripted stub model that calls the tools): a model that
proposes a fold gets a final answer with no trace of the call in the client's response; the next request is smaller, holds a
standard summary message and not the folded originals, the span is sealed and `--verify` is OK; `decompress` returns the
originals byte for byte; folding a `summarised` turn is rejected; mixed tool calls discard the loop; round, call, fold and time
caps (including that a finished answer round is never cut by the loop budget); a fold survives `GUARDIAN_SUMMARY_MEMO=0`
and memo eviction without a new summariser call or span per request; the fold summary sits directly after the system
messages; a fold is not applied when the summary would not be smaller; a cancellation of a live fold made in a discarded
loop changes nothing; a fold whose summariser call times out leaves no in-flight memo waiter hanging; once the unfolded history
reaches the threshold the folds are dropped and the forwarded request is exactly the flag-off one; flag off changes nothing (request bytes identical to main); gate; `tool_choice`, `response_format`, name collision,
`stream_options`; client with no tools; dry run. **Fail-open injections:** an exception in each stage, upstream 500 on round
1 and on round 2, unparseable reply, a client disconnect mid-loop, summariser failure when applying a fold: the request that
reaches the upstream last is the one the flag-off path would have sent, and the client gets that response. Probe from the
description, not the implementation: on `main` the model's tool call is returned to the client (nothing is folded), on the
branch the client sees only the answer and the next request shrinks.

## Alternatives

- **Model writes the summary** (first draft of this record): cheaper, but the model's text gains system authority, bypasses
  the fence and the pin logic, and is an irreducible floor. Rejected after review.
- **Expose folds through the MCP server**: 0006 keeps MCP read-only.
- **Apply a fold inside the same request**: needs a summariser call inside the loop and a second view of the history; the
  next-request design needs neither.
- **Fold by message index**: indexes drift and split tool pairs; turns do not.
- **Persist folds to disk**: more privacy surface and stale state; the client resends everything.
- **Stream the first round and intercept**: correct but needs a streaming tool-call parser and a second splice path; an open
  item.

## Consequences

- With the flag on and the gate open, answers arrive whole rather than token by token.
- A model-requested fold can only remove history the proxy has summarised with its own summariser and archived first.
- A fold or `decompress` changes the evicted prefix, so the summary memo re-summarises and a backend's prompt cache restarts
  at the change.

## Review log

Design review by an independent Fable reviewer, four rounds; the first draft was rejected.
Round 1 (*revise*, five design-level blockers in the first draft, in which the model wrote the summary and the proxy kept it as a
permanent system message): the model-written text would have gained system authority, bypassing the fence and the pin logic and
becoming a floor nothing could shrink; the model could fold turns it could not see; hash-tuple matching could hit a repeated run
inside the protected window; fail-open was not achievable after upstream calls and span writes; the non-streaming loop mishandled
`stream_options`, `tool_choice`, `accept-encoding`. Redesign: the model proposes a range only; the proxy's fenced summariser writes
the summary; folds apply on the next request, position-anchored; pending changes commit only when the loop finishes.
Round 2 (*revise*, three new blockers): the fold summary lived only in the memo (up to four summariser calls per request when it
missed); the loop deadline covered the round that produced the final answer; a mid-history `system` message is not what the code
or strict chat templates expect. Fixed: the summary, span and pointer are stored in the fold entry; the loop budget covers only
rounds that ended in proxy tool calls; one summary message directly after the system messages.
Round 3 (*approve* on condition): the claim that threshold compaction retires the fold message was false against `_split_system`
(the newest marker message is always kept). Fixed by changing the design instead of the claim: folds never coexist with threshold
compaction; once the unfolded history reaches the threshold the folds are dropped and ordinary compaction summarises the originals.
Adopted from the non-blocking lists: 1,500-token minimum and "apply only if smaller", a turn-only hash chain, decompress pending
until commit, synchronous id allocation, the fold marker on the span, round-1 error handling, client-visible usage, session-key note,
SDK parse evidence. Kept against the reviewer's "what to cut": `decompress` (as a cancel) and SSE re-framing, because the brief names
four tools and coding CLIs stream.

Code review (fresh Opus reviewer, after implementation), round 1 -- four blockers, all reproduced and fixed: (1) a fold could cover the most
recent turns: protection was checked when the fold was proposed but not when it was applied, so a client that rewound its history made a
folded range "recent" (now re-checked on every application against the current history, plus a turn-boundary check); (2)
`GUARDIAN_CONTEXT_TOOLS_TIMEOUT` never limited anything (the clock started after the upstream reply; it now counts each round that ended
in proxy tool calls, upstream time included, and never the answer round, with tests for both); (3) fail-open was broken for a `tools`
entry like `{"function": "read_file"}`: the eligibility check raised, and so did the fallback, so a request over the threshold lost the
compaction the flag-off proxy would apply (the check is type-safe and the fallback no longer re-runs it; tested byte for byte against the
flag-off request); (4) the audit trail only covered committed loops (events and counters are now written in a `finally`, with an
`outcome` of `committed` or `discarded`). Also fixed: an exact upstream token count could put a folded request over the threshold so
ordinary compaction ran on top of the fold message (folds are dropped and the unfolded request redone); a status-only loop churned the
fold store's LRU (a record is written only when there is something to commit); the fold label now says it came after the first turn
shown below. Left and documented: the loop path does not feed calibration; session ids that collide drop each other's folds; a
mid-history `developer` role message counts as part of a turn; the failed-prepare fallback runs the rewrite a second time.

