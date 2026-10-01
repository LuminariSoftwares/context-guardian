/**
 * dsh-context-guardian/engine -- the compaction half of Context Guardian, for
 * the AGENT-PRESET plane of DSH.
 *
 * Why a second entry file: DSH mounts compaction inside the agent preset
 * (`compaction` group, isolated realm), not in the profile. The bundle's
 * index.js lives in the profile plane and cannot see `ctx.compaction`. This
 * file is mounted as ONE ROW inside the preset's `compaction` group, after
 * `compaction-basic`:
 *
 *     - id: context-guardian
 *       name: 'file:///C:/path/to/context-guardian/engine.js'   # file URL, or ./relative to the preset
 *       config: { mode: llm-then-deterministic }
 *
 * What it does (DSH 0.1.2-alpha.2, source read 2026-09-20):
 *   1. Replaces `summarize()` -- "the sole subclass customization hook" of
 *      BasicCompactionEngine -- on the live service instance. compaction-basic
 *      still owns selection, stability checks, the durable transaction and the
 *      shrink guarantee. Only the text of the checkpoint changes.
 *        mode llm-then-deterministic (default): the stock LLM summary runs
 *          first; when it throws, returns nothing, or cannot fit in the window
 *          at all, the region is compiled deterministically instead. A
 *          conversation can no longer be bricked by a failed summary.
 *        mode deterministic: never call the model. mode off: install nothing.
 *   2. Deterministic checkpoint = vendored compaction-instant compiler
 *      (seq pointers on every line) + files written + a folded keyword index.
 *   3. `recall` / `search` tools and a `/recall` command restore the original
 *      text behind any pointer from the append-only session log.
 *   4. Archives every compacted span in context_guardian.py's span format, and
 *      snapshots session state on `compaction/start` (what dsh-openwolf did).
 *   5. Idle pressure trigger: when the agent goes idle above `idleCompactRatio`
 *      it compacts then, while nobody is waiting on the model.
 *
 * ZERO DSH imports on purpose: a linked checkout outside `$DSH_HOME/profiles`
 * cannot resolve in-box `@deepseek-ai/*` packages. Everything DSH-side arrives
 * through `ctx`.
 *
 * MIT licensed.
 */
