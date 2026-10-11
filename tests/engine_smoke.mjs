// Contract smoke for engine.js: a fake cordis ctx + fake compaction-basic service.
// usage: node tests/engine_smoke.mjs      (exit 0 iff "0 failed")
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { emptyMemory, loadMemory, mergeMemory, saveMemory } from '../cg_memory.js'
import { RECALL_GUIDE } from '../vendor/compiler.js'
import * as engine from '../engine.js'

const LIVE_ERROR = 'summarization produced no text summary content'
const T = (text) => ({ type: 'text', text })
const clone = (value) => (value === null ? null : JSON.parse(JSON.stringify(value)))

/** A session whose message events start at seq 100, to prove pointers are REAL seqs and not ordinals. */
function makeSession() {
  const events = []
  for (let seq = 0; seq < 100; seq += 1) events.push({ seq, type: 'assistant/chunk', data: {} })
  const push = (message) => events.push({ seq: events.length, type: 'user/message', data: message })
  for (let round = 0; round < 12; round += 1) {
    push({ role: 'user', content: [T(`Round ${round}: please update scripts/gpu_broker.py so lease_timeout honours the gaming toggle. ${'Detail sentence about the broker. '.repeat(12)}`)] })
    push({ role: 'assistant', content: [{ type: 'reasoning', text: 'hidden' }, T(`Editing scripts/gpu_broker.py now, round ${round}. ${'Explanation of the change. '.repeat(10)}`), { type: 'tool-call', id: `c${round}`, name: 'edit', arguments: JSON.stringify({ path: `scripts/file_${round % 3}.py`, old: 'a', new: 'b' }) }] })
    push({ role: 'user', content: [{ type: 'tool-result', toolCallId: `c${round}`, content: [T(`edited ok UNIQUE-RESULT-${round} ${'log line '.repeat(60)}`)] }] })
  }
  const surface = events.filter(event => event.type === 'user/message').map(event => event.seq)
  return {
    id: 'sess-smoke', events, surface: { nodes: surface, replaceGeneration: 0 },
    deriveEventMessage: (event) => (event?.type === 'user/message' ? clone(event.data) : null),
  }
}

function makeCtx({ llm = 'throw', pressure = 0.85, withMeter = true, tokens = null } = {}) {
  const listeners = new Map()
  const disposers = []
  const logs = []
  const tools = new Map()
  const commands = new Map()
  const calls = { llm: 0, compactNow: 0 }
  class FakeEngine {
    async summarize(input) {
      calls.llm += 1
      calls.lastInput = input
      if (llm === 'throw') throw new Error(LIVE_ERROR)
      if (llm === 'mustkeep') return { summary: [T('## Primary Request and Intent\n- x\n\n## Must Keep\n- the release codename for project Heron is TUNNEL-611\n- (none)')], provider: 'p', model: 'm', rawOutput: '', llmStreamCall: true }
      if (llm === 'empty') return { summary: [T('   ')], provider: 'p', model: 'm', rawOutput: '', llmStreamCall: true }
      return { summary: [T('LLM SUMMARY')], provider: 'p', model: 'm', rawOutput: 'LLM SUMMARY', llmStreamCall: true }
    }
    async compactNow() { calls.compactNow += 1; return { shadowedSeqs: [1] } }
  }
  const compaction = new FakeEngine()
  const ctx = {
    compaction,
    logger: { info: m => logs.push(`info ${m}`), warn: m => logs.push(`warn ${m}`), error: m => logs.push(`error ${m}`), debug: () => {} },
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); return () => {} },
    effect(fn) { const dispose = fn(); if (typeof dispose === 'function') disposers.push(dispose) },
    // cordis: a service that is not in the plugin's `inject` is NOT readable from its ctx.
    // Only `compaction` is declared, so everything else must arrive through ctx.inject().
    inject(deps, cb) { if (deps.every(dep => services[dep] !== undefined)) cb(Object.assign(Object.create(ctx), Object.fromEntries(deps.map(dep => [dep, services[dep]])))) },
  }
  const services = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name) } },
    commands: { register(def) { commands.set(def.name, def); return () => commands.delete(def.name) } },
  }
  if (withMeter) {
    const total = () => (tokens === null ? Math.round(pressure * 32768) : tokens)
    services.tokenMeter = { measure: (session) => ({ totalTokens: total(), surfaceTokens: total(), baseline: { kind: 'usage', usage: { inputTokens: 20000, outputTokens: 10, cacheReadTokens: 15000 } }, nodes: session.surface.nodes.map(seq => ({ seq, tokens: 600, heuristicTokens: 600 })) }) }
  }
  const emit = (name, ...args) => { for (const fn of listeners.get(name) ?? []) fn(...args) }
  const dispose = () => { while (disposers.length > 0) disposers.pop()() }
  return { ctx, compaction, logs, tools, commands, calls, emit, dispose }
}

const tmp = mkdtempSync(join(tmpdir(), 'cg-engine-'))
const baseConfig = (extra = {}) => ({ spanDir: join(tmp, 'spans'), logPath: join(tmp, 'log.jsonl'), idleDelayMs: 0, ...extra })
const regionOf = (session, count) => session.surface.nodes.slice(0, count).map(seq => session.deriveEventMessage(session.events[seq]))
const smallInput = (session) => ({ system: 'sys', tools: [], messages: regionOf(session, 30) })
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))

