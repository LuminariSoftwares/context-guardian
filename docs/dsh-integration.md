# Context Guardian inside DSH

## What you get

- **Compaction that cannot brick a conversation.** The stock LLM summary runs first. If it fails, returns nothing, or cannot fit in the window, the span is compiled deterministically instead (no model call, milliseconds).
- **Pointers back to the original text.** Every checkpoint line carries a `seq` pointer. The model gets `recall` and `search` tools, and you get the `/recall` and `/context` commands; all of them read the original text back from the append-only session log.
- **The session's first request, pinned.** It stays byte-for-byte at the top of every checkpoint, and `goal:` messages are kept as numbered updates.
- **Persistent memory.** Decisions, constraints, files, to-dos, errors and preferences are pulled out of the conversation on every compaction (no model call). They are saved to `memory.json` and shown at the top of every checkpoint, in this session and in the next one.
- **A durable file list.** Files written before an earlier compaction are carried forward, not dropped after 15.
- **`/guardian`.** Prints the engine revision, the window in use, pressure, the last compaction and why (llm / deterministic / overflow), the pinned goal, memory and archived spans. **`/recall digest`** answers "what did I miss?".
- **A recall budget per turn.** The model cannot flood the window with recalls in one turn.
- **Clean checkpoints.** Harness-injected context (workspace instructions, skill list, runtime notes) is left out of checkpoints and named by seq, with a warning in the log the first time each kind is dropped.
- **An idle pressure trigger.** Above 45% of the window it compacts when the agent goes idle.
- **An archive on disk.** Every compacted span is archived in the same format the Context Guardian proxy uses.
- **Opt-in append-only checkpoints** (experimental): pressure compactions append a new checkpoint after the old ones instead of rewriting them, so the provider's prompt cache survives.

## Why it exists

The build machine runs a 32,768-token local model. On 2026-09-20 every DSH session log on it was counted:

| outcome | attempts |
|---|---|
| compaction attempts | 314 |
| succeeded | 48 |
| failed: `summarization produced no text summary content` | 145 |
| failed: context overflow of the summariser's own request | 116 |

The summariser overflows because it replays the system prompt, the tools and the whole span, and then asks for output on top. Deterministic checkpointing removes both failure modes.

## Two pieces, two planes

DSH mounts compaction inside the AGENT PRESET (the `compaction` group), not in the profile, so there are two entry files:

- `index.js` is the profile bundle (settings and the optional Python bridge);
- `engine.js` is one row inside your agent preset. It imports nothing from DSH, so it works from a checkout anywhere on disk.

A plugin's `cordis.patch.yml` cannot add that row for you. It only composes the profile tree, while DSH mounts agent presets separately and never patches them (DSH `boot/app-boot/src/profile.ts`, `preset/agent-presets/src/mount.ts`). That is why there is a setup command.

## Install (2 minutes): `npm run setup`

1. Get the package into a DSH profile. `web` is the profile `dsh web` uses:
   ```bash
   dsh plugin --profile web add dsh-context-guardian
   ```
2. Let setup add the engine row to your agent preset. It is a dry run first; nothing changes until `--apply`:
   ```bash
   cd ~/.dsh/profiles/web/node_modules/dsh-context-guardian     # Windows: cd %USERPROFILE%\.dsh\profiles\web\node_modules\dsh-context-guardian
   npm run setup                  # shows the preset it picked, the exact row and the file:/// path
   npm run setup -- --apply       # backs up agent.cordis.yml, inserts the row, re-checks it
   ```
   - Setup finds your DSH home (`--dsh-home`, else `DSH_HOME`, else `~/.dsh`).
   - It picks the preset: `--preset <id>`, else your default preset, else your only preset. If you have none, it makes a `guardian` preset from DSH's shipped `standard` one.
   - It writes a backup `agent.cordis.yml.bak-context-guardian-<time>` beside the file.
   - It inserts the row at the end of the `compaction` group, and changes nothing else in the file.
   - If the row is already there, it says so and does nothing.
   - If the row points at an older copy of `engine.js`, it updates only that `name:` line.
   - Setup never touches anything outside that one preset folder.