import { appendFileSync, closeSync, mkdirSync, openSync, readdirSync, statSync, unlinkSync, writeFileSync, writeSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { COMPILER_REV, DEFAULT_NOISE_PATTERNS, RECALL_GUIDE, compileNoisePatterns, compileRegion, isCheckpointSource, joinCompiledEntries, parseToolArguments } from './vendor/compiler.js'
import { MEMORY_REV, emptyMemory, extractMemory, itemsForSession, loadMemory, memoryStats, mergeMemory, normalizeText, renderMemory, saveMemory } from './cg_memory.js'
import * as lib from './cg_recall.js'
import { ANCHORS_REV, findLostAnchors, lostConstraints, renderCarried } from './cg_anchors.js'

export const name = 'context-guardian-engine'
export const inject = ['compaction']
export const ENGINE_REV = 'cg-engine-4'

// Pinned goal markers. The session's original request is pinned into every
// checkpoint byte-for-byte so a local model never drifts from the task after
// one (or two) compactions.
export const GOAL_OPEN = '<<<GOAL'
export const GOAL_CLOSE = 'GOAL>>>'
export const GOAL_UPDATE_RE = /^\s*goal\s*:/i

const PACKAGE_ROOT = dirname(fileURLToPath(import.meta.url))
const MODES = ['llm-then-deterministic', 'deterministic', 'off']
const ANCHOR_MODES = ['repair', 'report', 'off']
const ALL_TOOLS = ['recall', 'search', 'context_rewrite_cost', 'context_compact', 'guardian_status']

export const DEFAULTS = Object.freeze({
  mode: 'llm-then-deterministic',
  numCtx: 32768,
  reserveOutput: 8192,
  idleCompactRatio: 0.45,
  idleDelayMs: 4000,
  checkpointMaxTokens: 3000,
  textTokens: 200,
  userTextTokens: 400,
  toolCallTokens: 32,
  toolResultExcerptTokens: 64,
  maxRecallTokens: 16000,
  maxSearchHits: 50,
  keywordTerms: 25,
  filesListed: 15,
  // Durable memory: 0 turns it completely OFF (nothing read, written or rendered).
  memoryMaxTokens: 1200,
  memoryPath: '',
  // 0 = unlimited model-facing recalls per turn (the /recall command is never limited).
  recallMaxPerTurn: 4,
  keepSpans: 500,
  spanDir: '',
  logPath: '',
  tools: ['recall', 'search'],
  toolArgTools: [],
  hideTools: [],
  // Harness-injected context the harness re-injects by itself: worthless inside a checkpoint.
  dropSources: ['agent-instructions', 'skill-catalog', '@deepseek-ai/dsh-system-prompt', 'repeat-tool-reminder'],
  // Append-only checkpoints: keep the leading run of checkpoint nodes and compact only
  // what follows, so the prompt prefix never changes and the provider's KV/prefix cache
  // survives. Opt-in until it has a live proof; 0 chainMaxTokens = auto per session.
  appendOnly: false,
  chainMaxTokens: 0,
  chainMaxCheckpoints: 4,
  anchorCheck: 'repair',
})

/**
 * Fold the environment over the preset row's config.
 * Precedence: env GUARDIAN_* > row config > DEFAULTS. A malformed value is
 * ignored, never half-applied.
 */
export function resolveEngineOptions(config = {}, env = process.env) {
  const row = config ?? {}
  const pick = (key) => (row[key] === undefined || row[key] === null ? DEFAULTS[key] : row[key])
  const num = (envKey, key, min, max) => {
    const raw = env[envKey]
    if (raw !== undefined && raw !== '') {
      const parsed = Number(raw)
      if (Number.isFinite(parsed) && parsed >= min && parsed <= max) return parsed
    }
    const value = Number(pick(key))
    return Number.isFinite(value) && value >= min && value <= max ? value : DEFAULTS[key]
  }
  const list = (envKey, key) => {
    const raw = env[envKey]
    if (raw !== undefined && raw !== '') return raw.split(',').map(s => s.trim()).filter(Boolean)
    const value = pick(key)
    return Array.isArray(value) ? value.map(String) : DEFAULTS[key]
  }
  const envMode = String(env.GUARDIAN_DSH_MODE ?? '').trim().toLowerCase()
  const rowMode = String(pick('mode')).trim().toLowerCase()
  const mode = MODES.includes(envMode) ? envMode : MODES.includes(rowMode) ? rowMode : DEFAULTS.mode
  // A boolean switch, not a number: only the four spellings turn it, everything
  // else falls through to the row, which counts as on when it is exactly `true`.
  const envAppendOnly = String(env.GUARDIAN_APPEND_ONLY ?? '').trim().toLowerCase()
  const appendOnly = envAppendOnly === '1' || envAppendOnly === 'true' ? true
    : envAppendOnly === '0' || envAppendOnly === 'false' ? false
      : pick('appendOnly') === true
  const numCtx = Math.floor(num('GUARDIAN_NUM_CTX', 'numCtx', 1024, 4_000_000))
  // Did the user pin the window themselves? An explicit numCtx (env or preset row)
  // must always win over what the host reports, so the window helper needs to know.
  const numCtxExplicit = (env.GUARDIAN_NUM_CTX !== undefined && env.GUARDIAN_NUM_CTX !== '')
    || (row.numCtx !== undefined && row.numCtx !== null)
  // Resolved once: the memory file lives next to the spans unless told otherwise.
  const spanDir = env.GUARDIAN_SPAN_DIR || String(pick('spanDir') || '') || join(PACKAGE_ROOT, 'logs', 'guardian_spans')
  return {
    mode,
    numCtx,
    numCtxExplicit,
    reserveOutput: Math.floor(num('GUARDIAN_RESERVE_OUTPUT', 'reserveOutput', 0, 1_000_000)),
    idleCompactRatio: num('GUARDIAN_IDLE_COMPACT_RATIO', 'idleCompactRatio', 0, 0.99),
    idleDelayMs: Math.floor(num('GUARDIAN_IDLE_DELAY_MS', 'idleDelayMs', 0, 3_600_000)),
    checkpointMaxTokens: Math.floor(num('GUARDIAN_CHECKPOINT_MAX_TOKENS', 'checkpointMaxTokens', 200, 1_000_000)),
    textTokens: Math.floor(num('GUARDIAN_TEXT_TOKENS', 'textTokens', 8, 100_000)),
    userTextTokens: Math.floor(num('GUARDIAN_USER_TEXT_TOKENS', 'userTextTokens', 8, 100_000)),
    toolCallTokens: Math.floor(num('GUARDIAN_TOOL_CALL_TOKENS', 'toolCallTokens', 8, 100_000)),
    toolResultExcerptTokens: Math.floor(num('GUARDIAN_TOOL_RESULT_TOKENS', 'toolResultExcerptTokens', 8, 100_000)),
    // One recall may never take more than a quarter of the window it lands in.
    // One recall may never take more than a quarter of the window it lands in. When the user pinned the window,
    // cap here; otherwise the quarter cap is applied per session at use (doRecall) against the model's real window --
    // capping by the default numCtx would pin every big-model user at 8192 (O5 2026-09-25).
    maxRecallTokens: numCtxExplicit
      ? Math.min(Math.floor(num('GUARDIAN_MAX_RECALL_TOKENS', 'maxRecallTokens', 100, 1_000_000)), Math.floor(numCtx / 4))
      : Math.floor(num('GUARDIAN_MAX_RECALL_TOKENS', 'maxRecallTokens', 100, 1_000_000)),
    maxSearchHits: Math.floor(num('GUARDIAN_MAX_SEARCH_HITS', 'maxSearchHits', 1, 1000)),
    keywordTerms: Math.floor(num('GUARDIAN_KEYWORD_TERMS', 'keywordTerms', 0, 500)),
    filesListed: Math.floor(num('GUARDIAN_FILES_LISTED', 'filesListed', 0, 200)),
    memoryMaxTokens: Math.floor(num('GUARDIAN_MEMORY_MAX_TOKENS', 'memoryMaxTokens', 0, 100_000)),
    memoryPath: env.GUARDIAN_MEMORY_PATH || String(pick('memoryPath') || '') || join(spanDir, 'memory.json'),
    recallMaxPerTurn: Math.floor(num('GUARDIAN_RECALL_MAX_PER_TURN', 'recallMaxPerTurn', 0, 1000)),
    keepSpans: Math.floor(num('GUARDIAN_KEEP_SPANS', 'keepSpans', 0, 1_000_000)),
    spanDir,
    logPath: env.GUARDIAN_DSH_LOG || String(pick('logPath') || '') || join(PACKAGE_ROOT, 'logs', 'guardian_dsh.jsonl'),
    tools: list('GUARDIAN_DSH_TOOLS', 'tools').filter(t => ALL_TOOLS.includes(t)),
    toolArgTools: list('GUARDIAN_TOOL_ARG_TOOLS', 'toolArgTools'),
    hideTools: list('GUARDIAN_HIDE_TOOLS', 'hideTools'),
    dropSources: list('GUARDIAN_DROP_SOURCES', 'dropSources'),
    appendOnly,
    chainMaxTokens: Math.floor(num('GUARDIAN_CHAIN_MAX_TOKENS', 'chainMaxTokens', 0, 1_000_000)),
    chainMaxCheckpoints: Math.floor(num('GUARDIAN_CHAIN_MAX_CHECKPOINTS', 'chainMaxCheckpoints', 1, 1000)),
    anchorCheck: (() => {
      const e = String(env.GUARDIAN_ANCHOR_CHECK ?? '').trim().toLowerCase()
      if (ANCHOR_MODES.includes(e)) return e
      const r = String(pick('anchorCheck') ?? '').trim().toLowerCase()
      return ANCHOR_MODES.includes(r) ? r : 'repair'
    })(),
  }
}

/**
 * The window pressure and caps must divide by, for ONE session: an explicit
 * user/preset numCtx always wins; otherwise a positive host-reported window
 * (the model's real `request/context` contextWindow); otherwise the default.
 * Pure. `hostWindow` is only honoured when it is a positive integer, so a
 * malformed 0 / -5 / 1.5 / "200000" falls back to `options.numCtx`.
 */
export function effectiveWindow(options, hostWindow, explicit) {
  const configured = Number(options?.numCtx)
  const fallback = Number.isFinite(configured) && configured > 0 ? configured : DEFAULTS.numCtx
  if (explicit) return fallback
  if (Number.isInteger(hostWindow) && hostWindow > 0) return hostWindow
  return fallback
}

/** Conservative token estimate for "will this request fit": chars / 3.5. */
export function estRequestTokens(value) {
  if (value === undefined || value === null) return 0
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return Math.ceil(text.length / 3.5)
}

/** Pressure tier of the 30/50/70/90 ladder. */
export function tierOf(pressure) {
  if (!(pressure >= 0.30)) return 'none'
  if (pressure < 0.50) return 'watch'
  if (pressure < 0.70) return 'idle'
  if (pressure < 0.90) return 'compact'
  return 'emergency'
}

/** How much checkpoint each tier is allowed: the fuller the window, the tighter the checkpoint. */
const TIER_FACTOR = Object.freeze({ none: 1.25, watch: 1.25, idle: 1.25, compact: 1, emergency: 0.6 })

/**
 * Every event of a session as `{seq, message}`; non-message events carry
 * `message: null`. Cached per session by log length (the log is append-only).
 */
const nodeCache = new WeakMap()
export function sessionNodes(session) {
  const events = session?.events
  if (!Array.isArray(events) && typeof events?.length !== 'number') return []
  const cached = nodeCache.get(session)
  const nodes = cached?.nodes ?? []
  for (let index = nodes.length; index < events.length; index += 1) {
    const event = events[index]
    let message = null
    try { message = session.deriveEventMessage(event) ?? null } catch { message = null }
    nodes.push({ seq: event?.seq ?? index, message })
  }
  nodeCache.set(session, { nodes })
  return nodes
}

/**
 * Give each summarization-input message its REAL log seq. The engine builds
 * `input.messages` as `shadowedSeqs.map(derive)`, in surface order, so a
 * forward walk over the surface with structural equality finds them. A
 * message that cannot be matched keeps a synthetic negative seq and is
 * reported, never silently mislabelled with someone else's pointer.
 */
export function mapRegionSeqs(session, messages) {
  const surface = Array.from(session?.surface?.nodes ?? [])
  const events = session?.events ?? []
  const derived = new Map()
  const key = (seq) => {
    if (!derived.has(seq)) {
      let text = null
      try { text = JSON.stringify(session.deriveEventMessage(events[seq]) ?? null) } catch { text = null }
      derived.set(seq, text)
    }
    return derived.get(seq)
  }
  const nodes = []
  let cursor = 0
  let unmatched = 0
  for (const message of messages) {
    const want = JSON.stringify(message)
    let found = -1
    for (let index = cursor; index < surface.length; index += 1) {
      if (key(surface[index]) === want) { found = index; break }
    }
    if (found === -1) {
      unmatched += 1
      nodes.push({ seq: -(nodes.length + 1), message })
    } else {
      nodes.push({ seq: surface[found], message })
      cursor = found + 1
    }
  }
  return { nodes, unmatched }
}

/** Files the region's tool calls wrote or edited, oldest first, distinct, last `limit`. */
export function filesWritten(nodes, limit) {
  if (!(limit > 0)) return []
  const seen = new Map()
  for (const node of nodes) {
    const content = node.message?.role === 'assistant' ? node.message.content : undefined
    if (!Array.isArray(content)) continue
    for (const block of content) {
      if (block?.type !== 'tool-call' || !/write|edit|patch|create|replace/i.test(String(block.name))) continue
      const args = parseToolArguments(block.arguments)
      const path = args?.path ?? args?.file_path ?? args?.filePath ?? args?.target
      if (typeof path !== 'string' || path.length === 0) continue
      seen.delete(path)
      seen.set(path, node.seq)
    }
  }
  return [...seen].slice(-limit).map(([path, seq]) => ({ path, seq }))
}

/**
 * Split harness-injected context out of a region. A user-role message whose
 * `source.kind` (or `source.plugin`) is listed in `dropSources` is context the
 * harness injects again by itself (workspace instructions, the skill
 * catalogue, runtime snapshots) -- 17 KB of it per injection in the 2026-09-17
 * session. Checkpoints (`plugin: compact`) and tool results are never dropped.
 */
export function splitInjected(nodes, dropSources) {
  const drop = new Set(dropSources ?? [])
  const kept = []
  const dropped = []
  for (const node of nodes) {
    const source = node.message?.source
    const injected = node.message?.role === 'user' && source !== undefined && source !== null && !isCheckpointSource(source)
      && (drop.has(source.kind) || (source.kind === 'plugin' && drop.has(source.plugin)))
    if (injected) dropped.push(node)
    else kept.push(node)
  }
  return { kept, dropped }
}

/** Identifier-like: a path, a dotted/underscored/hyphenated name, or anything with a digit -- not an English word. */
const IDENTIFIER_RE = /[_/\\.\-\d]/

// ── pinned goal ─────────────────────────────────────────────────────────────
// A `<<<GOAL ... >>>`-delimited block survives compaction inside the checkpoint
// text; extractGoal reads it back out (from earlier checkpoints and from the
// live user messages) and renderGoal writes it verbatim onto a fresh one.

const GOAL_BLOCK_RE = new RegExp(`${GOAL_OPEN}\\n([\\s\\S]*?)\\n${GOAL_CLOSE}`)
const GOAL_UPDATE_BLOCK_RE = new RegExp(`${GOAL_OPEN} update seq (\\d+)\\n([\\s\\S]*?)\\n${GOAL_CLOSE}`, 'g')

/** Every text block of a message joined with "\n". */
function messageText(message) {
  const blocks = Array.isArray(message?.content) ? message.content : []
  return blocks.filter(block => block?.type === 'text').map(block => String(block.text ?? '')).join('\n')
}

/** A real user turn: role user, not a checkpoint, no injected source, first block text. */
function isUserTextNode(message) {
  if (message?.role !== 'user') return false
  if (isCheckpointSource(message.source)) return false
  const source = message.source
  if (source !== undefined && source !== null) return false
  return message.content?.[0]?.type === 'text'
}

/**
 * The session's pinned goal and its `goal:` updates, recovered from `nodes`
 * (walked in the given order). A goal carried by any checkpoint node wins; the
 * first user-text node is the fallback goal. Updates are de-duplicated by seq
 * (first wins) and returned sorted by seq ascending.
 */
export function extractGoal(nodes) {
  let goal = null
  let goalNode = null
  let firstUserText = null
  const collected = []
  for (const node of nodes ?? []) {
    const message = node?.message
    if (message === undefined || message === null) continue
    if (isCheckpointSource(message.source)) {
      const text = messageText(message)
      if (goal === null) {
        const match = text.match(GOAL_BLOCK_RE)
        if (match !== null) goal = match[1]
      }
      for (const match of text.matchAll(GOAL_UPDATE_BLOCK_RE)) {
        collected.push({ seq: Number(match[1]), text: match[2], node: null })
      }
    } else if (isUserTextNode(message)) {
      const text = messageText(message)
      if (firstUserText === null) firstUserText = { node, text }
      if (GOAL_UPDATE_RE.test(text)) collected.push({ seq: node.seq, text, node })
    }
  }
  if (goal === null && firstUserText !== null) {
    goal = firstUserText.text
    goalNode = firstUserText.node
  }
  const bySeq = new Map()
  for (const update of collected) {
    // Skip the goal node's own text; checkpoint updates carry node=null, so
    // only a real goalNode may suppress an update.
    if ((goalNode !== null && update.node === goalNode) || bySeq.has(update.seq)) continue
    bySeq.set(update.seq, { seq: update.seq, text: update.text })
  }
  return { goal, updates: [...bySeq.values()].sort((a, b) => a.seq - b.seq) }
}

/**
 * Where the session's pinned goal came from: a checkpoint carried it, or it is
 * the first user-text node. Same walk as extractGoal; `seq` is null when a
 * checkpoint owns the goal or when there is no goal at all.
 */
export function goalOrigin(nodes) {
  let fromCheckpoint = false
  let seq = null
  for (const node of nodes ?? []) {
    const message = node?.message
    if (message === undefined || message === null) continue
    if (isCheckpointSource(message.source)) {
      if (!fromCheckpoint && GOAL_BLOCK_RE.test(messageText(message))) fromCheckpoint = true
      continue
    }
    if (seq === null && isUserTextNode(message)) seq = node.seq
  }
  if (fromCheckpoint) seq = null
  return { seq, fromCheckpoint, updates: extractGoal(nodes).updates.length }
}

/** Render a pinned-goal block for a checkpoint head. '' when there is no goal. */
export function renderGoal(g) {
  if (g?.goal === null || g?.goal === undefined) return ''
  let out = "[pinned goal -- the session's first request, verbatim; later 'goal:' updates follow. Keep working toward it.]"
  out += `\n${GOAL_OPEN}\n${g.goal}\n${GOAL_CLOSE}`
  for (const update of g.updates ?? []) {
    out += `\n${GOAL_OPEN} update seq ${update.seq}\n${update.text}\n${GOAL_CLOSE}`
  }
  return out
}

// ── append-only checkpoint chains ──────────────────────────────────────────
// DSH's compaction-basic always compacts a range that starts at the FIRST surface
// node, so every compaction rewrites the whole prompt prefix, the earlier
// checkpoints included, and the provider's KV/prefix cache dies each time. There is
// no strategy hook, but the automatic paths reach the PUBLIC `compactRegion` by
// dynamic dispatch, so overriding it on the service instance is enough: keep the
// leading run of checkpoint nodes (the "chain head") and compact only what follows.
// The new checkpoint is then APPENDED after the head instead of replacing it.

/**
 * Where an append-only compaction should start, or why the whole chain must roll
 * up instead. Pure and never throws: an exception is reported as
 * `reason: 'error: <msg>'` with the given start kept, because a bad plan must
 * never be a failed compaction.
 *
 * `headSeqs` is the leading run of checkpoint surface seqs (the chain head) and
 * `chainTokens` what they cost, so the caller can record the decision. Both are
 * also carried by every "keep start" return, empty / 0 when not computed.
 */
export function planChainStart(session, start, end, opts = {}) {
  const keep = (reason, headSeqs = [], chainTokens = 0) => ({ start, reason, headSeqs, chainTokens })
  try {
    const surface = Array.from(session?.surface?.nodes ?? [])
    const si = surface.indexOf(start)
    const ei = surface.indexOf(end)
    // Only a range anchored at the very first surface node can leave a head behind.
    if (si !== 0 || ei < 0) return keep('not head-anchored')
    const headSeqs = []
    const headMessages = []
    let k = 0
    for (; k <= ei; k += 1) {
      const seq = surface[k]
      const message = session.deriveEventMessage(session.events[seq]) ?? null
      if (!isCheckpointSource(message?.source)) break
      headSeqs.push(seq)
      headMessages.push(message)
    }
    if (headSeqs.length === 0) return keep('no checkpoint head')
    // The whole range is checkpoints: there is nothing to append after it.
    if (k > ei) return keep('nothing after the head', headSeqs)
    const chainTokens = headMessages.reduce((total, message) => total + lib.estTokens(lib.renderMessage(message)), 0)
    // Past either budget the head is not a prefix worth preserving: roll it up.
    if (chainTokens > opts.chainMaxTokens) return keep('roll-up: chain over budget', headSeqs, chainTokens)
    if (headSeqs.length >= opts.chainMaxCheckpoints) return keep('roll-up: chain at max checkpoints', headSeqs, chainTokens)
    if (opts.pressure >= 0.9) return keep('roll-up: emergency pressure', headSeqs, chainTokens)
    return { start: surface[k], reason: 'append', headSeqs, chainTokens }
  } catch (error) {
    return keep(`error: ${String(error?.message ?? error)}`)
  }
}

/**
 * The goal block for a checkpoint APPENDED after a chain head. The head already
 * carries the goal verbatim, so repeating it would grow the prefix for nothing:
 * the appended checkpoint only names what changed. Without a head-carried goal
 * there is nothing to point at, and the full block is rendered as usual.
 */
export function renderGoalDelta(headNodes, allGoalNodes) {
  const head = extractGoal(headNodes)
  const full = extractGoal(allGoalNodes)
  if (head.goal === null || head.goal === undefined) return renderGoal(full)
  const seen = new Set((head.updates ?? []).map(update => update.seq))
  const fresh = (full.updates ?? []).filter(update => !seen.has(update.seq))
  if (fresh.length === 0) return ''
  let out = "[pinned goal: unchanged, see the first checkpoint above; new 'goal:' updates follow]"
  for (const update of fresh) out += `\n${GOAL_OPEN} update seq ${update.seq}\n${update.text}\n${GOAL_CLOSE}`
  return out
}

/** The deterministic checkpoint body for one region. Pure. */
export function buildCheckpoint(allRegionNodes, options, pressure, allNodes, extras = {}) {
  const { kept: nodes, dropped } = splitInjected(allRegionNodes, options.dropSources)
  const regionTokens = allRegionNodes.reduce((total, node) => total + lib.estTokens(lib.renderMessage(node.message)), 0)
  const tier = tierOf(pressure)
  const cap = Math.max(200, Math.min(Math.floor(options.checkpointMaxTokens * TIER_FACTOR[tier]), Math.floor(regionTokens * 0.5)))
  const checkpointOrdinals = new Map()
  let ordinal = 0
  for (const node of allNodes ?? allRegionNodes) {
    if (node.message !== null && isCheckpointSource(node.message?.source)) checkpointOrdinals.set(node.seq, ordinal += 1)
  }
  const compiled = compileRegion(nodes, {
    textTokens: options.textTokens,
    userTextTokens: options.userTextTokens,
    toolCallTokens: options.toolCallTokens,
    toolResultExcerptTokens: options.toolResultExcerptTokens,
    maxTokens: cap,
    toolArgTools: options.toolArgTools,
    hideTools: options.hideTools,
    noisePatterns: compileNoisePatterns(DEFAULT_NOISE_PATTERNS),
    checkpointOrdinals,
  })
  const real = allRegionNodes.filter(node => node.seq >= 0).map(node => node.seq)
  const range = real.length === 0 ? 'unmapped' : `${Math.min(...real)}-${Math.max(...real)}`
  // The goal is computed from the region PLUS the whole session (region first),
  // so a goal carried by a checkpoint inside the region wins over a later user
  // message. It is never counted against or cut by any cap. An append-only chain
  // supplies its own block instead (renderGoalDelta), '' meaning no goal block.
  const goalNodes = allNodes === undefined || allNodes === null ? allRegionNodes : [...allRegionNodes, ...allNodes]
  const goalBlock = typeof extras?.goalBlock === 'string' ? extras.goalBlock : renderGoal(extractGoal(goalNodes))
  // The durable memory block sits right under the goal, like it: never capped.
  const memoryBlock = String(extras?.memoryBlock ?? '')
  const parts = [
    `[context-guardian checkpoint · deterministic · ${allRegionNodes.length} nodes · seqs ${range} · ~${regionTokens} -> ~${compiled.stats.tokens} tokens · ${COMPILER_REV}]`,
  ]
  if (goalBlock.length > 0) parts.push(goalBlock)
  if (memoryBlock.length > 0) parts.push(memoryBlock)
  parts.push(RECALL_GUIDE)
  if (dropped.length > 0) parts.push(`[${dropped.length} harness-injected context messages omitted (instructions, skill list, runtime notes -- the harness re-injects them): seqs ${dropped.map(node => node.seq).join(', ')}]`)
  parts.push(...compiled.entries)
  // Durable files: whatever an EARLIER checkpoint inside this region already
  // named, then what this region wrote. Without the first group a file edited
  // before the previous compaction is lost at every later one.
  const durable = new Map()
  for (const item of extractMemory(nodes.filter(node => isCheckpointSource(node.message?.source)), { session: '' })) {
    if (item.cat !== 'files' || typeof item.text !== 'string' || item.text === '') continue
    durable.delete(item.text)
    durable.set(item.text, { path: item.text, seq: item.seq })
  }
  for (const file of filesWritten(nodes, Infinity)) {
    if (durable.has(file.path)) continue
    durable.set(file.path, file)
  }
  const files = options.filesListed > 0 ? [...durable.values()].slice(-options.filesListed) : []
  if (files.length > 0) parts.push(`FILES WRITTEN (latest last): ${files.map(file => `${file.path} (seq ${file.seq})`).join(', ')}`)
  if (options.keywordTerms > 0) {
    // Only identifiers somebody SAID (user or assistant text): a directory listing in a
    // tool result otherwise fills the index with paths nobody ever talked about.
    const spoken = new Set()
    for (const node of nodes) {
      if (node.message?.content?.[0]?.type === 'tool-result') continue
      for (const block of node.message?.content ?? []) {
        if (block?.type !== 'text') continue
        for (const token of String(block.text ?? '').match(/[A-Za-z_][\w.\-\/\\]{3,}/g) ?? []) spoken.add(token.toLowerCase().replace(/[.,]+$/, ''))
      }
    }
    const terms = lib.foldKeywordIndex(nodes, { maxTerms: options.keywordTerms * 8 })
      .filter(row => IDENTIFIER_RE.test(row.term) && spoken.has(row.term)).slice(0, options.keywordTerms)
    const index = lib.renderKeywordIndex(terms)
    if (index.length > 0) parts.push(index)
  }
  // `kind` is the dropSources key a user would have to remove: a plugin source
  // is named by its plugin id, everything else by its own kind.
  const droppedNodes = dropped.map(node => {
    const source = node.message?.source
    return { seq: node.seq, kind: source?.kind === 'plugin' ? source.plugin : source?.kind }
  })
  return { text: joinCompiledEntries(parts), stats: compiled.stats, capped: compiled.capped, tier, regionTokens, cap, dropped: dropped.length, droppedNodes }
}

/** True when the summary blocks carry at least one non-blank text block. */
function hasText(result) {
  return Array.isArray(result?.summary) && result.summary.some(block => block?.type === 'text' && String(block.text ?? '').trim().length > 0)
}

const TEXT_OUTPUT = { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: String(value) }] }