/** A session built from raw messages, for checks that need an exact region. */
const makeSessionOf = (messages, id = 'sess-custom') => {
  const events = messages.map((data, index) => ({ seq: index, type: 'user/message', data }))
  return {
    id, events, surface: { nodes: events.map(event => event.seq), replaceGeneration: 0 },
    deriveEventMessage: (event) => (event?.type === 'user/message' ? clone(event.data) : null),
  }
}
const wholeInput = (session) => ({ system: 'sys', tools: [], messages: session.events.map(event => event.data) })
/** One assistant turn that only edits `path` -- no prose, so memory holds files only. */
const editTurns = (paths) => paths.map((path, index) => ({
  role: 'assistant',
  content: [{ type: 'tool-call', id: `t${index}`, name: 'edit', arguments: JSON.stringify({ path }) }],
}))
const checkpointTurn = (text) => ({ role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T(text)] })
const readLog = (path) => {
  try { return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) } catch { return [] }
}
/** A memory file holding exactly one durable fact. */
const seedMemory = (path, item) => saveMemory(path, mergeMemory(emptyMemory(), [{ first: '2026-01-01T00:00:00.000Z', last: '2026-01-01T00:00:00.000Z', ...item }]))
const fileLineOf = (text) => text.split('\n').find(line => line.startsWith('FILES WRITTEN'))

const checks = []
const check = (name, fn) => checks.push([name, fn])

