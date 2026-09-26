# Proposal for DeepSeek Harness: let a compaction engine keep a stable checkpoint head

*Issue text for `deepseek-ai/deepseek-harness`, written against 0.1.2-alpha.2 (source read 2026-09-25). Context Guardian works around part of this today; this is what would close the rest.*

---

**Title:** compaction: let `compactNow` (and a head-selection hook) honour an engine's `compactRegion` so checkpoints can be appended instead of rewritten

**Problem**

`selectCompactableRange` (`packages/compaction/compaction-basic/src/region.ts`) always returns a range that starts at `session.surface.nodes[0]`. As a result, every compaction replaces the whole head of the surface, including earlier checkpoints, with one new summary node. The prompt prefix changes on every compaction, so a provider's prefix/KV cache cannot be reused across compactions. On local models (llama.cpp, Ollama) that means re-prefilling system prompt + tools + all history after each one. The earlier checkpoints are also re-summarised every time, which loses detail.

**What already works**

`CompactionEngine.compactRegion(start, end, agent, signal)` is public, and `BasicCompactionEngine.compactIfNeeded` calls it through `this.compactRegion(...)` on both the `pressure` and `context-overflow` paths. A plugin can therefore wrap `compactRegion` on the service instance and move `start` past a leading run of checkpoint nodes. The new checkpoint is then appended after the old ones, and the prefix up to them is byte-stable. `validateSurfaceRegion` rejects a start that is not a balanced boundary before anything durable happens, so a wrong guess is safe. `dsh-context-guardian` ships this as an opt-in (`appendOnly: true`).

**What does not**

`BasicCompactionEngine.compactNow` (manual `/compact`, and every plugin that compacts an idle session) calls `selectCompactableRange` and then `compactSurfaceRegion(...)` directly, with `owner: null`. It never goes through `this.compactRegion`, and it cannot, because `compactRegion` uses `owner: 'current-turn'`, which requires an open turn. So idle-time and manual compactions always rewrite from the head.

**Proposal (either is enough; the first is the smaller change)**

1. Add a protected, overridable range-selection hook used by BOTH paths:
   ```ts
   /** Choose the inclusive surface range to compact. Default: selectCompactableRange(...). */
   protected selectRange(agent: Agent, measurement: TokenMeasurement, retainTokens: number): { start: number; end: number } | null
   ```
   `compactIfNeeded` and `compactNow` would call `this.selectRange(...)` instead of `selectCompactableRange(...)`. An engine could then skip a checkpoint head in one place, for every trigger. Validation stays exactly as it is.
2. Or: give `selectCompactableRange` an optional `firstCompactable?: (surfaceSeqs: readonly number[]) => number` (default `() => 0`), and expose it through the engine config so that a preset row can set it.

**Why it is safe**

The range still ends on a balanced boundary chosen by the existing logic. The start is validated by `validateSurfaceRegion`, which already exists and already rejects unbalanced starts. Nothing about the durable transaction, the shrink guarantee, or the `compaction/start` / `compaction/end` markers changes. The default behaviour is unchanged.

**How an engine would use it**

Keep up to N leading checkpoint nodes, or up to T tokens of them, and compact from the first non-checkpoint node. Once the head passes the bound, compact from the head (a roll-up), which re-summarises the chain into one checkpoint. This is what `dsh-context-guardian`'s `planChainStart` does on the paths it can reach today.
