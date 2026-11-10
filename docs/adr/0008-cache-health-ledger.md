# 0008. Cache-health ledger: measure what compaction does to the reusable prefix

**Status:** proposed (this change). Item B of the 2026-11 feature batch.

## Context

Hosted providers (OpenAI, Anthropic) and local servers with prefix or KV caching (vLLM `--enable-prefix-caching`,
llama.cpp slots) reuse the work done for the leading part of a request when the next request starts with
byte-for-byte (token-for-token) the same tokens. A compaction rewrites the history, so it can throw that
reusable prefix away: the next request pays full price for tokens the backend had already processed. Nothing in
the proxy measures this today, so nobody can tell whether compaction is cheap or expensive in cache terms.

How the proxy rewrites (read from `maybe_compact`): the forwarded request is `system messages + one summary
message + the newest messages`. The system prompt and tool definitions are untouched and come first. Everything
between the system prompt and the kept-recent window, the earliest turns included, is replaced by the summary.
Between compactions the summary memo (`GUARDIAN_SUMMARY_MEMO`) reuses the same summary for the same evicted range,
so successive requests share the whole previous request as a prefix. A compaction that *extends* the summary
changes the message right after the system prompt, so the shared prefix ends there.

## Decision

**Measure only. Do not change what compaction produces.** Add a per-session ledger, `cg_cache_health.py`
(pure, stdlib), fed from the one place the final forwarded request is known (`proxy()`), and surface it. Off by
default (`GUARDIAN_CACHE_HEALTH=0`); proposed to flip on in the PR.

1. *Fingerprint (estimates, not tokenizer output).* For each forwarded request build an ordered list of
   elements `(sha256-12 of canonical JSON, estimated tokens)` in the order providers cache: the `tools` array, then
   the Anthropic `system` field, then each message. Canonical JSON = sorted keys, no whitespace,
   `ensure_ascii=False`. On the Anthropic route the proxy appends its summary *inside* `system`
   (`with_note`), so a string `system` is cut into 4,096-character chunks and a list `system` is hashed block by
   block: the unchanged leading text keeps counting as shared. On the chat route tools are rendered inside the
   chat template, so "tools first" is a guess at the real token order and every number is labelled an estimate.
   Memory per session: one digest + one integer per element of the **latest** request only, plus bounded rows.
2. *Which requests are compactions.* Never the global `_state` counter (other sessions move it during the
   awaits of a rewrite, and a memo reuse also bumps it). The two rewrite paths already write a per-request
   `_usage_snap` (a contextvar); they now also set `snap["source"]` (`generated`, `extended` or `reused`, the memo
   source) next to `snap["at"]`, and `_note_spill` sets `snap["spill"]`. The ledger reads those. Rows are written
   for **real compactions only** (`generated`, `extended`): a `reused` turn does not rewrite the prefix, so it
   is counted (`reused_turns`) and not averaged in. A turn whose only change was an oversized-message spill is counted
   as `spills` and is not blamed on the client. Any other request whose prefix is shorter than the previous
   one's counts as `prefix_breaks_without_compaction` (the client edited its own history).
3. *Row* (compaction only): `{seq, at, route, source, before_tokens, after_tokens, shared_tokens, shared_pct,
   head_n, head_identical, head_hash_before, head_hash_after, hit_before, hit_at}`. `head_identical`: did the leading
   `min(GUARDIAN_CACHE_PREFIX_TOKENS, previous total)` tokens (default 1024, the smallest block providers
   cache) survive, at element granularity. The two 8-hex head hashes cover the same leading elements before and
   after; they appear in `/guardian/stats` (a loopback-only route) for the last row only, because a short hash of
   a known system prompt can be matched against a list of known prompts, and nothing more than that is exposed.
   A row is keyed by `(session, seq)`; usage arrives after the next request may have started, and fills the row
   with that key, never "the latest".
4. *Observed hit rate.* When the upstream reports cached tokens: OpenAI/vLLM `usage.prompt_tokens_details.cached_tokens`
   (vLLM fills it only when started with `--enable-prompt-tokens-details`, so absence there means "not reported",
   not "no cache"), Anthropic `cache_read_input_tokens` (prompt total = input + cache read + cache creation, as the
   calibrator counts it; hosted Anthropic only caches up to the client's `cache_control` breakpoints, so a kept prefix is
   not the same as a cached one, and the line says so), llama.cpp `timings.cache_n`. `hit_before` is the previous
   request's rate, `hit_at` the compacted request's own. Backends that report none (Ollama's OpenAI route among
   them) leave both `null`, and the line says "this upstream reports no cached-token counts, so only prefix
   stability is shown" rather than 0%.
5. *When the work runs.* Not before the request is sent. The fingerprint, comparison and usage read all run in the
   response tap's `finally`, after the last byte has gone to the client (the tap is installed when calibration is on
   or `GUARDIAN_CACHE_HEALTH=1`; with calibration off it is a pure copy-after-yield wrapper and a test proves byte and
   chunk equality). The only work on the request path is reading three values from a contextvar. Requests whose JSON
   exceeds `GUARDIAN_CACHE_HEALTH_MAX_MB` (16) are skipped and counted.