check('hook_installed_and_logged', () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  return Object.hasOwn(h.compaction, 'summarize') && h.logs.some(line => line.includes('summarize hook installed'))
})
check('mode_off_installs_nothing', () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig({ mode: 'off' }))
  return !Object.hasOwn(h.compaction, 'summarize') && h.tools.size === 0
})
check('llm_success_passes_through_untouched', async () => {
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  // The stock summary text and object shape pass through unchanged. Since the
  // goal-pin change, a pinned-goal block may be prepended as summary[0], so
  // assert the stock block is present rather than at a fixed index.
  return result.llmStreamCall === true && result.summary.some(b => b.text === 'LLM SUMMARY') && h.calls.llm === 1
})
check('live_error_falls_back_to_deterministic_with_real_seq_pointers', async () => {
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const input = smallInput(session)
  const result = await h.compaction.summarize(input, { session })
  const text = result.summary[0].text
  const inputChars = JSON.stringify(input.messages).length
  return h.calls.llm === 1 && result.provider === 'context-guardian' && result.llmStreamCall === undefined
    && text.trim().length > 0 && text.length < inputChars / 2
    && /seq 1\d\d/.test(text) && !/\(seq [0-9]\b/.test(text) && text.includes('RECALL:') && text.includes('seqs 100-129')
})
check('empty_llm_summary_falls_back', async () => {
  const h = makeCtx({ llm: 'empty' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  return result.provider === 'context-guardian'
})
check('abort_is_not_swallowed', async () => {
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const controller = new AbortController(); controller.abort()
  try { await h.compaction.summarize(smallInput(session), { session }, controller.signal); return false } catch (error) { return error.message === LIVE_ERROR }
})
check('doomed_llm_call_is_never_made', async () => {
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const input = { system: 'x'.repeat(100_000), tools: [], messages: regionOf(session, 30) }
  const result = await h.compaction.summarize(input, { session })
  return h.calls.llm === 0 && result.provider === 'context-guardian'
})
check('mode_deterministic_never_calls_llm', async () => {
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig({ mode: 'deterministic' }))
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  return h.calls.llm === 0 && result.provider === 'context-guardian'
})
check('env_overrides_row', () => {
  const o = engine.resolveEngineOptions({ mode: 'llm-then-deterministic', numCtx: 8192, maxRecallTokens: 16000 }, { GUARDIAN_DSH_MODE: 'deterministic', GUARDIAN_NUM_CTX: '65536', GUARDIAN_IDLE_COMPACT_RATIO: 'banana' })
  const p = engine.resolveEngineOptions({ numCtx: 8192, maxRecallTokens: 16000, tools: ['recall', 'nope'] }, {})
  return o.mode === 'deterministic' && o.numCtx === 65536 && o.idleCompactRatio === 0 && p.maxRecallTokens === 2048 && JSON.stringify(p.tools) === '["recall"]'
})
check('checkpoint_lists_files_and_keyword_index', async () => {
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const text = (await h.compaction.summarize(smallInput(session), { session })).summary[0].text
  return text.includes('FILES WRITTEN') && text.includes('scripts/file_0.py') && text.includes('KEYWORD INDEX') && text.includes('scripts/gpu_broker.py:')
})
check('injected_context_is_omitted_and_named_by_seq', () => {
  const nodes = [
    { seq: 8, message: { role: 'user', source: { kind: 'user' }, content: [T('Human request about scripts/gpu_broker.py. ' + 'More words here. '.repeat(40))] } },
    { seq: 9, message: { role: 'user', source: { kind: 'agent-instructions' }, content: [T('<system-reminder>INSTRUCTION-DUMP ' + 'rule '.repeat(2000) + '</system-reminder>')] } },
    { seq: 10, message: { role: 'user', source: { kind: 'plugin', plugin: '@deepseek-ai/dsh-system-prompt' }, content: [T('RUNTIME-SNAPSHOT ' + 'x '.repeat(200))] } },
    { seq: 11, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T('PRIOR-CHECKPOINT body')] } },
    { seq: 12, message: { role: 'assistant', content: [T('Answer mentioning scripts/gpu_broker.py again. ' + 'Words. '.repeat(40))] } },
  ]
  const cp = engine.buildCheckpoint(nodes, engine.resolveEngineOptions({}, {}), 0.8)
  return cp.dropped === 2 && !cp.text.includes('INSTRUCTION-DUMP') && !cp.text.includes('RUNTIME-SNAPSHOT') && cp.text.includes('PRIOR-CHECKPOINT') && cp.text.includes('Human request')
    && cp.text.includes('2 harness-injected context messages omitted') && cp.text.includes('seqs 9, 10')
})
check('keyword_index_keeps_identifiers_not_english', () => {
  const mk = (seq, text) => ({ seq, message: { role: 'assistant', content: [T(text)] } })
  const nodes = [mk(1, 'check the files found in scripts/gpu_broker.py ' + 'pad '.repeat(100)), mk(2, 'check files found again scripts/gpu_broker.py lease_timeout ' + 'pad '.repeat(100)), mk(3, 'found lease_timeout check files ' + 'pad '.repeat(100))]
  const text = engine.buildCheckpoint(nodes, engine.resolveEngineOptions({}, {}), 0.8).text
  const index = text.slice(text.indexOf('KEYWORD INDEX'))
  const listing = (seq) => ({ seq, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'c' + seq, content: [T('_archive/never_mentioned_file.py scripts/gpu_broker.py ' + 'pad '.repeat(50))] }] } })
  nodes.push(listing(4), listing(5))
  const text2 = engine.buildCheckpoint(nodes, engine.resolveEngineOptions({}, {}), 0.8).text
  if (text2.includes('never_mentioned_file') || !text2.includes('scripts/gpu_broker.py: 1, 2, 4, 5')) return false
  return index.includes('scripts/gpu_broker.py: 1, 2') && index.includes('lease_timeout: 2, 3') && !/^check:/m.test(index) && !/^files:/m.test(index) && !/^found:/m.test(index)
})
check('emergency_tier_is_tighter_than_compact_tier', () => {
  const session = makeSession()
  const { nodes } = engine.mapRegionSeqs(session, regionOf(session, 36))
  const options = engine.resolveEngineOptions({ checkpointMaxTokens: 1200 }, {})
  const compact = engine.buildCheckpoint(nodes, options, 0.75), emergency = engine.buildCheckpoint(nodes, options, 0.95)
  return compact.tier === 'compact' && emergency.tier === 'emergency' && emergency.cap < compact.cap && emergency.stats.tokens <= compact.stats.tokens
})
check('unmatched_messages_are_reported_not_mislabelled', () => {
  const session = makeSession()
  const { nodes, unmatched } = engine.mapRegionSeqs(session, [{ role: 'user', content: [T('never in the log')] }, session.deriveEventMessage(session.events[100])])
  return unmatched === 1 && nodes[0].seq < 0 && nodes[1].seq === 100
})
check('span_archived_in_write_span_format', async () => {
  const h = makeCtx({ llm: 'throw' }); const config = baseConfig({ spanDir: join(tmp, 'spans-a') }); engine.apply(h.ctx, config)
  const session = makeSession()
  await h.compaction.summarize(smallInput(session), { session })
  const run = readdirSync(config.spanDir)[0]
  const span = JSON.parse(readFileSync(join(config.spanDir, run, '0001.json'), 'utf8'))
  return ['run_id', 'index', 'at', 'num_ctx', 'message_count', 'summary', 'messages'].every(key => key in span) && span.index === 1 && span.message_count === 30 && span.messages.length === 30
})
check('span_write_failure_leaves_no_file', async () => {
  // The span file is claimed with openSync('wx') before the JSON is written. A message that cannot be
  // serialised INSIDE the span (toJSON throws for any non-root key) makes that write fail after the claim;
  // the claimed file must be removed, not left empty for recall/prune to read as an archived span.
  const h = makeCtx({ llm: 'throw' }); const config = baseConfig({ mode: 'deterministic', spanDir: join(tmp, 'spans-fail'), logPath: join(tmp, 'log-fail.jsonl') }); engine.apply(h.ctx, config)
  const session = makeSession()
  const input = smallInput(session)
  const poison = { role: 'user', content: [T('unserialisable in a span')] }
  Object.defineProperty(poison, 'toJSON', { enumerable: false, value: (key) => { if (key !== '') throw new Error('cannot serialise'); return { role: poison.role, content: poison.content } } })
  input.messages.push(poison)
  await h.compaction.summarize(input, { session })
  const runs = existsSync(config.spanDir) ? readdirSync(config.spanDir, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name) : []
  const left = runs.flatMap(run => readdirSync(join(config.spanDir, run)).filter(f => /^\d{4}\.json$/.test(f)))
  if (left.length) console.log('    left behind:', left.join(', '))
  return left.length === 0 && h.logs.some(line => line.includes('span NOT archived'))
})
check('precompact_snapshot_and_outcome_log', () => {
  const h = makeCtx(); const config = baseConfig({ spanDir: join(tmp, 'spans-b'), logPath: join(tmp, 'log-b.jsonl') }); engine.apply(h.ctx, config)
  const session = makeSession()
  h.emit('session/event', session, { seq: 140, type: 'compaction/start', data: { compactionId: 'cmp-1', turn: 3 } })
  h.emit('session/event', session, { seq: 143, type: 'compaction/end', data: { compactionId: 'cmp-1', turn: 3, error: LIVE_ERROR } })
  const run = readdirSync(config.spanDir)[0]
  const snapshot = JSON.parse(readFileSync(join(config.spanDir, run, 'precompact-000140.json'), 'utf8'))
  const lines = readFileSync(config.logPath, 'utf8').trim().split('\n').map(line => JSON.parse(line))
  return typeof snapshot.at === 'string' && snapshot.trigger === 'auto' && snapshot.session === 'sess-smoke' && Array.isArray(snapshot.filesWritten) && snapshot.filesWritten.length === 3
    && lines.some(line => line.event === 'compaction/end' && line.error === LIVE_ERROR) && h.logs.some(line => line.startsWith('warn') && line.includes(LIVE_ERROR))
})
check('default_tools_are_recall_and_search_only', () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  const all = makeCtx(); engine.apply(all.ctx, baseConfig({ tools: ['recall', 'search', 'context_rewrite_cost', 'context_compact'] }))
  return JSON.stringify([...h.tools.keys()]) === '["recall","search"]' && all.tools.size === 4
})
check('recall_tool_restores_original_text', async () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const byResult = await h.tools.get('recall').execute({ type: 'result', id: '101' }, { agent: { session } })
  const bySeq = await h.tools.get('recall').execute({ id: '100-101' }, { agent: { session } })
  const bad = await h.tools.get('recall').execute({ id: 'banana' }, { agent: { session } })
  return byResult.includes('UNIQUE-RESULT-0') && bySeq.includes('[seq 100 user]') && bySeq.includes('* edit(') && bad.startsWith('recall: ') && bad.includes('seq "3-7"')
})
check('search_tool_points_at_recall', async () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const out = await h.tools.get('search').execute({ query: 'UNIQUE-RESULT-7' }, { agent: { session } })
  return out.includes('1 hits') && out.trimEnd().endsWith('NEXT STEP: recall(type="seq", id="123")')
})
check('recall_command', async () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const ok = await h.commands.get('recall').handler({ rawInput: ' 100-101 ', agent: { session } })
  const find = await h.commands.get('recall').handler({ rawInput: 'find UNIQUE-RESULT-3', agent: { session } })
  const bad = await h.commands.get('recall').handler({ rawInput: '', agent: { session } })
  if (typeof h.commands.get('recall').input?.hint !== 'string' || h.commands.get('context').input !== undefined) return false
  return ok.kind === 'success' && ok.text.includes('[seq 100 user]') && find.kind === 'success' && find.text.includes('seq 111') && bad.kind === 'error'
})
check('context_command_reports_pressure_cost_and_cache', async () => {
  const h = makeCtx({ pressure: 0.75 }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  const out = (await h.commands.get('context').handler({ rawInput: '', agent: { session } })).text
  const none = makeCtx({ withMeter: false }); engine.apply(none.ctx, baseConfig())
  const missing = (await none.commands.get('context').handler({ rawInput: '', agent: { session } })).text
  return out.includes('tier compact') && out.includes('75 %') && out.includes('15000 of 20000') && out.includes('prefilled again') && missing.includes('token meter is not available')
})
check('idle_trigger_fires_above_ratio_only', async () => {
  const high = makeCtx({ pressure: 0.5 }); engine.apply(high.ctx, baseConfig({ idleCompactRatio: 0.45 })) // default is 0 (off) since 2026-10-01
  const low = makeCtx({ pressure: 0.2 }); engine.apply(low.ctx, baseConfig({ idleCompactRatio: 0.45 }))
  const off = makeCtx({ pressure: 0.9 }); engine.apply(off.ctx, baseConfig({ idleCompactRatio: 0 }))
  const session = makeSession()
  for (const h of [high, low, off]) h.emit('agent/status', { agent: { session }, status: 'idle' })
  await sleep(30)
  return high.calls.compactNow === 1 && low.calls.compactNow === 0 && off.calls.compactNow === 0
})
check('idle_trigger_cancelled_when_agent_becomes_busy', async () => {
  const h = makeCtx({ pressure: 0.6 }); engine.apply(h.ctx, baseConfig({ idleDelayMs: 40, idleCompactRatio: 0.45 }))
  const agent = { session: makeSession() }
  h.emit('agent/status', { agent, status: 'idle' })
  h.emit('agent/status', { agent, status: 'running' })
  await sleep(90)
  return h.calls.compactNow === 0
})
check('idle_trigger_labels_the_snapshot', async () => {
  const h = makeCtx({ pressure: 0.6 }); const config = baseConfig({ spanDir: join(tmp, 'spans-c'), idleCompactRatio: 0.45 }); engine.apply(h.ctx, config)
  const agent = { session: makeSession() }
  h.compaction.compactNow = async () => { h.emit('session/event', agent.session, { seq: 150, type: 'compaction/start', data: { compactionId: 'cmp-2', turn: null } }); return null }
  h.emit('agent/status', { agent, status: 'idle' })
  await sleep(30)
  const run = readdirSync(config.spanDir)[0]
  return JSON.parse(readFileSync(join(config.spanDir, run, 'precompact-000150.json'), 'utf8')).trigger === 'idle'
})
check('dispose_restores_the_stock_summarizer', async () => {
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig({ mode: 'deterministic' }))
  h.dispose()
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  return !Object.hasOwn(h.compaction, 'summarize') && result.summary[0].text === 'LLM SUMMARY'
})

