// Contract smoke for engine.js: a fake cordis ctx + fake compaction-basic service.
// usage: node tests/engine_smoke.mjs      (exit 0 iff "0 failed")
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
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

function makeCtx({ llm = 'throw', pressure = 0.85, withMeter = true } = {}) {
  const listeners = new Map()
  const disposers = []
  const logs = []
  const tools = new Map()
  const commands = new Map()
  const calls = { llm: 0, compactNow: 0 }
  class FakeEngine {
    async summarize() {
      calls.llm += 1
      if (llm === 'throw') throw new Error(LIVE_ERROR)
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
    services.tokenMeter = { measure: (session) => ({ totalTokens: Math.round(pressure * 32768), surfaceTokens: Math.round(pressure * 32768), baseline: { kind: 'usage', usage: { inputTokens: 20000, outputTokens: 10, cacheReadTokens: 15000 } }, nodes: session.surface.nodes.map(seq => ({ seq, tokens: 600, heuristicTokens: 600 })) }) }
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
  return o.mode === 'deterministic' && o.numCtx === 65536 && o.idleCompactRatio === 0.45 && p.maxRecallTokens === 2048 && JSON.stringify(p.tools) === '["recall"]'
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
  const high = makeCtx({ pressure: 0.5 }); engine.apply(high.ctx, baseConfig())
  const low = makeCtx({ pressure: 0.2 }); engine.apply(low.ctx, baseConfig())
  const off = makeCtx({ pressure: 0.9 }); engine.apply(off.ctx, baseConfig({ idleCompactRatio: 0 }))
  const session = makeSession()
  for (const h of [high, low, off]) h.emit('agent/status', { agent: { session }, status: 'idle' })
  await sleep(30)
  return high.calls.compactNow === 1 && low.calls.compactNow === 0 && off.calls.compactNow === 0
})
check('idle_trigger_cancelled_when_agent_becomes_busy', async () => {
  const h = makeCtx({ pressure: 0.6 }); engine.apply(h.ctx, baseConfig({ idleDelayMs: 40 }))
  const agent = { session: makeSession() }
  h.emit('agent/status', { agent, status: 'idle' })
  h.emit('agent/status', { agent, status: 'running' })
  await sleep(90)
  return h.calls.compactNow === 0
})
check('idle_trigger_labels_the_snapshot', async () => {
  const h = makeCtx({ pressure: 0.6 }); const config = baseConfig({ spanDir: join(tmp, 'spans-c') }); engine.apply(h.ctx, config)
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
