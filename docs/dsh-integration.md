# Context Guardian inside DSH

## What you get

- Compaction that cannot brick a conversation: the stock LLM summary runs first; if it fails, returns nothing, or cannot fit in the window, the span is compiled deterministically instead (no model call, milliseconds)
- Every checkpoint line carries a `seq` pointer; `recall` and `search` tools (for the model) and `/recall`, `/context` commands (for you) read the original text back from the append-only session log
- Harness-injected context (workspace instructions, skill list, runtime notes) is left out of checkpoints and named by seq
- Idle pressure trigger: above 45% of the window it compacts when the agent goes idle
- Every compacted span is archived on disk in the same format the Context Guardian proxy uses

## Why it exists

On a build machine with a 32,768-token local model, every DSH session log counted on 2026-09-20: 314 compaction attempts, 48 succeeded; 145 ended with `summarization produced no text summary content`; 116 ended in a context overflow of the summariser's own request, because it replays system prompt + tools + the whole span and asks for output on top. Deterministic checkpointing eliminates both failure modes.

## Two pieces, two planes

DSH mounts compaction inside the AGENT PRESET (the `compaction` group), not in the profile. So there are two entry files: `index.js` is the profile bundle (settings + optional Python bridge), `engine.js` is one row inside your agent preset. `engine.js` imports nothing from DSH, so it works from a checkout anywhere on disk.

## Install the engine (5 minutes)

1. Get the code — either way works, and you need the absolute path of `engine.js` for step 3:
   - **From npm (recommended):** `dsh plugin --profile <your-profile> add dsh-context-guardian`. The engine then sits at `<DSH_HOME>/profiles/<your-profile>/node_modules/dsh-context-guardian/engine.js`.
   - **From git:** `git clone https://github.com/LuminariSoftwares/context-guardian` anywhere on disk. No `pnpm install` is needed for the engine; it has no dependencies.

2. Make your own agent preset if you do not have one: copy the shipped `standard` preset folder to `<DSH_HOME>/.agent-presets/<your-id>/` (it contains `agent.cordis.yml` and `preset.yml`). `<DSH_HOME>` is `~/.dsh` unless you set the `DSH_HOME` environment variable.

3. Back up `agent.cordis.yml`, then add this row at the END of the `config:` list of the group whose `id` is `compaction` (after `tool-result-pruner`), indented exactly like its siblings:
   ```yaml
       - id: context-guardian
         name: 'file:///C:/path/to/context-guardian/engine.js'
         config:
           mode: llm-then-deterministic
           numCtx: 32768
           idleCompactRatio: 0.45
           tools: [recall, search]
   ```
   In the shipped preset the sibling rows (`- id: tool-result-pruner`) start at 4 spaces; match whatever yours use.
   Note: `name` is a `file:///` URL to the `engine.js` from step 1 — for an npm install that is `file:///C:/Users/<you>/.dsh/profiles/<your-profile>/node_modules/dsh-context-guardian/engine.js`. It is a URL to YOUR copy (forward slashes, also on Windows), or a path starting with `./` relative to the preset folder if you copy `engine.js`, `cg_recall.js` and `vendor/compiler.js` next to it. `numCtx` must be your model's real context window.

4. Start a NEW session with that preset selected. No restart is needed: DSH re-mounts a preset whose file changed when the next session starts.

5. Success looks like: typing `/` in the message box lists `recall` and `context`; the file `logs/guardian_dsh.jsonl` in the checkout (or your `logPath`) gets a line with `"event":"installed"`.

## Check it works

1. In a session type `/context` and pick it: you get a pressure line like `context: ~14089 of 32768 tokens (43%), tier watch`.

2. Work until the agent goes idle above 45%: `guardian_dsh.jsonl` gets `idle-check`, `compaction/start`, `deterministic` or `llm`, `compaction/end` with `"error":null`, and `idle-compaction` with `before` and `after` token counts.

3. `/recall result <seq>` with a seq taken from a checkpoint line like `* read "scripts/x.py" (seq 29 -> result 42)` prints the original tool result. Example: idle trigger at 71%, 15 nodes, ~14,012 -> ~584 tokens, surface 23,395 -> 10,043 tokens, 23 ms, no model call.

## Options