3. Start a NEW session with that preset selected. No DSH restart is needed: DSH re-reads a preset when the next session starts.
4. Type `/guardian`. You should see `context-guardian cg-engine-...` and your model's window.

A checkout works too: `git clone https://github.com/LuminariSoftwares/context-guardian`, then run `node setup.mjs` in it. The row then points at that checkout's `engine.js`.

### Manual install (what setup does)

1. Make your own agent preset if you do not have one. Copy the shipped `standard` preset folder to `<DSH_HOME>/.agent-presets/<your-id>/`; it holds `agent.cordis.yml` and `preset.yml`. `<DSH_HOME>` is `~/.dsh` unless you set `DSH_HOME`. A user preset with the same id as a shipped one is ignored, so pick a new id.
2. Back up `agent.cordis.yml`. Then add this row at the END of the `config:` list of the group whose `id` is `compaction` (after `tool-result-pruner`), indented exactly like its siblings:
   ```yaml
       - id: context-guardian
         name: 'file:///C:/path/to/context-guardian/engine.js'
         config:
           mode: llm-then-deterministic
           idleCompactRatio: 0.45
           tools: [recall, search]
   ```
   - `name` is a `file:///` URL to YOUR copy of `engine.js`, with forward slashes, on Windows too. For an npm install that is `file:///C:/Users/<you>/.dsh/profiles/<profile>/node_modules/dsh-context-guardian/engine.js`.
   - Leave `numCtx` out: the engine reads your model's real context window from DSH.
3. Start a new session with that preset selected.

## First 5 minutes

This walkthrough starts from a fresh DSH install and ends with a compaction you can read back.

1. **Install** as above, then `dsh web` and open a new session with the preset setup named.
2. **Check that it loaded.** Type `/guardian`:
   ```
   context-guardian cg-engine-4 (memory cg-memory-1, ...) mode llm-then-deterministic
   window: 32768 tokens (reported by the model)
   pressure: ~3120 of 32768 tokens (10 %), tier none
   last compaction: none yet in this session
   goal: none yet
   memory: 0 items (...) in .../logs/guardian_spans/memory.json
   append-only: off
   spans archived: 0 in this run (...)
   recall this turn: 0 of 4
   ```
   The log file (`logs/guardian_dsh.jsonl` beside `engine.js`, or your `logPath`) has one `"event":"installed"` line.
3. **Give it a task that reads a lot.** For example: "Read every file under src/ and summarise what each does. decision: keep the public API unchanged. todo: list the files without tests." Lines that start with `decision:`, `constraint:`, `todo:`, `error:` or `preference:` go into memory as written.
4. **Let it go idle.** When the agent stops above 45% of the window, the idle trigger compacts after 4 seconds. The log shows, in order: `idle-check`, `compaction/start`, `memory`, `deterministic` (or `llm`), `compaction/end` (with `"error":null`), and `idle-compaction` (with `before`/`after` token counts). DSH shows its own "Context compacted" row.
5. **See what survived.** `/recall digest` prints the pinned goal, the memory items this session added, the files written, open to-dos, the last error and the last compaction. `/guardian` now says `last compaction: deterministic (...)` or `llm (...)`.
6. **Read an original back.** Take a seq from the checkpoint, e.g. `* read "src/app.py" (seq 29 -> result 42)`, and type `/recall result 42`. You get the full original tool result, straight from the session log.
7. **Start a second session** with the same preset. At its first compaction, the memory block (`[memory -- durable notes ...]`) carries the decision and the to-do from session 1.

## Check it works

1. In a session, type `/context` and pick it. You get a pressure line like `context: ~14089 of 32768 tokens (43%), tier watch`.
2. Work until the agent goes idle above 45%. `guardian_dsh.jsonl` gets these lines:
   - `idle-check`;
   - `compaction/start`;
   - `memory`;
   - `deterministic` or `llm`;
   - `compaction/end` with `"error":null`;
   - `idle-compaction` with `before` and `after` token counts.
