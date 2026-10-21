# 0002. Estimate tokens from characters, calibrate per model

**Status:** accepted. Character estimate since 0.1; calibration added in 0.7.0 (`04be7f1`).

## Context

The proxy must decide before forwarding whether a request will fit. It fronts any OpenAI-compatible backend, so the
model, and therefore the tokenizer, is unknown and can change per request. Everything it estimates includes message
text, tool calls, images and the `tools` array (0.2.0 found the `tools` array alone at 87.6 % of a 32K window).

## Decision

Estimate `characters / GUARDIAN_CHARS_PER_TOKEN` (3.5). Correct it per model from what the backend reports
afterwards (`usage.prompt_tokens`, Ollama's `prompt_eval_count`, llama.cpp `timings`): keep the last 20
reported/estimated ratios, use factor 1.0 until there are 5, then the **maximum** ratio, clamped to 0.7–1.3.
`GUARDIAN_CALIBRATE=0` turns it off at runtime. On Ollama the window itself comes from `/api/show` (0.10.0).

## Alternatives

- **A real tokenizer per model**: exact, but needs model-specific vocab files or a dependency like `tiktoken` that
  is wrong for most local models anyway, and a lookup per model name the proxy cannot guarantee.
- **Ask the backend to count** (a tokenize endpoint): not part of the OpenAI-compatible surface; an extra round trip
  on every request.
- **Average the ratios**: a warm KV cache makes Ollama report only the tokens it evaluated, so averaging drags the
  estimate down and compaction fires late, which reintroduces the hard stop. Hence the maximum.

## Consequences

- Zero extra dependencies and no per-request cost; the estimate errs high, so compaction fires a little early.
- Accuracy depends on the backend reporting usage; streaming clients only report it with `stream_options`
  (`GUARDIAN_INJECT_STREAM_OPTIONS=1`, opt-in).
- The README says plainly that this is not tokenizer-accurate.