export function apply(ctx, config) {
  const options = resolveEngineOptions(config)
  const log = ctx.logger ?? console
  const runId = `dsh-${new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)}-${process.pid}`
  const pressureBySession = new WeakMap()
  const triggerBySession = new WeakMap()
  // The plan of the append-only compaction in flight, per session: the summarize
  // hook reads it to know that this checkpoint is being APPENDED after a chain
  // head rather than replacing it.
  const chainBySession = new WeakMap()
  // The host reports the model's real context window on every `request/context`
  // event; keep the last one per session and honour it for pressure and caps.
  const windowBySession = new WeakMap()
  const windowLogged = new WeakSet()
  const sessionWindow = (session) => effectiveWindow(options, windowBySession.get(session), options.numCtxExplicit)
  // Durable memory for this run: one closure document, re-read from disk at
  // every compaction so another process's writes are never clobbered.
  let memory = emptyMemory()
  // Per-session: what the last compaction was, per-agent: recalls this turn,
  // per-session: drop-source kinds already warned about.
  const lastCompaction = new WeakMap()
  const recallCount = new WeakMap()
  const droppedWarned = new WeakMap()
  const keyable = (value) => value !== null && value !== undefined && typeof value === 'object'

  const record = (entry) => {
    try {
      mkdirSync(dirname(options.logPath), { recursive: true })
      appendFileSync(options.logPath, `${JSON.stringify({ at: new Date().toISOString(), run: runId, ...entry })}\n`)
    } catch { /* logging must never break compaction */ }
  }

  if (options.mode === 'off') {
    log.info('context-guardian engine: mode off -- summarize hook NOT installed; compaction-basic is unchanged')
    return
  }

  // ── durable memory ───────────────────────────────────────────────────────
  if (options.memoryMaxTokens > 0) {
    const loaded = loadMemory(options.memoryPath)
    memory = loaded.memory
    record({ event: 'memory-loaded', path: options.memoryPath, status: loaded.status, items: loaded.memory.items.length })
    if (loaded.status === 'corrupt') {
      log.warn(`context-guardian engine: memory at ${options.memoryPath} is unreadable (${loaded.error ?? 'corrupt'}); starting from an empty memory, and the bad file is moved aside when the filesystem allows it`)
    }
  }

  const memoryBlockText = () => renderMemory(memory, { maxTokens: options.memoryMaxTokens })

  /** Merge key of a memory item, the same key cg_memory.js merges on. */
  const memoryKey = (item) => item.cat + '\u0000' + (item.cat === 'files' ? item.text : normalizeText(item.text))

  /** The session nodes of the chain head an appended checkpoint keeps in front of it. */
  const chainHeadNodes = (session, chain) => {
    if (chain === undefined) return []
    const wanted = new Set(chain.headSeqs ?? [])
    return sessionNodes(session).filter(node => wanted.has(node.seq))
  }

  /**
   * The memory block of an APPENDED checkpoint: only the notes this compaction
   * actually touched. The chain head in front of it already carries the rest, so
   * re-rendering the whole memory would grow the prompt prefix for nothing.
   */
  const deltaMemoryText = (touched) => renderMemory(
    { ...memory, items: (memory.items ?? []).filter(item => touched?.has?.(memoryKey(item))) },
    { maxTokens: options.memoryMaxTokens },
  )

  /**
   * Extract the region's durable facts, merge them over the file on disk and
   * write it back. Runs BEFORE any text is built, so the checkpoint renders
   * what it just learned. Never throws: memory must not break compaction.
   * Returns the merge keys this compaction touched, for the delta memory block.
   */
  const updateMemory = (session, regionNodes) => {
    if (options.memoryMaxTokens <= 0) return new Set()
    try {
      const items = extractMemory(regionNodes ?? [], { session: String(session?.id ?? '') })
      const reloaded = loadMemory(options.memoryPath)
      const base = reloaded.status === 'loaded' ? reloaded.memory : memory
      memory = mergeMemory(base, items)
      const saved = saveMemory(options.memoryPath, memory)
      record({ event: 'memory', session: String(session?.id ?? ''), extracted: items.length, total: memory.items.length, saved })
      if (!saved) log.warn(`context-guardian engine: memory NOT written to ${options.memoryPath}; compaction proceeds`)
      return new Set(items.map(item => memoryKey(item)))
    } catch (error) {
      log.warn(`context-guardian engine: memory not updated (${error.message}); compaction proceeds`)
      return new Set()
    }
  }

  const rememberCompaction = (session, entry) => {
    if (keyable(session)) lastCompaction.set(session, entry)
  }
  const compactionLine = (session) => {
    const last = keyable(session) ? lastCompaction.get(session) : undefined
    return last === undefined ? 'none yet in this session' : `${last.kind} (${last.detail}) at ${last.at}, trigger ${last.trigger}`
  }

  // ── span archive (context_guardian.py `write_span` format) ───────────────
  let spanIndex = 0
  const writeSpan = (session, messages, summary) => {
    try {
      const dir = join(options.spanDir, runId)
      mkdirSync(dir, { recursive: true })
      for (let attempt = 0; attempt < 50; attempt += 1) {
        const index = spanIndex + 1 + attempt
        const path = join(dir, `${String(index).padStart(4, '0')}.json`)
        let fd
        try { fd = openSync(path, 'wx') } catch (error) { if (error?.code === 'EEXIST') continue; throw error }
        try {
          writeSync(fd, JSON.stringify({
            run_id: runId, index, at: new Date().toISOString().replace('T', ' ').slice(0, 19), num_ctx: options.numCtx,
            message_count: messages.length, summary, messages, session_id: String(session?.id ?? ''), engine: ENGINE_REV,
          }, null, 1))
        } finally { closeSync(fd) }
        spanIndex = index
        pruneSpans()
        return path
      }
    } catch (error) {
      log.warn(`context-guardian engine: span NOT archived (${error.message}); compaction proceeds`)
    }
    return null
  }
  const pruneSpans = () => {
    try {
      const files = []
      for (const run of readdirSync(options.spanDir)) {
        const dir = join(options.spanDir, run)
        if (!statSync(dir).isDirectory()) continue
        for (const file of readdirSync(dir)) if (/^\d+\.json$/.test(file)) files.push(join(dir, file))
      }
      files.sort()
      for (const doomed of files.slice(0, Math.max(0, files.length - options.keepSpans))) unlinkSync(doomed)
    } catch { /* housekeeping only */ }
  }

  // ── the summarize hook ───────────────────────────────────────────────────
  const service = ctx.compaction
  const original = service.summarize
  if (typeof original !== 'function') {
    log.error('context-guardian engine: ctx.compaction has no summarize() -- this DSH is not the 0.1.2 compaction-basic engine; nothing installed')
    return
  }

  const anchorsBySession = new WeakMap()
  const anchorPass = (session, regionNodes, finalText) => {
    if (options.anchorCheck === 'off') return ''
    try {
      const real = (regionNodes ?? []).filter(node => node.seq >= 0).map(node => node.seq)
      const maxSeq = real.length > 0 ? Math.max(...real) : Infinity
      const all = session === undefined || session === null ? [] : sessionNodes(session)
      const later = all.filter(node => node.seq > maxSeq).map(node => lib.renderMessage(node.message))
      const goal = extractGoal(all).goal
      if (typeof goal === 'string') later.push(goal)
      const found = findLostAnchors({ regionTexts: (regionNodes ?? []).map(node => lib.renderMessage(node.message)), laterTexts: later, checkpointText: finalText, memoryTexts: [] })
      const items = lostConstraints({ regionItems: extractMemory(regionNodes ?? [], { session: '' }), checkpointText: finalText })
      const stats = { recurring: found.recurring.length, kept: found.kept.length, lost: found.lost.length, constraintsLost: items.length }
      if (keyable(session)) anchorsBySession.set(session, stats)
      record({ event: 'anchors', session: String(session?.id ?? ''), mode: options.anchorCheck, rev: ANCHORS_REV, ...stats })
      return options.anchorCheck === 'repair' ? renderCarried(found.lost, items) : ''
    } catch (error) {
      record({ event: 'anchors-error', session: String(session?.id ?? ''), error: String(error?.message ?? error).slice(0, 200) })
      return ''
    }
  }

  const deterministic = (input, agent, reason) => {
    const session = agent?.session
    const { nodes, unmatched } = mapRegionSeqs(session, input.messages)
    const touched = updateMemory(session, nodes)
    const chain = keyable(session) ? chainBySession.get(session) : undefined
    const allNodes = session === undefined ? nodes : sessionNodes(session)
    const extras = { memoryBlock: memoryBlockText() }
    if (chain !== undefined) {
      // Appended after a chain head: the head in front of this checkpoint already
      // carries the goal and the older memory, so this one carries only the delta.
      extras.goalBlock = renderGoalDelta(chainHeadNodes(session, chain), [...nodes, ...allNodes])
      extras.memoryBlock = deltaMemoryText(touched)
    }
    const pressure = pressureBySession.get(session) ?? measure(agent)?.pressure ?? 0.8
    const checkpoint = buildCheckpoint(nodes, options, pressure, allNodes, extras)
    const carried = anchorPass(session, nodes, checkpoint.text)
    if (carried.length > 0) checkpoint.text = checkpoint.text + '\n\n' + carried
    const span = writeSpan(session, input.messages, checkpoint.text)
    rememberCompaction(session, {
      kind: String(reason).startsWith('llm summary cannot fit') ? 'overflow' : 'deterministic',
      detail: reason, at: new Date().toISOString(), trigger: triggerBySession.get(session) ?? 'auto',
    })
    record({ event: 'deterministic', session: String(session?.id ?? ''), reason, nodes: nodes.length, unmatched, tier: checkpoint.tier, regionTokens: checkpoint.regionTokens, checkpointTokens: checkpoint.stats.tokens, capped: checkpoint.capped, span })
    reportDropped(session, checkpoint.droppedNodes)
    log.info(`context-guardian engine: deterministic checkpoint (${reason}) -- ${nodes.length} nodes, ~${checkpoint.regionTokens} -> ~${checkpoint.stats.tokens} tokens, tier ${checkpoint.tier}${unmatched > 0 ? `, ${unmatched} unmapped` : ''}`)
    return { summary: [{ type: 'text', text: checkpoint.text }], provider: 'context-guardian', model: COMPILER_REV, rawOutput: checkpoint.text }
  }

  /**
   * Dropped harness injections are always recorded, but warned about once per
   * session per source kind: a repeated reminder should not fill the log with
   * the same sentence, while a NEW kind must always speak up once.
   */
  const reportDropped = (session, droppedNodes) => {
    if (!Array.isArray(droppedNodes) || droppedNodes.length === 0) return
    record({ event: 'dropped-injected', session: String(session?.id ?? ''), seqs: droppedNodes.map(node => node.seq), kinds: [...new Set(droppedNodes.map(node => node.kind))] })
    const warned = keyable(session) ? (droppedWarned.get(session) ?? new Set()) : null
    if (warned === null) return
    for (const kind of new Set(droppedNodes.map(node => node.kind))) {
      if (warned.has(kind)) continue
      warned.add(kind)
      const seqs = droppedNodes.filter(node => node.kind === kind).map(node => node.seq)
      log.warn(`context-guardian engine: left ${seqs.length} user-role message(s) of source kind "${kind}" out of the checkpoint (seqs ${seqs.join(', ')}); they are still in the session log (recall them by seq). If that was real user text, remove "${kind}" from dropSources.`)
    }
    droppedWarned.set(session, warned)
  }

  const summarize = async function (input, agent, signal) {
    if (options.mode === 'deterministic') return deterministic(input, agent, 'mode deterministic')
    // The stock summarizer replays system + tools + the whole region and asks
    // for output on top. When that cannot fit in the window the call is
    // doomed before it is made -- a failure seen in real use.
    const need = estRequestTokens(input.system) + estRequestTokens(input.tools) + estRequestTokens(input.messages) + options.reserveOutput
    const window = sessionWindow(agent?.session)
    if (need > window) return deterministic(input, agent, `llm summary cannot fit: ~${need} > ${window}`)
    try {
      const result = await original.call(this, input, agent, signal)
      if (hasText(result)) {
        const session = agent?.session
        record({ event: 'llm', session: String(session?.id ?? ''), provider: result.provider, model: result.model })
        let region = null
        const chain = keyable(session) ? chainBySession.get(session) : undefined
        // Pin the goal on top of the stock summary too, as a NEW first text block.
        let goalBlock = ''
        try {
          region = mapRegionSeqs(session, input.messages).nodes
          const goalNodes = session === undefined || session === null ? region : [...region, ...sessionNodes(session)]
          goalBlock = chain === undefined ? renderGoal(extractGoal(goalNodes)) : renderGoalDelta(chainHeadNodes(session, chain), goalNodes)
        } catch { goalBlock = '' }
        const touched = updateMemory(session, region ?? [])
        rememberCompaction(session, {
          kind: 'llm', detail: `${result.provider}/${result.model}`, at: new Date().toISOString(), trigger: triggerBySession.get(session) ?? 'auto',
        })
        // Goal first, memory second, each its OWN text block, before the stock summary.
        const prepended = []
        if (goalBlock.length > 0) prepended.push({ type: 'text', text: goalBlock })
        const memoryBlock = chain === undefined ? memoryBlockText() : deltaMemoryText(touched)
        if (memoryBlock.length > 0) prepended.push({ type: 'text', text: memoryBlock })
        const stock = Array.isArray(result.summary) ? result.summary : []
        const finalText = [...prepended, ...stock].filter(b => b?.type === 'text').map(b => String(b.text ?? '')).join('\n')
        const carried = anchorPass(session, region ?? [], finalText)
        const tail = carried.length > 0 ? [{ type: 'text', text: carried }] : []
        if (prepended.length > 0 || tail.length > 0) {
          return { ...result, summary: [...prepended, ...stock, ...tail] }
        }
        return result
      }
      return deterministic(input, agent, 'llm summary was empty')
    } catch (error) {
      if (signal?.aborted) throw error
      return deterministic(input, agent, `llm summary failed: ${String(error?.message ?? error).slice(0, 200)}`)
    }
  }

  Object.defineProperty(service, 'summarize', { value: summarize, configurable: true, writable: true, enumerable: false })
  ctx.effect(() => () => { try { delete service.summarize } catch { /* realm already gone */ } })

  // ── append-only checkpoint chains ───────────────────────────────────────
  // compaction-basic reaches this PUBLIC method by dynamic dispatch on its two
  // automatic paths (pressure and context-overflow), so overriding it here is the
  // only lever there is. The manual /compact, the idle trigger and context_compact
  // go through compactNow instead, which keeps rolling the whole chain up: that
  // is where a chain is retired.
  if (options.appendOnly && typeof service.compactRegion === 'function') {
    const originalRegion = service.compactRegion
    const chained = async function (start, end, agent, signal) {
      const session = agent?.session
      // 0 means auto, resolved per session against that session's real window.
      const chainMaxTokens = options.chainMaxTokens > 0
        ? options.chainMaxTokens
        : Math.min(2 * options.checkpointMaxTokens, Math.floor(sessionWindow(session) * 0.15))
      const plan = planChainStart(session, start, end, {
        chainMaxTokens,
        chainMaxCheckpoints: options.chainMaxCheckpoints,
        pressure: measure(agent)?.pressure ?? pressureBySession.get(session) ?? 0,
      })
      if (plan.start === start) {
        if (plan.headSeqs.length > 0) {
          record({ event: 'chain', session: String(session?.id ?? ''), action: plan.reason.startsWith('roll-up') ? 'roll-up' : 'full', reason: plan.reason, head: plan.headSeqs.length })
        }
        return originalRegion.call(this, start, end, agent, signal)
      }
      // Published so the summarize hook can see that this checkpoint is being
      // appended after the head rather than replacing it.
      chainBySession.set(session, plan)
      try {
        const result = await originalRegion.call(this, plan.start, end, agent, signal)
        record({ event: 'chain', session: String(session?.id ?? ''), action: 'append', start: plan.start, originalStart: start, end, head: plan.headSeqs.length, chainTokens: plan.chainTokens })
        return result
      } catch (error) {
        // DSH validates the range before anything durable happens, so a start it
        // will not take costs nothing: retry the whole range and record why.
        const message = String(error?.message ?? error)
        if (message.startsWith('compactRegion: start seq')) {
          record({ event: 'chain', session: String(session?.id ?? ''), action: 'fallback', reason: message.slice(0, 200) })
          chainBySession.delete(session)
          return await originalRegion.call(this, start, end, agent, signal)
        }
        throw error
      } finally {
        chainBySession.delete(session)
      }
    }
    Object.defineProperty(service, 'compactRegion', { value: chained, configurable: true, writable: true, enumerable: false })
    ctx.effect(() => () => { try { delete service.compactRegion } catch { /* realm already gone */ } })
    log.info(`context-guardian engine: append-only chains enabled (max ${options.chainMaxCheckpoints} checkpoints, ${options.chainMaxTokens > 0 ? options.chainMaxTokens : 'auto'} tokens)`)
  }

  record({ event: 'installed', engine: ENGINE_REV, mode: options.mode, numCtx: options.numCtx, idleCompactRatio: options.idleCompactRatio, tools: options.tools, memory: options.memoryMaxTokens > 0 ? options.memoryPath : 'off', recallMaxPerTurn: options.recallMaxPerTurn, appendOnly: options.appendOnly })
  log.info(`context-guardian engine: summarize hook installed (mode ${options.mode}, window ${options.numCtx}, idle ${options.idleCompactRatio}, ${COMPILER_REV}, ${lib.RECALL_REV})`)

  // ── pressure, snapshot, outcome log ──────────────────────────────────────
  // cordis hides every service a plugin did not declare: `ctx.tokenMeter` reads
  // undefined here even though the host provides it (seen live 2026-09-20).
  // It is optional, so it arrives through ctx.inject rather than `inject`.
  let tokenMeter
  ctx.inject(['tokenMeter'], (inner) => {
    tokenMeter = inner.tokenMeter
    inner.effect(() => () => { tokenMeter = undefined })
  })
  function measure(agent) {
    try {
      const measurement = tokenMeter?.measure?.(agent.session)
      if (measurement === undefined || measurement === null) return undefined
      const tokens = Number(measurement.totalTokens ?? measurement.surfaceTokens ?? 0)
      const pressure = tokens / sessionWindow(agent.session)
      pressureBySession.set(agent.session, pressure)
      return { tokens, surfaceTokens: Number(measurement.surfaceTokens ?? tokens), pressure, usage: measurement.baseline?.kind === 'usage' ? measurement.baseline.usage : undefined, nodes: measurement.nodes ?? [] }
    } catch { return undefined }
  }

  ctx.on('session/event', (session, event) => {
    if (event?.type === 'compaction/start') {
      const trigger = triggerBySession.get(session) ?? (event.data?.sourceCommandId === undefined ? 'auto' : 'manual')
      const snapshot = {
        at: new Date().toISOString(), trigger, session: String(session?.id ?? ''), compactionId: event.data?.compactionId,
        startSeq: event.seq, surfaceNodes: session?.surface?.nodes?.length ?? 0, pressure: pressureBySession.get(session) ?? null,
        filesWritten: filesWritten(sessionNodes(session), options.filesListed),
      }
      try {
        const dir = join(options.spanDir, runId)
        mkdirSync(dir, { recursive: true })
        writeFileSync(join(dir, `precompact-${String(event.seq).padStart(6, '0')}.json`), JSON.stringify(snapshot, null, 1))
      } catch (error) { log.warn(`context-guardian engine: precompact snapshot not written (${error.message})`) }
      record({ event: 'compaction/start', ...snapshot, filesWritten: snapshot.filesWritten.length })
    } else if (event?.type === 'compaction/end') {
      record({ event: 'compaction/end', session: String(session?.id ?? ''), compactionId: event.data?.compactionId, error: event.data?.error ?? null })
      if (event.data?.error) log.warn(`context-guardian engine: compaction ended with error: ${event.data.error}`)
      triggerBySession.delete(session)
    } else if (event?.type === 'request/context') {
      const host = event.data?.contextWindow
      if (Number.isInteger(host) && host > 0 && session !== null && typeof session === 'object') {
        windowBySession.set(session, host)
        if (!windowLogged.has(session) && host !== options.numCtx) {
          windowLogged.add(session)
          log.info(options.numCtxExplicit
            ? `context-guardian engine: GUARDIAN_NUM_CTX=${options.numCtx} overrides the model's window ${host}`
            : `context-guardian engine: using the model's window ${host} (host-reported); set GUARDIAN_NUM_CTX to override`)
        }
      }
    }
  })

  // ── idle pressure trigger ────────────────────────────────────────────────
  // The per-turn recall counter resets on the same event, so it is registered
  // ALWAYS -- it must not depend on the idle trigger being enabled.
  ctx.on('agent/status', ({ agent, status }) => {
    if (status === 'idle') recallCount.delete(agent)
  })
  const idleTimers = new Map()
  const idleFloor = new WeakMap()
  const clearIdle = (agent) => { const timer = idleTimers.get(agent); if (timer !== undefined) { clearTimeout(timer); idleTimers.delete(agent) } }
  const runIdle = async (agent) => {
    idleTimers.delete(agent)
    const before = measure(agent)
    record({ event: 'idle-check', session: String(agent.session?.id ?? ''), tokens: before?.tokens ?? null, pressure: before === undefined ? null : Number(before.pressure.toFixed(4)), threshold: options.idleCompactRatio })
    if (before === undefined || before.pressure < options.idleCompactRatio) return
    // After a failed or empty attempt, wait for the surface to grow 10 % before trying again.
    if (before.tokens < (idleFloor.get(agent.session) ?? 0)) return
    triggerBySession.set(agent.session, 'idle')
    try {
      const result = await ctx.compaction.compactNow(agent, AbortSignal.timeout(600_000))
      const after = measure(agent)
      if (result === null) idleFloor.set(agent.session, before.tokens * 1.1)
      record({ event: 'idle-compaction', session: String(agent.session?.id ?? ''), before: before.tokens, after: after?.tokens ?? null, compacted: result !== null })
      if (result !== null) log.info(`context-guardian engine: idle compaction at ${(before.pressure * 100).toFixed(0)} % -- ~${before.tokens} -> ~${after?.tokens ?? '?'} tokens`)
    } catch (error) {
      idleFloor.set(agent.session, before.tokens * 1.1)
      record({ event: 'idle-compaction', session: String(agent.session?.id ?? ''), before: before.tokens, error: String(error?.code ?? error?.message ?? error).slice(0, 200) })
    } finally { triggerBySession.delete(agent.session) }
  }
  if (options.idleCompactRatio > 0) {
    ctx.on('agent/status', ({ agent, status }) => {
      clearIdle(agent)
      if (status !== 'idle') return
      const timer = setTimeout(() => { void runIdle(agent) }, options.idleDelayMs)
      timer.unref?.()
      idleTimers.set(agent, timer)
    })
    ctx.effect(() => () => { for (const timer of idleTimers.values()) clearTimeout(timer); idleTimers.clear() })
  }

  // ── reports ──────────────────────────────────────────────────────────────
  const costReport = (agent) => {
    const m = measure(agent)
    if (m === undefined) return 'context-guardian: the token meter is not available in this preset, so pressure cannot be measured.'
    const keep = Math.max(1, Math.ceil(m.nodes.length * 0.16))
    const shadowed = m.nodes.slice(0, Math.max(0, m.nodes.length - keep)).reduce((total, node) => total + Number(node.tokens ?? 0), 0)
    const replacement = Math.min(options.checkpointMaxTokens, Math.floor(shadowed * 0.5))
    const cached = Number(m.usage?.cacheReadTokens ?? 0)
    const window = sessionWindow(agent?.session)
    const cost = lib.rewriteCost({ surfaceTokens: m.tokens, shadowedTokens: shadowed, replacementTokens: replacement, window, cachedPrefixTokens: cached })
    if (cost.error) return `context-guardian: ${cost.error}`
    const cacheLine = m.usage === undefined ? 'cache: no provider usage yet'
      : m.usage.cacheReadTokens === undefined ? 'cache: provider does not report cache reads'
        : `cache: ${cached} of ${m.usage.inputTokens} input tokens read from cache (${m.usage.inputTokens > 0 ? Math.round(cached / m.usage.inputTokens * 100) : 0} %)`
    return [
      `context: ~${m.tokens} of ${window} tokens (${(cost.pressureBefore * 100).toFixed(0)} %), tier ${cost.tier}`,
      `compacting now (estimate): replaces ~${shadowed} tokens with ~${replacement}; saves ~${cost.saved}; pressure -> ${(cost.pressureAfter * 100).toFixed(0)} %`,
      `rewrite cost: ~${cost.reprefillTokens} tokens must be prefilled again because the prompt prefix changes`,
      cacheLine,
      `verdict: ${cost.worthIt ? 'worth compacting' : 'not worth compacting yet'} (ladder: 30 watch · 50 idle · 70 compact · 90 emergency; idle trigger at ${(options.idleCompactRatio * 100).toFixed(0)} %)`,
    ].join('\n')
  }

  const doRecall = (session, request) => {
    if (!request.ok) return { ok: false, text: `recall: ${request.error}` }
    // One recall may never take more than a quarter of THIS session's window.
    return lib.recall(sessionNodes(session), request, { maxTokens: Math.min(options.maxRecallTokens, Math.floor(sessionWindow(session) / 4)) })
  }
  const recallCap = (session) => Math.min(options.maxRecallTokens, Math.floor(sessionWindow(session) / 4))

  // ── status / digest ──────────────────────────────────────────────────────
  const countSpans = () => {
    try {
      return readdirSync(join(options.spanDir, runId)).filter(file => /^\d+\.json$/.test(file)).length
    } catch { return 0 }
  }
  /** One report line. A line that throws reads `label: unavailable (message)`. */
  const line = (label, fn) => {
    try { return `${label}: ${fn()}` } catch (error) { return `${label}: unavailable (${error.message})` }
  }
  const goalText = (session) => {
    const nodes = sessionNodes(session)
    return { goal: extractGoal(nodes).goal, origin: goalOrigin(nodes) }
  }
  const firstChars = (text, max) => {
    const value = String(text ?? '')
    return value.length > max ? value.slice(0, max - 3) + '...' : value
  }

  const statusReport = (agent) => {
    const session = agent?.session
    return [
      `context-guardian ${ENGINE_REV} (memory ${MEMORY_REV}, ${COMPILER_REV}, ${lib.RECALL_REV}) mode ${options.mode}`,
      line('window', () => {
        const host = keyable(session) ? windowBySession.get(session) : undefined
        const source = options.numCtxExplicit ? 'set by numCtx / GUARDIAN_NUM_CTX'
          : Number.isInteger(host) && host > 0 ? 'reported by the model'
            : 'default, the model has not reported one yet'
        return `${sessionWindow(session)} tokens (${source})`
      }),
      line('pressure', () => {
        const m = measure(agent)
        if (m === undefined) return 'the token meter is not available in this preset'
        return `~${m.tokens} of ${sessionWindow(session)} tokens (${(m.pressure * 100).toFixed(0)} %), tier ${tierOf(m.pressure)}`
      }),
      line('last compaction', () => compactionLine(session)),
      line('goal', () => {
        const { origin } = goalText(session)
        const updates = ` (+${origin.updates} updates)`
        if (origin.fromCheckpoint) return `pinned from a checkpoint${updates}`
        if (origin.seq !== null) return `pinned from seq ${origin.seq}${updates}`
        return 'none yet'
      }),
      line('memory', () => {
        if (options.memoryMaxTokens <= 0) return 'off (memoryMaxTokens 0)'
        const stats = memoryStats(memory)
        const by = stats.byCategory
        return `${stats.total} items (decisions ${by.decisions}, constraints ${by.constraints}, files ${by.files}, todos ${stats.openTodos} open, errors ${by.errors}, preferences ${by.preferences}) in ${options.memoryPath}`
      }),
      line('anchors', () => {
        if (options.anchorCheck === 'off') return 'off'
        const st = keyable(session) ? anchorsBySession.get(session) : undefined
        if (st === undefined) return 'no compaction yet'
        return `last compaction ${st.recurring} recurring, ${st.kept} kept, ${st.lost + st.constraintsLost} carried (mode ${options.anchorCheck})`
      }),
      line('append-only', () => options.appendOnly
        ? `on (chain max ${options.chainMaxCheckpoints} checkpoints, ${options.chainMaxTokens > 0 ? options.chainMaxTokens : 'auto'} tokens)`
        : 'off'),
      line('spans archived', () => `${countSpans()} in this run (${options.spanDir}/${runId})`),
      line('recall this turn', () => `${keyable(agent) ? (recallCount.get(agent) ?? 0) : 0} of ${options.recallMaxPerTurn > 0 ? options.recallMaxPerTurn : 'unlimited'}`),
    ].join('\n')
  }

  const digest = (session) => {
    const out = ['DIGEST (context-guardian)']
    const { goal, origin } = goalText(session)
    out.push(goal === null || goal === undefined ? 'goal: none yet'
      : `goal: ${firstChars(goal, 300)} (${origin.fromCheckpoint ? 'from a checkpoint' : `seq ${origin.seq}`}) +${origin.updates} updates`)

    const mine = itemsForSession(memory, String(session?.id ?? ''))
      .slice()
      .sort((a, b) => (a.last || '').localeCompare(b.last || '') || (a.seq || 0) - (b.seq || 0))
      .slice(-20)
    out.push('memory updated this session:')
    out.push(...(mine.length > 0 ? mine.map(item => `- [${item.cat}] ${item.text}`) : ['- none']))

    const listed = filesWritten(sessionNodes(session), 30).map(file => file.path)
    for (const item of memory.items ?? []) if (item?.cat === 'files' && !listed.includes(item.text)) listed.push(item.text)
    out.push(listed.length > 0 ? `files written: ${listed.join(', ')}` : 'files written: none')

    const todos = (memory.items ?? []).filter(item => item?.cat === 'todos' && item.done !== true).slice(0, 20)
    out.push('open todos:')
    out.push(...(todos.length > 0 ? todos.map(item => `- ${item.text}`) : ['- none']))

    const errors = (memory.items ?? []).filter(item => item?.cat === 'errors')
    const lastError = errors.slice().sort((a, b) => (a.last || '').localeCompare(b.last || '') || (a.seq || 0) - (b.seq || 0)).pop()
    out.push(lastError === undefined ? 'last error: none' : `last error: ${lastError.text} (seq ${lastError.seq})`)

    out.push(`last compaction: ${compactionLine(session)}`)
    return out.join('\n')
  }

  // ── model-facing tools ───────────────────────────────────────────────────
  const definitions = {
    recall: {
      name: 'recall',
      description: 'Restore ORIGINAL conversation text behind a checkpoint pointer. type "seq" id "3-7" | type "result" id "3" (a tool result) | type "checkpoint" id "1".',
      parameters: {
        type: 'object',
        properties: { type: { type: 'string', enum: ['seq', 'result', 'checkpoint'] }, id: { type: 'string', description: '"7", "3-7"' } },
        required: ['id'],
      },
      output: TEXT_OUTPUT,
      execute: async (args, exec) => {
        const agent = exec?.agent
        // Per-turn budget for the MODEL-facing tool only; /recall is never limited.
        if (options.recallMaxPerTurn > 0 && keyable(agent)) {
          const used = recallCount.get(agent) ?? 0
          if (used >= options.recallMaxPerTurn) {
            return `recall: limit of ${options.recallMaxPerTurn} recalls per turn reached (each one adds up to ${recallCap(agent.session)} tokens to the window). Use search to find the exact seq, then recall a narrow range in your next turn.`
          }
          recallCount.set(agent, used + 1)
        }
        return doRecall(agent?.session, lib.parseRecallRequest(args?.type, args?.id)).text
      },
    },
    search: {
      name: 'search',
      description: 'Find where something was said earlier in this session (including compacted history). Returns seq numbers for recall.',
      parameters: {
        type: 'object',
        properties: { query: { type: 'string' }, regex: { type: 'boolean' } },
        required: ['query'],
      },
      output: TEXT_OUTPUT,
      execute: async (args, exec) => lib.renderSearch(
        lib.search(sessionNodes(exec?.agent?.session), args?.query, { maxHits: options.maxSearchHits, regex: args?.regex === true, contextChars: 80 }),
        String(args?.query ?? ''),
      ),
    },
    context_rewrite_cost: {
      name: 'context_rewrite_cost',
      description: 'Report context pressure, what compacting now would save, and what it costs in re-prefill.',
      parameters: { type: 'object', properties: {} },
      output: TEXT_OUTPUT,
      execute: async (_args, exec) => costReport(exec?.agent),
    },
    guardian_status: {
      name: 'guardian_status',
      description: 'Report Context Guardian status: engine revision, context window, pressure, last compaction, pinned goal, memory items and archived spans.',
      parameters: { type: 'object', properties: {} },
      output: TEXT_OUTPUT,
      execute: async (_args, exec) => statusReport(exec?.agent),
    },
    context_compact: {
      name: 'context_compact',
      description: 'Ask for the conversation to be compacted as soon as this turn ends. Use before starting a long new task when context is above 50 %.',
      parameters: { type: 'object', properties: {} },
      output: TEXT_OUTPUT,
      execute: async (_args, exec) => {
        const agent = exec?.agent
        if (agent === undefined) return 'context_compact: no agent on this call.'
        idleFloor.delete(agent.session)
        pendingCompact.add(agent)
        return 'Compaction is scheduled for the end of this turn. Finish your current step normally.'
      },
    },
  }
  const pendingCompact = new WeakSet()
  if (options.tools.includes('context_compact')) {
    ctx.on('agent/status', ({ agent, status }) => {
      if (status !== 'idle' || !pendingCompact.has(agent)) return
      pendingCompact.delete(agent)
      triggerBySession.set(agent.session, 'model')
      ctx.compaction.compactNow(agent, AbortSignal.timeout(600_000))
        .then(result => record({ event: 'model-compaction', session: String(agent.session?.id ?? ''), compacted: result !== null }))
        .catch(error => record({ event: 'model-compaction', session: String(agent.session?.id ?? ''), error: String(error?.code ?? error?.message ?? error).slice(0, 200) }))
        .finally(() => triggerBySession.delete(agent.session))
    })
  }
  if (options.tools.length > 0) {
    ctx.inject(['tools'], (inner) => {
      for (const tool of options.tools) inner.tools.register(definitions[tool])
      log.info(`context-guardian engine: tools registered: ${options.tools.join(', ')}`)
    })
  }

  // ── human commands ───────────────────────────────────────────────────────
  ctx.inject(['commands'], (inner) => {
    inner.commands.register({
      name: 'recall',
      description: 'Show original history: /recall 3-7 · /recall result 3 · /recall checkpoint 1 · /recall find <text> · /recall digest',
      // Without `input` DSH treats a command as argument-free: the web UI runs it bare on
      // select and sends "/recall result 42" typed in full to the MODEL (seen live 2026-09-20).
      input: { hint: '<3-7 | result 3 | checkpoint 1 | find text | digest>' },
      handler: async (invocation) => {
        const raw = String(invocation?.rawInput ?? '').trim()
        const session = invocation?.agent?.session
        if (/^digest$/i.test(raw)) return { kind: 'success', text: digest(session) }
        const find = /^(?:find|search)\s+(.+)$/i.exec(raw)
        if (find !== null) {
          const result = lib.search(sessionNodes(session), find[1], { maxHits: options.maxSearchHits, regex: false, contextChars: 80 })
          return { kind: result.ok ? 'success' : 'error', text: lib.renderSearch(result, find[1]) }
        }
        const result = doRecall(session, lib.parseRecallCommand(raw))
        return { kind: result.ok ? 'success' : 'error', text: result.text }
      },
    })
    inner.commands.register({
      name: 'guardian',
      description: 'Context Guardian status: engine, window, pressure, last compaction, goal, memory, spans',
      handler: async (invocation) => ({ kind: 'success', text: statusReport(invocation?.agent) }),
    })
    inner.commands.register({
      name: 'context',
      description: 'Context pressure, cache hits and what compacting now would cost',
      handler: async (invocation) => ({ kind: 'success', text: costReport(invocation?.agent) }),
    })
  })
}