3. Type `/recall result <seq>`, with a seq taken from a checkpoint line like `* read "scripts/x.py" (seq 29 -> result 42)`. It prints the original tool result.

   Example run: the idle trigger fired at 71%. It compacted 15 nodes from ~14,012 to ~584 tokens, and the surface went from 23,395 to 10,043 tokens, in 23 ms with no model call.

## Memory

**Where it lives.** `memory.json` sits beside the span archive (`<spanDir>/memory.json`), or at `memoryPath` / `GUARDIAN_MEMORY_PATH`. It is plain JSON; you can read it, edit it or delete it. Every session that uses the same path shares it. For separate memories per project, give each project's preset (or environment) its own `memoryPath`.

**What goes in.** On every compaction, the region being compacted is scanned with fixed rules; there is no model call:

| category | taken from |
|---|---|
| decisions | lines starting `decision:`, `decided:` or `remember:`; user lines like "let's use X" or "we'll go with X" |
| constraints | lines starting `constraint:` or `rule:`; user lines starting "never", "always", "do not", "don't" or "must" |
| files | every write / edit / patch / create / replace tool call (up to 200 paths), and the file lists of earlier checkpoints |
| todos | lines starting `todo:`, `next:` or `next step:`; `- [ ] x` checkboxes (`- [x] x` closes one) |
| errors | the first error line of a failed tool result (`Error`, `Exception`, `Traceback`, `FAILED`, `ENOENT` …) |
| preferences | lines starting `preference:` or `prefer:`; user lines like "I prefer …" |

Duplicates merge: the same fact is kept once, with a count and a last-seen time. Each category has a cap, and the oldest items go first.

**What the model sees.** A `[memory -- ...]` block after the pinned goal in every checkpoint. It is capped at `memoryMaxTokens` (1200), and the categories are admitted in this order: constraints, preferences, decisions, to-dos, files, errors (newest first). `memoryMaxTokens: 0` turns memory off completely.

**Failure.** A memory file that cannot be read is renamed to `memory.json.corrupt-<time>`, and the engine starts fresh. A memory file that cannot be written is logged. In both cases, compaction goes on without it.

## Recall budget

Each `recall` is capped at `maxRecallTokens`, and at a quarter of the window. A model that recalls several times in one turn could still fill the window. After `recallMaxPerTurn` recalls (4) in one turn, the tool answers with a note telling the model to use `search` and recall a narrower range next turn. The counter resets when the agent goes idle. Your own `/recall` command is never limited.

## Append-only checkpoints (experimental, opt-in)

By default, every compaction replaces everything from the start of the conversation, including earlier checkpoints, with one new checkpoint. The prompt prefix changes each time, so a local server's prompt cache is lost.

With `appendOnly: true`, a compaction that DSH starts on its own under pressure or on overflow keeps the leading checkpoints and compacts only what comes after them. The new checkpoint is appended after the old ones, and the prefix up to them does not change. In an appended checkpoint, the pinned goal is not repeated (only new `goal:` updates are), and the memory block shows only the items this compaction touched. When the chain passes `chainMaxCheckpoints` (4), or `chainMaxTokens` (auto: the smaller of 2 × `checkpointMaxTokens` and 15% of the window), or pressure is at 90% or more, the next compaction rolls the whole chain up into one checkpoint.

**The limitation.** The idle trigger, the model's `context_compact` and your manual `/compact` go through a DSH path (`compactNow`) that always starts at the head. Those compactions roll up. Closing that gap needs a small DSH change; the proposal is in [dsh-upstream-append-only.md](dsh-upstream-append-only.md). The log shows `"event":"chain"` with `action` set to `append`, `roll-up`, `full` or `fallback` for each compaction.

## Options