6. *Surfaces.* `/guardian/stats` `cache_health` (`enabled`, `sessions`, `compactions`, `reused_turns`, `spills`,
   `prefix_breaks_without_compaction`, `reports_cached_tokens`, `errors`, and for the latest session its last 5
   rows and `line`); the `/guardian/health` dashboard shows the line in one card; `context-guardian-statusline --cache`
   appends a short form (`prefix 12%`); `context-guardian-doctor` prints the line as one `ok` finding when a running
   proxy has data, an `ok` "off (GUARDIAN_CACHE_HEALTH=1 to measure it)" when disabled, and nothing for a proxy that
   predates it. Plain-English example:
   `last 5 compactions kept 12% of the prefix (first 1,024 tokens identical in 5/5); cache hit rate 81% before, 9% on the compacted request (estimates; hosted Anthropic caches only up to cache_control breakpoints)`.
7. *DSH engine:* out of scope. It runs inside DSH, which owns the request to the model and any caching; the
   proxy ledger has nothing to measure there (ADR 0004 parity is not claimed for this feature).

**Why not change compaction to keep the prefix stable?** The ADRs do not forbid it: 0003 only requires originals to
be archived and 0005 only governs the *tail* that is kept. This is a judgement call, not the ADRs' verdict. A stable
prefix means keeping the earliest N tokens of turns verbatim ahead of the summary, or freezing the summary until it
is a given amount stale. Both change what the model sees and interact with pinned facts, the memo and retired
summaries; whether answers stay good cannot be shown without a quality benchmark, which this batch excludes. The
ledger produces the number that says whether that work is worth doing. Recorded as a decision for the maintainer and
an open item.

## Files

`cg_cache_health.py` (new; also in `pyproject.toml`, `Dockerfile`, `.dockerignore`, `package.json`);
`context_guardian.py` (`snap["source"]` / `snap["spill"]`, usage-cache reader beside `usage_prompt_tokens`,
`_tapped_stream` `on_done`, wiring in `proxy()`, `/guardian/stats`, health card); `cg_statusline.py`; `cg_doctor.py`;
README (config rows, Prove-it paragraph, limits); CHANGELOG; `llms.txt`.

## Failure modes (all fail open, ADR 0001)

- Any raise in fingerprinting, comparison, the usage reader, the on-done callback or stats rendering is caught at its
  call site, counted in `cache_health.errors`, and never reaches the request or the response. Stats rendering of the
  block degrades to `{"enabled": ..., "error": true}`.
- Huge histories: skipped above the size cap, after the response.
- Session id collisions (two clients with the same system prompt and opener) make `shared_pct` read low; the same
  limit `/guardian/usage` documents; `X-Guardian-Session` fixes it.
- Usage never arrives (stream closed early, compressed body, buffer cap): the row keeps `hit_at: null`.
- Memory: at most 64 sessions (LRU), 50 rows each, one digest list for the latest request; no message text.
- Streaming and non-streaming responses share the tap; the tap's contract (yield first, copy after) is unchanged.

## Tests (written first)

Pure: fingerprint equality/inequality; shared-prefix length; head-identical edge cases (previous shorter than N);
Anthropic string and list `system`; row shape; bounded sessions/rows; memo `reused` not averaged in; spill and
client-edit counters; line wording with and without cached counts, with `null`s. Usage reader: OpenAI non-stream and
stream, Anthropic non-stream and stream (`message_start` carries the cache fields), llama.cpp `timings`, Ollama-shaped
(none). Proxy: scripted session through the real app and stub upstream reporting cached tokens: compaction row has the
right shared tokens and hit rates; two interleaved sessions do not contaminate each other; flag off records nothing;
calibration off + flag on: response bytes and chunking identical to a run with the flag off; **failure injection**:
fingerprint raising, usage reader raising, on-done callback raising, tap raising mid-stream, stats rendering
raising -> response bytes identical and the upstream saw the client's bytes. Statusline `--cache`, doctor finding, dashboard
card. Probe from the description: a 12-turn scripted session; the reported "kept N% of the prefix" must equal an
independent computation of the common prefix of the stub's recorded requests (run red on `main`, green on the branch).

## Alternatives

- **Record per-request stability, not per-compaction rows**: bigger, noisier, and the question asked is about
  compactions.
- **Token-exact comparison with the upstream's tokenizer**: exact only on two backends and costs a call per request.
  Element-level digests with the proxy's own estimate are enough to say "the prefix survived or did not".
- **Persist the ledger to disk**: more privacy surface, and the cache it describes is itself short-lived.
- **Rewrite compaction for prefix stability now**: see above; deferred with evidence to be gathered first.

## Consequences

- Users see, in numbers, what compaction costs them in cache terms; with the current design the shared share at a
  compaction is expected to be low (system prompt + tools only) and that is what the line will honestly say.
- A second tap consumer exists; both are copy-after-yield, so the stream-integrity promise of `_tapped_stream` stands.

## Review log

(filled in during review)