| option | default | environment override | what it does |
|--------|---------|----------------------|--------------|
| mode | llm-then-deterministic | GUARDIAN_DSH_MODE | try LLM summary; fall back to deterministic on failure, empty output, or overflow |
| numCtx | 32768 | GUARDIAN_NUM_CTX | your model's context window in tokens |
| reserveOutput | 8192 | GUARDIAN_RESERVE_OUTPUT | space held for the summarizer's output |
| idleCompactRatio | 0.45 | GUARDIAN_IDLE_COMPACT_RATIO | trigger compaction when idle and pressure exceeds this fraction |
| idleDelayMs | 4000 | GUARDIAN_IDLE_DELAY_MS | wait this many milliseconds after the agent goes idle before checking pressure |
| checkpointMaxTokens | 3000 | GUARDIAN_CHECKPOINT_MAX_TOKENS | deterministic checkpoint length cap, adjusted down at high pressure |
| textTokens | 200 | GUARDIAN_TEXT_TOKENS | tokens allowed per text block in checkpoint |
| userTextTokens | 400 | GUARDIAN_USER_TEXT_TOKENS | tokens allowed per user message in checkpoint |
| toolCallTokens | 32 | GUARDIAN_TOOL_CALL_TOKENS | tokens allowed per tool call in checkpoint |
| toolResultExcerptTokens | 64 | GUARDIAN_TOOL_RESULT_TOKENS | tokens allowed per tool result excerpt in checkpoint |
| maxRecallTokens | 16000 | GUARDIAN_MAX_RECALL_TOKENS | maximum tokens per recall (capped at numCtx / 4) |
| maxSearchHits | 50 | GUARDIAN_MAX_SEARCH_HITS | maximum search results returned |
| keywordTerms | 25 | GUARDIAN_KEYWORD_TERMS | keyword index entries in checkpoint |
| filesListed | 15 | GUARDIAN_FILES_LISTED | file modifications to list in checkpoint |
| keepSpans | 500 | GUARDIAN_KEEP_SPANS | how many archived spans to keep on disk |
| spanDir | logs/guardian_spans | GUARDIAN_SPAN_DIR | directory for span archives |
| logPath | logs/guardian_dsh.jsonl | GUARDIAN_DSH_LOG | append-only event log (JSON lines) |
| tools | [recall, search] | GUARDIAN_DSH_TOOLS | which tools to expose to the model |
| toolArgTools | [] (the compiler's default list) | GUARDIAN_TOOL_ARG_TOOLS | tools whose checkpoint line keeps its key argument (comma-separated in the env var) |
| hideTools | [] | GUARDIAN_HIDE_TOOLS | tools that get no checkpoint line at all |
| dropSources | agent-instructions, skill-catalog, @deepseek-ai/dsh-system-prompt, repeat-tool-reminder | GUARDIAN_DROP_SOURCES | harness-injected message kinds left out of checkpoints |

Precedence: environment variable > preset row config > default. The `maxRecallTokens` option is additionally capped at a quarter of `numCtx`.

## Commands and tools

| command or tool | usage | what it does |
|-----------------|-------|--------------|
| /recall | /recall 3-7, /recall result 3, /recall checkpoint 1, /recall find text | restore original text behind a seq pointer |
| /context | /context | show context pressure, cache stats, compaction savings estimate |
| recall (tool) | called by model | restore original text to the model |
| search (tool) | called by model | find where something was said; returns seqs for recall |
| context_rewrite_cost (tool) | called by model | report pressure and compaction cost-benefit (opt-in) |
| context_compact (tool) | called by model | request compaction at end of this turn (opt-in) |

## Updating the engine without restarting DSH

Node caches an imported file for the life of the process. After you change `engine.js`, change the row's `name` to end in `engine.js?v=2` (then `?v=3`, ...) and start a new session; the `installed` line in `guardian_dsh.jsonl` shows the engine revision that loaded.

## Turn it off

Set environment variable `GUARDIAN_DSH_MODE=off` before starting DSH, or delete the row. Stock compaction-basic behaviour returns; nothing else changes.

## Migrating from dsh-openwolf / dsh-compaction-instant / dsh-trim

- openwolf's pre-compaction snapshot is covered by the `precompact-<seq>.json` file written next to the span archive plus the FILES WRITTEN list in each checkpoint
- compaction-instant's compiler is vendored here (MIT, see vendor/LICENSE.dsh-compaction-instant) and keeps the same `recall`/`search` contract
- dsh-trim's result shaping lives in the companion plugin dsh-tool-guardian. Do not run compaction-instant and this engine in the same preset.

## Troubleshooting

| symptom | cause | fix |
|---------|-------|-----|
| `/context` says the token meter is not available | the preset does not provide `tokenMeter` to the row | keep the row INSIDE the `compaction` group of a preset copied from `standard` |
| typing `/recall result 42` sends the text to the model | engine older than cg-engine-3 is loaded | use the `?v=N` step above and start a new session |
| new sessions fail to start after editing the preset | YAML indentation of the new row is wrong | restore your backup of `agent.cordis.yml`, re-add the row with the same indentation as `tool-result-pruner` |
| no `guardian_dsh.jsonl` appears | the session is not using your preset | pick the preset in the new-session screen, or set it as default in DSH settings (`agent-presets: default: <your-id>`) |