check('effective_window_table', () => {
  const defaults = engine.resolveEngineOptions({}, {})
  const explicit = engine.resolveEngineOptions({}, { GUARDIAN_NUM_CTX: '65536' })
  const rows = [
    [defaults, 1_000_000, false, 1_000_000], // host window honoured when not explicit
    [defaults, undefined, false, 32768],      // no host window -> the default
    [explicit, 1_000_000, true, 65536],       // explicit numCtx always wins
    [defaults, 0, false, 32768],              // non-positive / non-integer fall back
    [defaults, -5, false, 32768],
    [defaults, 1.5, false, 32768],
    [defaults, '200000', false, 32768],
  ]
  return rows.every(([options, host, exp, want]) => engine.effectiveWindow(options, host, exp) === want)
})
check('resolve_engine_options_reports_num_ctx_explicit', () => {
  const none = engine.resolveEngineOptions({}, {})
  const fromEnv = engine.resolveEngineOptions({}, { GUARDIAN_NUM_CTX: '65536' })
  const fromRow = engine.resolveEngineOptions({ numCtx: 8192 }, {})
  return none.numCtxExplicit === false && fromEnv.numCtxExplicit === true
    && fromEnv.numCtx === 65536 && fromRow.numCtxExplicit === true && fromRow.numCtx === 8192
})
check('host_context_window_lowers_pressure_and_logs_once', async () => {
  const h = makeCtx({ tokens: 20_000 }); engine.apply(h.ctx, baseConfig())
  const session = makeSession()
  h.emit('session/event', session, { seq: 1, type: 'request/context', data: { contextWindow: 1_000_000 } })
  h.emit('session/event', session, { seq: 2, type: 'request/context', data: { contextWindow: 1_000_000 } })
  const out = (await h.commands.get('context').handler({ rawInput: '', agent: { session } })).text
  const hostLogs = h.logs.filter(line => line.includes("using the model's window 1000000"))
  // 20k tokens is ~2 % of a 1M window (not the ~61 % of the 32k default).
  return out.includes('of 1000000 tokens (2 %)') && !out.includes('61 %') && hostLogs.length === 1
})