| option | default | environment override | what it does |
|--------|---------|----------------------|--------------|
| mode | llm-then-deterministic | GUARDIAN_DSH_MODE | try the LLM summary; fall back to deterministic on failure, empty output or overflow (`deterministic`, `off`) |
| numCtx | the model's window, as DSH reports it | GUARDIAN_NUM_CTX | set only to force a smaller window |
| reserveOutput | 8192 | GUARDIAN_RESERVE_OUTPUT | space held for the summarizer's output |
| idleCompactRatio | 0.45 | GUARDIAN_IDLE_COMPACT_RATIO | compact when idle and pressure exceeds this fraction |
| idleDelayMs | 4000 | GUARDIAN_IDLE_DELAY_MS | wait this long after the agent goes idle before checking pressure |
| checkpointMaxTokens | 3000 | GUARDIAN_CHECKPOINT_MAX_TOKENS | deterministic checkpoint length cap, lowered at high pressure |
| textTokens | 200 | GUARDIAN_TEXT_TOKENS | tokens allowed per text block in a checkpoint |
| userTextTokens | 400 | GUARDIAN_USER_TEXT_TOKENS | tokens allowed per user message in a checkpoint |
| toolCallTokens | 32 | GUARDIAN_TOOL_CALL_TOKENS | tokens allowed per tool call in a checkpoint |
| toolResultExcerptTokens | 64 | GUARDIAN_TOOL_RESULT_TOKENS | tokens allowed per tool result excerpt in a checkpoint |
| maxRecallTokens | 16000 | GUARDIAN_MAX_RECALL_TOKENS | maximum tokens per recall (also capped at a quarter of the window) |
| recallMaxPerTurn | 4 | GUARDIAN_RECALL_MAX_PER_TURN | recalls the model may make in one turn; 0 = unlimited |
| maxSearchHits | 50 | GUARDIAN_MAX_SEARCH_HITS | maximum search results returned |
| keywordTerms | 25 | GUARDIAN_KEYWORD_TERMS | keyword index entries in a checkpoint |
| filesListed | 15 | GUARDIAN_FILES_LISTED | files on the FILES WRITTEN line (the memory file list keeps up to 200) |
| memoryMaxTokens | 1200 | GUARDIAN_MEMORY_MAX_TOKENS | size of the memory block; 0 turns memory off |
| memoryPath | `<spanDir>/memory.json` | GUARDIAN_MEMORY_PATH | where memory is kept between sessions |
| appendOnly | false | GUARDIAN_APPEND_ONLY | append checkpoints after the existing ones on pressure/overflow compactions (experimental) |
| chainMaxCheckpoints | 4 | GUARDIAN_CHAIN_MAX_CHECKPOINTS | roll the chain up once it holds this many checkpoints |
| chainMaxTokens | 0 (auto) | GUARDIAN_CHAIN_MAX_TOKENS | roll the chain up once it is this large; auto = min(2 × checkpointMaxTokens, 15% of the window) |
| keepSpans | 500 | GUARDIAN_KEEP_SPANS | how many archived spans to keep on disk |
| spanDir | logs/guardian_spans | GUARDIAN_SPAN_DIR | directory for span archives |
| logPath | logs/guardian_dsh.jsonl | GUARDIAN_DSH_LOG | append-only event log (JSON lines) |
| tools | [recall, search] | GUARDIAN_DSH_TOOLS | tools exposed to the model (also: `context_rewrite_cost`, `context_compact`, `guardian_status`) |
| toolArgTools | [] (the compiler's default list) | GUARDIAN_TOOL_ARG_TOOLS | tools whose checkpoint line keeps its key argument (comma-separated in the env var) |
| hideTools | [] | GUARDIAN_HIDE_TOOLS | tools that get no checkpoint line at all |
| dropSources | agent-instructions, skill-catalog, @deepseek-ai/dsh-system-prompt, repeat-tool-reminder | GUARDIAN_DROP_SOURCES | harness-injected message kinds left out of checkpoints; the first drop of each kind per session is logged as a warning |

Precedence: environment variable > preset row config > default.

## Commands and tools

| command or tool | usage | what it does |
|-----------------|-------|--------------|
| /guardian | /guardian | status: engine revision, window, pressure, last compaction and why, pinned goal, memory, append-only, spans archived, recalls this turn |
| /recall | /recall 3-7, /recall result 3, /recall checkpoint 1, /recall find text, /recall digest | restore the original text behind a seq pointer; `digest` shows what the last compactions kept |
| /context | /context | show context pressure, cache stats and a compaction savings estimate |
| recall (tool) | called by model | restore original text to the model (at most `recallMaxPerTurn` per turn) |
| search (tool) | called by model | find where something was said; returns seqs for recall |
| context_rewrite_cost (tool) | called by model | report pressure and compaction cost-benefit (opt-in) |
| context_compact (tool) | called by model | request compaction at the end of this turn (opt-in) |
| guardian_status (tool) | called by model | the same text as `/guardian` (opt-in) |

## Updating the engine without restarting DSH

Node caches an imported file for the life of the process. After you change `engine.js`, change the row's `name` to end in `engine.js?v=2` (then `?v=3`, and so on) and start a new session.

After `dsh plugin ... add dsh-context-guardian` installs a newer version, run `npm run setup -- --apply` again from the new package folder. If the row points at a different folder, setup rewrites only its `name:` line, and the new engine loads in the next session. If the folder is the same (an in-place upgrade), setup reports `already installed`, but a running DSH still has the old `engine.js` cached: restart DSH, or bump the `?v=N` suffix as above. The `installed` line in `guardian_dsh.jsonl`, and `/guardian`, show the engine revision that loaded.

## Turn it off

Either set the environment variable `GUARDIAN_DSH_MODE=off` before starting DSH, or delete the row (setup's backup file is the preset from before the row was added). Stock compaction-basic behaviour returns, and nothing else changes.

## Migrating from dsh-openwolf / dsh-compaction-instant / dsh-trim

- **dsh-openwolf.** Its pre-compaction snapshot is covered by the `precompact-<seq>.json` file written next to the span archive, plus the file list in each checkpoint and in memory.
- **dsh-compaction-instant.** Its compiler is vendored here (MIT, see vendor/LICENSE.dsh-compaction-instant) and keeps the same `recall`/`search` contract. Do not run compaction-instant and this engine in the same preset.
- **dsh-trim.** Its result shaping lives in the companion plugin dsh-tool-guardian.

## Troubleshooting

| symptom | cause | fix |
|---------|-------|-----|
| `npm run setup` says `no DSH home` | DSH keeps its home elsewhere | pass `--dsh-home <dir>` or set `DSH_HOME` |
| setup exits 2: no preset and no shipped `standard` found | DSH is installed somewhere setup does not look | pass `--standard <dir of DSH's presets/standard>`, or copy that folder to `<DSH_HOME>/.agent-presets/<id>/` and re-run |
| `/guardian` is not in the `/` list | the session is not using the preset with the row | pick the preset in the new-session screen, or set `agent-presets: default: <id>` in `<DSH_HOME>/settings.yaml` |
| `/context` says the token meter is not available | the preset does not provide `tokenMeter` to the row | keep the row INSIDE the `compaction` group of a preset copied from `standard` |
| typing `/recall result 42` sends the text to the model | an engine older than cg-engine-3 is loaded | use the `?v=N` step above and start a new session |
| new sessions fail to start after editing the preset | the YAML indentation of the new row is wrong | restore the `agent.cordis.yml.bak-...` backup and run `npm run setup -- --apply` instead of hand-editing |
| no `guardian_dsh.jsonl` appears | the session is not using your preset | as for `/guardian` above |
| a `warn` about `dropSources` in the log | a message of a harness-injected kind was left out of a checkpoint | usually fine (it is still in the session log). If it was your own text, remove that kind from `dropSources` |
