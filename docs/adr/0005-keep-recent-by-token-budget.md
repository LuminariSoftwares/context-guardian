# 0005. Keep recent messages by token budget

**Status:** accepted. Token budget default since 0.6.0 (`7c8e213`); short conversations compact since 0.10.0.

## Context

Compaction keeps the newest messages verbatim and summarises the rest. A fixed count ("keep the newest 8") treats
a 40 KB tool result and a "hi" the same: it could keep 90 % of the window, so compaction freed nothing, or keep far
too little in a chatty session. Separately, an OpenAI tool call and its result must stay together, or the backend
rejects the request.

## Decision

By default keep the newest messages totalling at most `GUARDIAN_KEEP_RECENT_FRACTION` (20 %) of the usable window
(`NUM_CTX - RESERVE_OUTPUT`), never fewer than 4. Never split an assistant tool call from its tool results; on the
Anthropic route the kept window always opens on a user turn. `GUARDIAN_KEEP_RECENT_MESSAGES=N` restores an exact
count for anyone who needs the old behaviour. Reserve output tokens explicitly (`GUARDIAN_RESERVE_OUTPUT`, 8192):
logs showed that a model squeezed for room to answer falls back to worse tool calls and then fabricates.

## Alternatives

- **Fixed message count**: the old default; kept as an explicit opt-in.
- **Keep by turns**: same problem as a count, with a less predictable size.
- **Summarise everything but the last message**: maximum savings, but the model loses the working context it is in
  the middle of using.

## Consequences

- Savings scale with the window and with message size, not with message count.
- The startup line and `/guardian/stats` (`keep_recent_label`) say which mode is active, because the two behave
  differently enough to confuse a user reading logs.