// ── durable memory, durable files, /guardian, recall limits (cg-engine-4) ────

check('defaults_resolve_memory_and_recall_limit', () => {
  const plain = engine.resolveEngineOptions({}, {})
  const off = engine.resolveEngineOptions({}, { GUARDIAN_MEMORY_MAX_TOKENS: '0' })
  const envPath = join(tmp, 'chk-defaults-env.json')
  const env = engine.resolveEngineOptions({}, { GUARDIAN_MEMORY_PATH: envPath })
  const row = engine.resolveEngineOptions({ memoryPath: join(tmp, 'chk-defaults-row.json') }, {})
  return plain.memoryMaxTokens === 1200 && plain.recallMaxPerTurn === 4
    && plain.memoryPath === join(plain.spanDir, 'memory.json')
    && off.memoryMaxTokens === 0 && env.memoryPath === envPath && row.memoryPath === join(tmp, 'chk-defaults-row.json')
})
check('memory_loaded_at_apply_and_rendered', async () => {
  const memoryPath = join(tmp, 'chk-loaded', 'memory.json')
  const config = baseConfig({ memoryPath, logPath: join(tmp, 'chk-loaded.jsonl') })
  seedMemory(memoryPath, { cat: 'decisions', text: 'use sqlite for the ledger', seq: 4, session: 'sess-smoke' })
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, config)
  const session = makeSession()
  // The file on disk is already in the closure before any compaction happens.
  const before = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  const text = (await h.compaction.summarize(smallInput(session), { session })).summary[0].text
  const loaded = readLog(config.logPath).find(entry => entry.event === 'memory-loaded')
  return text.includes('use sqlite for the ledger') && text.includes('[memory --')
    && text.indexOf('[memory --') !== -1 && text.indexOf('[memory --') < text.indexOf(RECALL_GUIDE)
    && loaded !== undefined && loaded.status === 'loaded' && loaded.path === memoryPath && loaded.items === 1
    && before.includes(`memory: 1 items (decisions 1, constraints 0, files 0, todos 0 open, errors 0, preferences 0) in ${memoryPath}`)
})
check('memory_saved_after_compaction', async () => {
  const memoryPath = join(tmp, 'chk-saved', 'memory.json')
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig({ memoryPath }))
  const session = makeSessionOf([{ role: 'user', content: [T('decision: keep the broker on port 8790')] }])
  await h.compaction.summarize(wholeInput(session), { session })
  const items = loadMemory(memoryPath).memory.items
  return items.some(item => item.cat === 'decisions' && item.text === 'keep the broker on port 8790' && item.session === 'sess-custom')
})
check('memory_prepended_to_llm_summary', async () => {
  const memoryPath = join(tmp, 'chk-llm', 'memory.json')
  seedMemory(memoryPath, { cat: 'preferences', text: 'prefer ruff over flake8', seq: 3, session: 'sess-smoke' })
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig({ memoryPath, anchorCheck: 'off' })) // exact block count: keep the anchor check out of it
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  const texts = result.summary.map(block => block.text)
  // 2026-10-01: the LLM path now also carries the recovery note (search/recall before answering, inline text is not on disk)
  return result.llmStreamCall === true && texts.length === 4
    && texts[0].includes("[pinned goal --") && texts[1].includes('[memory --') && texts[1].includes('prefer ruff over flake8')
    && texts[2] === engine.RECOVERY_NOTE && texts[3] === 'LLM SUMMARY'
})
check('must_keep_is_asked_and_pinned', async () => {
  // 2026-10-01: the summary call is asked for `## Must Keep`; its bullets become pins of the session.
  const memoryPath = join(tmp, 'chk-mk', 'memory.json')
  const h = makeCtx({ llm: 'mustkeep' }); engine.apply(h.ctx, baseConfig({ memoryPath, anchorCheck: 'off' }))
  const session = makeSession()
  await h.compaction.summarize(smallInput(session), { session })
  const last = h.calls.lastInput.messages.at(-1)
  const asked = last.role === 'user' && last.content[0].text === engine.MUST_KEEP_INSTRUCTION
  const saved = JSON.parse(readFileSync(memoryPath, 'utf8')).items.filter(it => it.cat === 'pins')
  const off = makeCtx({ llm: 'mustkeep' }); engine.apply(off.ctx, baseConfig({ memoryPath: join(tmp, 'chk-mk2', 'memory.json'), mustKeep: false }))
  await off.compaction.summarize(smallInput(session), { session })
  const notAsked = off.calls.lastInput.messages.at(-1)?.content?.[0]?.text !== engine.MUST_KEEP_INSTRUCTION
  return asked && notAsked && saved.length === 1 && saved[0].text.includes('TUNNEL-611') && saved[0].session === String(session.id)
})
check('memory_off_writes_nothing', async () => {
  const memoryPath = join(tmp, 'chk-off', 'memory.json')
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig({ memoryPath, memoryMaxTokens: 0 }))
  const session = makeSession()
  const text = (await h.compaction.summarize(smallInput(session), { session })).summary[0].text
  const status = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  return !existsSync(memoryPath) && !text.includes('[memory --') && status.includes('memory: off (memoryMaxTokens 0)')
})
check('memory_failure_never_breaks_compaction', async () => {
  const dir = join(tmp, 'chk-fail'); mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'blocker'), 'a plain file where a directory would have to be')
  const config = baseConfig({ memoryPath: join(dir, 'blocker', 'memory.json') })
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, config)
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  return result.provider === 'context-guardian' && result.summary[0].text.includes('RECALL:')
    && result.summary[0].text.includes('seqs 100-129')
    // Reading a path THROUGH a plain file is ENOTDIR on POSIX (-> "unreadable") but ENOENT on Windows
    // (-> "missing", start fresh); either is right. The unreadable warning itself is proven cross-platform by
    // corrupt_memory_file_is_warned_and_moved_aside below.
    && (h.logs.some(line => line.startsWith('warn') && line.includes(`memory at ${config.memoryPath} is unreadable`))
      || readLog(config.logPath).some(entry => entry.event === 'memory-loaded' && entry.status === 'missing'))
    && h.logs.some(line => line.startsWith('warn') && line.includes('memory NOT written to') && line.includes('compaction proceeds'))
    && readLog(config.logPath).some(entry => entry.event === 'memory' && entry.saved === false)
})
check('corrupt_memory_file_is_warned_and_moved_aside', async () => {
  const dir = join(tmp, 'chk-corrupt'); mkdirSync(dir, { recursive: true })
  const memoryPath = join(dir, 'memory.json'); writeFileSync(memoryPath, '{ this is not json')
  const config = baseConfig({ memoryPath })
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, config)
  const session = makeSession()
  const result = await h.compaction.summarize(smallInput(session), { session })
  return result.provider === 'context-guardian'
    && h.logs.some(line => line.startsWith('warn') && line.includes(`memory at ${memoryPath} is unreadable`))
    && readdirSync(dir).some(name => name.startsWith('memory.json.corrupt'))
    && readLog(config.logPath).some(entry => entry.event === 'memory' && entry.saved === true)
})
check('durable_files_survive_second_compaction', async () => {
  const many = Array.from({ length: 20 }, (_, index) => `file_${String(index).padStart(2, '0')}.py`)
  const compactTwice = async (h, memoryOff) => {
    const first = makeSessionOf(editTurns(many))
    const firstText = (await h.compaction.summarize(wholeInput(first), { session: first })).summary[0].text
    const second = makeSessionOf([checkpointTurn(firstText), ...editTurns(['late.py'])])
    return (await h.compaction.summarize(wholeInput(second), { session: second })).summary[0].text
  }
  const off = makeCtx({ llm: 'throw' })
  engine.apply(off.ctx, baseConfig({ memoryPath: join(tmp, 'chk-durable-off', 'memory.json'), memoryMaxTokens: 0, filesListed: 15 }))
  const withoutMemory = await compactTwice(off, true)
  const filesLine = fileLineOf(withoutMemory)
  if (filesLine === undefined || !filesLine.includes('late.py') || !filesLine.includes('file_19.py')) return false
  if (filesLine.includes('file_00.py') || withoutMemory.includes('[memory --')) return false

  const memoryPath = join(tmp, 'chk-durable-on', 'memory.json')
  const on = makeCtx({ llm: 'throw' })
  engine.apply(on.ctx, baseConfig({ memoryPath, filesListed: 15 }))
  const withMemory = await compactTwice(on, false)
  const memoryFiles = (withMemory.split('\n').find(line => line.startsWith('files:')) ?? '')
  return withMemory.includes('[memory --') && memoryFiles.includes('file_00.py') && memoryFiles.includes('late.py')
})
check('guardian_command_registered', async () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig({ memoryPath: join(tmp, 'chk-guardian', 'memory.json') }))
  const command = h.commands.get('guardian')
  if (command === undefined || command.input !== undefined) return false
  const out = (await command.handler({ rawInput: '', agent: { session: makeSession() } })).text
  return ['context-guardian cg-engine-4', 'window:', 'pressure:', 'last compaction:', 'goal:', 'memory:', 'spans archived:', 'recall this turn:']
    .every(needle => out.includes(needle))
})
check('guardian_reports_last_compaction_kinds', async () => {
  const report = async (h, session) => (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  const ok = makeCtx({ llm: 'ok' }); engine.apply(ok.ctx, baseConfig({ memoryPath: join(tmp, 'chk-kinds-ok', 'memory.json') }))
  const usedLlm = makeSession()
  await ok.compaction.summarize(smallInput(usedLlm), { session: usedLlm })
  const afterLlm = await report(ok, usedLlm)

  const narrow = makeCtx({ llm: 'ok' }); engine.apply(narrow.ctx, baseConfig({ numCtx: 1024, memoryPath: join(tmp, 'chk-kinds-over', 'memory.json') }))
  const overflowed = makeSession()
  const fellBack = await narrow.compaction.summarize(smallInput(overflowed), { session: overflowed })
  const afterOverflow = await report(narrow, overflowed)

  const broken = makeCtx({ llm: 'throw' }); engine.apply(broken.ctx, baseConfig({ memoryPath: join(tmp, 'chk-kinds-det', 'memory.json') }))
  const fellToDeterministic = makeSession()
  await broken.compaction.summarize(smallInput(fellToDeterministic), { session: fellToDeterministic })
  const afterFailure = await report(broken, fellToDeterministic)
  return afterLlm.includes('last compaction: llm (p/m) at ')
    && fellBack.provider === 'context-guardian' && afterOverflow.includes('last compaction: overflow (llm summary cannot fit:')
    && afterFailure.includes('last compaction: deterministic (llm summary failed:')
})
check('guardian_status_tool_opt_in', async () => {
  const off = makeCtx(); engine.apply(off.ctx, baseConfig({ memoryPath: join(tmp, 'chk-opt-off', 'memory.json') }))
  const on = makeCtx(); engine.apply(on.ctx, baseConfig({ tools: ['recall', 'search', 'guardian_status'], memoryPath: join(tmp, 'chk-opt-on', 'memory.json') }))
  if (off.tools.has('guardian_status') || !on.tools.has('guardian_status')) return false
  if (on.tools.get('guardian_status').parameters.properties === undefined) return false
  const session = makeSession()
  const fromTool = await on.tools.get('guardian_status').execute({}, { agent: { session } })
  const fromCommand = (await on.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  return fromTool === fromCommand && fromTool.includes('context-guardian cg-engine-4') && fromTool.includes('spans archived:')
})
check('recall_digest', async () => {
  const memoryPath = join(tmp, 'chk-digest', 'memory.json')
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, baseConfig({ memoryPath }))
  const session = makeSessionOf([
    { role: 'user', content: [T('todo: write the docs for the broker')] },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'e1', name: 'edit', arguments: JSON.stringify({ path: 'docs/broker.md' }) }] },
  ])
  await h.compaction.summarize(wholeInput(session), { session })
  const out = (await h.commands.get('recall').handler({ rawInput: 'DIGEST', agent: { session } })).text
  const command = h.commands.get('recall')
  if (!['DIGEST (context-guardian)', 'goal:', 'memory updated this session:', 'open todos:', 'write the docs', 'files written: docs/broker.md', 'last compaction: deterministic', 'last error: none']
    .every(needle => out.includes(needle))) return false
  if (!command.description.includes('/recall digest') || !command.input.hint.includes('digest')) return false

  // A goal long enough to be cut, carried into the second compaction BY the checkpoint.
  const long = `Línea ${'fix café/naïve '.repeat(60)}`
  if (long.length <= 300) return false
  const goalSession = makeSessionOf([{ role: 'user', content: [T(long)] }, ...editTurns(['a.py'])])
  const firstText = (await h.compaction.summarize(wholeInput(goalSession), { session: goalSession })).summary[0].text
  const carried = makeSessionOf([checkpointTurn(firstText), ...editTurns(['b.py'])])
  await h.compaction.summarize(wholeInput(carried), { session: carried })
  const second = (await h.commands.get('recall').handler({ rawInput: 'digest', agent: { session: carried } })).text
  const status = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session: carried } })).text
  const goalLine = second.split('\n').find(line => line.startsWith('goal: ')) ?? ''
  const shown = goalLine.slice('goal: '.length, goalLine.indexOf(' (from a checkpoint)'))
  return goalLine.endsWith('(from a checkpoint) +0 updates') && shown.length === 300 && shown.endsWith('...')
    && shown.startsWith('Línea fix café/naïve') && shown.includes('naïve')
    && status.includes('goal: pinned from a checkpoint (+0 updates)')
})
check('recall_limit_per_turn', async () => {
  const h = makeCtx(); engine.apply(h.ctx, baseConfig({ recallMaxPerTurn: 2, idleCompactRatio: 0, memoryPath: join(tmp, 'chk-limit', 'memory.json') }))
  const agent = { session: makeSession() }
  const first = await h.tools.get('recall').execute({ type: 'result', id: '101' }, { agent })
  const second = await h.tools.get('recall').execute({ type: 'result', id: '103' }, { agent })
  const third = await h.tools.get('recall').execute({ type: 'result', id: '105' }, { agent })
  const viaCommand = await h.commands.get('recall').handler({ rawInput: 'result 101', agent })
  h.emit('agent/status', { agent, status: 'idle' })
  const afterIdle = await h.tools.get('recall').execute({ type: 'result', id: '101' }, { agent })
  if (!first.includes('UNIQUE-RESULT-0') || first.includes('limit of')) return false
  if (second.includes('limit of') || !third.includes('limit of 2 recalls per turn') || !third.includes('search')) return false
  if (viaCommand.kind !== 'success' || !viaCommand.text.includes('UNIQUE-RESULT-0')) return false
  if (!afterIdle.includes('UNIQUE-RESULT-0') || afterIdle.includes('limit of')) return false

  const unlimited = makeCtx(); engine.apply(unlimited.ctx, baseConfig({ recallMaxPerTurn: 0, memoryPath: join(tmp, 'chk-limit-0', 'memory.json') }))
  const other = { session: makeSession() }
  const texts = []
  for (let index = 0; index < 5; index += 1) texts.push(await unlimited.tools.get('recall').execute({ type: 'result', id: '101' }, { agent: other }))
  return texts.every(text => !text.includes('limit of')) && texts.every(text => text.includes('UNIQUE-RESULT-0'))
})
check('dropped_injected_warns_once_per_kind', async () => {
  const config = baseConfig({ memoryPath: join(tmp, 'chk-dropped', 'memory.json'), logPath: join(tmp, 'chk-dropped.jsonl') })
  const h = makeCtx({ llm: 'throw' }); engine.apply(h.ctx, config)
  const session = makeSessionOf([
    { role: 'user', content: [T('the real request about the broker ' + 'Detail sentence about the broker. '.repeat(12))] },
    { role: 'user', source: { kind: 'agent-instructions' }, content: [T('INSTRUCTION-DUMP ' + 'rule '.repeat(500))] },
    { role: 'user', source: { kind: 'agent-instructions' }, content: [T('SECOND-DUMP ' + 'rule '.repeat(500))] },
    { role: 'user', source: { kind: 'plugin', plugin: 'repeat-tool-reminder' }, content: [T('REMINDER ' + 'x '.repeat(200))] },
    { role: 'assistant', content: [T('working on it now, some prose about the broker. ' + 'Explanation of the change. '.repeat(10))] },
  ])
  const input = wholeInput(session)
  await h.compaction.summarize(input, { session })
  await h.compaction.summarize(input, { session })
  const warns = h.logs.filter(line => line.startsWith('warn') && line.includes('out of the checkpoint'))
  const records = readLog(config.logPath).filter(entry => entry.event === 'dropped-injected')
  const nodes = session.events.map(event => ({ seq: event.seq, message: event.data }))
  const droppedNodes = engine.buildCheckpoint(nodes, engine.resolveEngineOptions({ memoryMaxTokens: 0 }, {}), 0.8).droppedNodes
  return warns.length === 2
    && warns.some(line => line.includes('of source kind "agent-instructions" out of the checkpoint (seqs 1, 2);'))
    && warns.some(line => line.includes('left 1 user-role message(s) of source kind "repeat-tool-reminder" out of the checkpoint (seqs 3);'))
    && warns.every(line => line.includes('remove "') && line.includes('from dropSources.'))
    && records.length === 2 && records.every(entry => entry.seqs.join(',') === '1,2,3' && entry.kinds.join(',') === 'agent-instructions,repeat-tool-reminder')
    && JSON.stringify(droppedNodes) === JSON.stringify([{ seq: 1, kind: 'agent-instructions' }, { seq: 2, kind: 'agent-instructions' }, { seq: 3, kind: 'repeat-tool-reminder' }])
})
check('build_checkpoint_unchanged_without_extras', () => {
  const session = makeSession()
  const { nodes } = engine.mapRegionSeqs(session, regionOf(session, 36))
  const options = engine.resolveEngineOptions({ memoryMaxTokens: 0 }, {})
  const all = engine.sessionNodes(session)
  const plain = engine.buildCheckpoint(nodes, options, 0.8, all)
  const withExtras = engine.buildCheckpoint(nodes, options, 0.8, all, {})
  if (plain.text !== withExtras.text || plain.droppedNodes.length !== 0) return false
  if (!plain.text.includes(RECALL_GUIDE) || !plain.text.includes('context-guardian checkpoint · deterministic')) return false
  // With a block supplied it lands after the goal and before the recall guide.
  const seeded = engine.buildCheckpoint(nodes, options, 0.8, all, { memoryBlock: '[memory -- seeded]' }).text
  const at = seeded.indexOf('[memory -- seeded]')
  return at !== -1 && at < seeded.indexOf(RECALL_GUIDE) && seeded.indexOf('[pinned goal --') < at
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false, err = ''
  try { ok = (await fn()) === true } catch (error) { err = ` (error: ${error.message})` }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${err}`)
  if (ok) passed += 1
}
try { rmSync(tmp, { recursive: true, force: true }) } catch { /* temp dir */ }
console.log(`engine_smoke: ${checks.length} checks, ${passed} passed, ${checks.length - passed} failed`)
process.exit(passed === checks.length ? 0 : 1)
