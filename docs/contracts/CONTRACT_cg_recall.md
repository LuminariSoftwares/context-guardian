# CONTRACT: `cg_recall.js`

ONE file, ESM (`export`), Node >= 20, ZERO imports except `node:` builtins (only needed for the CLI selftest).
Pure functions over an array of NODES. No I/O outside `--selftest`. Deterministic. Never throws on bad input — returns an error shape.

## Data shapes (given, do not change)

    node    = { seq: <int>, message: <Message|null> }
    Message = { role: 'user'|'assistant', content: Block[], source?: {kind, plugin?} }
    Block   = {type:'text', text}
            | {type:'reasoning', text}
            | {type:'tool-call', id, name, arguments: <JSON string>}
            | {type:'tool-result', toolCallId, isError?: boolean, content: Block[]}   // always FIRST block of a user message
            | {type:'image'} | {type:'document'}

A CHECKPOINT node is a user message whose `source.kind === 'plugin' && source.plugin === 'compact'`.

## Exports

1. `RECALL_REV` — the string `'cg-recall-1'`.
2. `estTokens(text)` → `Math.ceil(String(text).length / 4)`; `''`/null/undefined → 0.
3. `parseRecallRequest(type, id)` →
   `{ok:true, type:'seq'|'result'|'checkpoint', from:<int>, to:<int>}` or `{ok:false, error:<string>}`.
   - `type` missing/empty → `'seq'`. Case-insensitive, trimmed. Any other type → error.
   - `id` may be a number or string. `"7"` → from=to=7. `"3-7"`, `"3 - 7"`, `"3..7"` → 3,7. `"7-3"` → swapped to 3,7.
   - `result` and `checkpoint` accept ONLY a single integer (a range → error).
   - negative, non-integer, empty → error. The error string must contain the text `seq "3-7"` as an example of a valid form.
4. `parseRecallCommand(raw)` — the argument string of `/recall`: `"seq 3-7"`, `"3-7"`, `"7"`, `"result 3"`, `"checkpoint 1"`; extra whitespace OK.
   Same return shape as (3). Empty → `{ok:false,…}`.
5. `renderMessage(message)` → string. `null`/undefined → `''`.
   - text block → its text verbatim. reasoning block → SKIPPED entirely.
   - tool-call → one line `* <name>(<arguments string verbatim>)`.
   - tool-result → first line `[result of <toolCallId>]` (or `[ERROR result of <toolCallId>]` when isError), then the nested text blocks verbatim; nested image → `[image]`, document → `[document]`.
   - top-level image → `[image]`, document → `[document]`. Unknown type → `[<type>]`.
   - blocks joined with `\n`.
6. `recall(nodes, request, opts)` where `request` is the ok-shape of (3), `opts = {maxTokens = 16000}` →
   `{ok:boolean, text:string, seqs:number[], truncated:boolean, tokens:number}`.
   - type `seq`: every node with `from <= seq <= to` and a non-null message, in seq order, each rendered
     `[seq <N> <role>]\n<renderMessage>`; nodes joined by a blank line.
     Budget: add whole nodes while `estTokens(text so far + next)` <= maxTokens. If the FIRST node alone exceeds the budget, include it cut to `maxTokens*4` characters.
     When anything was left out: `truncated:true` and the text ends with the line
     `[recall truncated — next: recall(type="seq", id="<firstOmittedSeq>-<to>")]`. `seqs` lists only the included seqs.
   - type `result`: if node `from` is a tool-result message → return it. If node `from` is an assistant message with tool-call blocks → return the tool-result node(s) whose `toolCallId` matches any of its call ids. Same budget rule.
   - type `checkpoint`: the Nth (1-based, in seq order) CHECKPOINT node.
   - nothing matched → `{ok:false, text:'NOT FOUND: …', seqs:[], truncated:false, tokens:<n>}`; the text names the valid seq range present: `seqs <min>-<max>`.
   - `tokens` = `estTokens(text)` always.
7. `search(nodes, query, opts)` with `opts = {maxHits = 50, regex = false, contextChars = 80}` →
   `{ok:true, hits:[{seq, role, snippet}], total:<int>, capped:boolean}` or `{ok:false, error}`.
   - Searches `renderMessage(node.message)`; case-insensitive. `regex:false` → literal substring (regex metacharacters are literal). `regex:true` → `new RegExp(query,'i')`; invalid pattern → `{ok:false, error}` (no throw). Empty/blank query → `{ok:false, error}`.
   - ONE hit per node (first match). `snippet` = up to `contextChars` chars each side of the match, newlines/tabs collapsed to single spaces, `…` prefixed/suffixed when cut.
   - `total` = number of matching nodes; `hits` = the first `maxHits` in seq order; `capped = total > maxHits`.
8. `renderSearch(result, query)` → string for the model.
   - error → `search error: <error>`.
   - zero hits → a line containing `0 hits` and the query.
   - otherwise one line per hit: `seq <N> [<role>] <snippet>`, a header line `<total> hits for "<query>"` (append ` (showing first <hits.length>)` when capped),
     and a LAST line exactly: `NEXT STEP: recall(type="seq", id="<seq of first hit>")`.
9. `foldKeywordIndex(nodes, opts)` with `opts = {maxTerms = 40, minLen = 4, maxSeqsPerTerm = 6}` → `[{term, seqs:number[]}]`.
   - Terms = file-path-like or identifier-like tokens from `renderMessage`: regex `/[A-Za-z_][\w.\-\/\\]{3,}/g`, lowercased, trailing `.`/`,` stripped; drop a term shorter than `minLen` or in a small English stopword list (at least: this, that, with, from, have, will, your, were, been, they, their, there, what, when, which, would, could, should, about, into, then, than, them, some, only, also, more, here, just, like).
   - Keep terms that occur in >= 2 DIFFERENT nodes. Rank by number of distinct nodes desc, then term asc. Top `maxTerms`. `seqs` ascending, distinct, first `maxSeqsPerTerm`.
10. `renderKeywordIndex(index)` → `''` for an empty index, else first line `KEYWORD INDEX (term → seqs; use recall):` then one line per term `<term>: 3, 7, 12`.
11. `rewriteCost(input)` with `input = {surfaceTokens, shadowedTokens, replacementTokens, window, cachedPrefixTokens = 0}` →
    `{pressureBefore, pressureAfter, saved, reprefillTokens, worthIt:boolean, tier:'none'|'watch'|'idle'|'compact'|'emergency'}`.
    - `saved = shadowedTokens - replacementTokens`; `pressureBefore = surfaceTokens/window`; `pressureAfter = (surfaceTokens - saved)/window` (both rounded to 4 decimals).
    - `reprefillTokens = max(0, surfaceTokens - saved - max(0, cachedPrefixTokens - shadowedTokens))` — what the provider must prefill again because the prompt prefix changed.
    - tier from pressureBefore: `<0.30 none`, `<0.50 watch`, `<0.70 idle`, `<0.90 compact`, else `emergency`.
    - `worthIt = saved > 0 && (tier === 'emergency' || tier === 'compact' || saved >= 0.10 * window)`.
    - non-finite or `window <= 0` → `{error:<string>}`.

## CLI

`node cg_recall.js --selftest` runs >= 14 named checks of your own, prints one `  ok   <name>` / `  FAIL <name>` line per check, then
EXACTLY one summary line `cg_recall selftest: <N> checks, <P> passed, <F> failed`, exit 0 iff F == 0. Importing the module must NOT run the selftest or print anything.
