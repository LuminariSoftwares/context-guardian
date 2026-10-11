// tests/anchors_engine_smoke.mjs -- contract probe for the engine.js anchor check.
// Written FROM THE CONTRACT before the wiring existed. Fake cordis ctx +
// fake compaction service, as in engine_smoke.mjs. No network.
// Prints `anchors_engine_smoke: N checks, N passed, M failed`.
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as engine from '../engine.js'

const T = (text) => ({ type: 'text', text })
const clone = (value) => (value === null ? null : JSON.parse(JSON.stringify(value)))
const CARRIED = '[context-guardian: carried facts'

function makeCtx({ llm = 'ok' } = {}) {
  const listeners = new Map()
  const disposers = []
  const logs = []
  const tools = new Map()
  const commands = new Map()
  class FakeEngine {
    async summarize() {
      if (llm === 'throw') throw new Error('summarization produced no text summary content')
      return { summary: [T('LLM SUMMARY')], provider: 'p', model: 'm', rawOutput: 'LLM SUMMARY', llmStreamCall: true }
    }
    async compactNow() { return { shadowedSeqs: [1] } }
  }
  const compaction = new FakeEngine()
  const services = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name) } },
    commands: { register(def) { commands.set(def.name, def); return () => commands.delete(def.name) } },
    tokenMeter: { measure: (session) => ({ totalTokens: 20000, surfaceTokens: 20000, baseline: { kind: 'usage', usage: { inputTokens: 20000, outputTokens: 10 } }, nodes: session.surface.nodes.map(seq => ({ seq, tokens: 60, heuristicTokens: 60 })) }) },
  }
  const ctx = {
    compaction,
    logger: { info: m => logs.push(`info ${m}`), warn: m => logs.push(`warn ${m}`), error: m => logs.push(`error ${m}`), debug: () => {} },
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); return () => {} },
    effect(fn) { const dispose = fn(); if (typeof dispose === 'function') disposers.push(dispose) },
    inject(deps, cb) { if (deps.every(dep => services[dep] !== undefined)) cb(Object.assign(Object.create(ctx), Object.fromEntries(deps.map(dep => [dep, services[dep]])))) },
  }
  return { ctx, compaction, logs, tools, commands }
}

const makeSessionOf = (messages, id) => {
  const events = messages.map((data, index) => ({ seq: index, type: 'user/message', data }))
  return {
    id, events, surface: { nodes: events.map(event => event.seq), replaceGeneration: 0 },
    deriveEventMessage: (event) => (event?.type === 'user/message' ? clone(event.data) : null),
  }
}

// Region = messages 0..5; messages 6..7 come AFTER the region and still name the file and the helper.
// Message 0 is the pinned goal, deliberately free of anchors: the goal block is part of what the
// model sees, so an anchor named in the goal is kept, not lost.
const MESSAGES = [
  { role: 'user', content: [T('Build the ETL exporter for the reports team.')] },
  { role: 'assistant', content: [T('Starting.')] },
  { role: 'user', content: [T('Please fix `parse_row` in src/etl/loader.py so it keeps UTF-8.\nconstraint: never write to prod.db directly')] },
  { role: 'assistant', content: [T('Looking at src/etl/loader.py now.\ndecision: keep UTF-8 only')] },
  { role: 'user', content: [T('ok')] },
  { role: 'assistant', content: [T('Done with the first pass.')] },
  { role: 'user', content: [T('now run the tests for src/etl/loader.py and check parse_row( again')] },
  { role: 'assistant', content: [T('running')] },
]
let sessionCounter = 0
const newSession = () => makeSessionOf(clone(MESSAGES), `sess-anchor-${sessionCounter += 1}`)
const regionInput = (session) => ({ system: 'sys', tools: [], messages: session.events.slice(0, 6).map(event => clone(event.data)) })

const tmp = mkdtempSync(join(tmpdir(), 'cg-anchor-'))
let configCounter = 0
const config = (extra = {}) => {
  configCounter += 1
  return { spanDir: join(tmp, `spans${configCounter}`), logPath: join(tmp, `log${configCounter}.jsonl`), idleDelayMs: 0, ...extra }
}
const readLog = (path) => {
  try { return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) } catch { return [] }
}
const texts = (result) => (Array.isArray(result?.summary) ? result.summary : []).filter(b => b?.type === 'text').map(b => String(b.text))
const carriedBlock = (result) => texts(result).find(t => t.startsWith(CARRIED))

const checks = []
const check = (name, fn) => checks.push([name, fn])

check('options_default_env_and_unknown', () => {
  const d = engine.resolveEngineOptions({}, {})
  const envOff = engine.resolveEngineOptions({ anchorCheck: 'repair' }, { GUARDIAN_ANCHOR_CHECK: 'off' })
  const report = engine.resolveEngineOptions({ anchorCheck: 'report' }, {})
  const junk = engine.resolveEngineOptions({ anchorCheck: 'banana' }, {})
  return d.anchorCheck === 'repair' && envOff.anchorCheck === 'off' && report.anchorCheck === 'report' && junk.anchorCheck === 'repair'
})

check('llm_summary_gets_carried_block_after_stock_summary', async () => {
  const h = makeCtx({ llm: 'ok' })
  engine.apply(h.ctx, config({ memoryMaxTokens: 0 }))
  const session = newSession()
  const result = await h.compaction.summarize(regionInput(session), { session })
  const all = texts(result)
  const block = carriedBlock(result)
  if (block === undefined) return false
  return all.indexOf(block) > all.indexOf('LLM SUMMARY')
    && block.includes('`src/etl/loader.py`')
    && block.includes('`parse_row`')
    && block.includes('- constraint: never write to prod.db directly')
    && block.includes('- decision: keep UTF-8 only')
})

check('memory_on_keeps_constraints_out_of_carried', async () => {
  const h = makeCtx({ llm: 'ok' })
  engine.apply(h.ctx, config())
  const session = newSession()
  const result = await h.compaction.summarize(regionInput(session), { session })
  const block = carriedBlock(result) ?? ''
  return !block.includes('- constraint: never write to prod.db directly') && !block.includes('- decision: keep UTF-8 only')
})

check('report_mode_records_without_appending', async () => {
  const h = makeCtx({ llm: 'ok' })
  const c = config({ memoryMaxTokens: 0, anchorCheck: 'report' })
  engine.apply(h.ctx, c)
  const session = newSession()
  const result = await h.compaction.summarize(regionInput(session), { session })
  const ev = readLog(c.logPath).find(entry => entry.event === 'anchors')
  return carriedBlock(result) === undefined && ev !== undefined && ev.mode === 'report' && ev.lost >= 2 && ev.constraintsLost >= 2
    && typeof ev.recurring === 'number' && typeof ev.kept === 'number'
})

check('off_mode_does_nothing', async () => {
  const h = makeCtx({ llm: 'ok' })
  const c = config({ memoryMaxTokens: 0, anchorCheck: 'off' })
  engine.apply(h.ctx, c)
  const session = newSession()
  const result = await h.compaction.summarize(regionInput(session), { session })
  return carriedBlock(result) === undefined && !readLog(c.logPath).some(entry => entry.event === 'anchors')
})

check('deterministic_checkpoint_keeps_or_carries_the_anchors', async () => {
  const h = makeCtx({ llm: 'throw' })
  const c = config({ memoryMaxTokens: 0 })
  engine.apply(h.ctx, c)
  const session = newSession()
  const result = await h.compaction.summarize(regionInput(session), { session })
  const text = texts(result).join('\n')
  const ev = readLog(c.logPath).find(entry => entry.event === 'anchors')
  return text.includes('src/etl/loader.py') && text.includes('parse_row') && ev !== undefined
})

check('status_line_before_and_after', async () => {
  const h = makeCtx({ llm: 'ok' })
  engine.apply(h.ctx, config({ memoryMaxTokens: 0 }))
  const session = newSession()
  const before = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  await h.compaction.summarize(regionInput(session), { session })
  const after = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  const b = before.split('\n').find(l => l.startsWith('anchors: '))
  const a = after.split('\n').find(l => l.startsWith('anchors: '))
  return b === 'anchors: no compaction yet' && a !== undefined && a.includes('carried') && a.includes('mode repair')
})

check('status_line_off', async () => {
  const h = makeCtx({ llm: 'ok' })
  engine.apply(h.ctx, config({ anchorCheck: 'off' }))
  const session = newSession()
  const text = (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  return text.split('\n').includes('anchors: off')
})

check('no_session_never_throws', async () => {
  const h = makeCtx({ llm: 'ok' })
  engine.apply(h.ctx, config({ memoryMaxTokens: 0 }))
  const result = await h.compaction.summarize(regionInput(newSession()), { session: undefined })
  return texts(result).includes('LLM SUMMARY')
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false, err = ''
  try { ok = (await fn()) === true } catch (error) { err = ` (error: ${error.message})` }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${err}`)
  if (ok) passed += 1
}
try { rmSync(tmp, { recursive: true, force: true }) } catch { /* temp dir */ }
console.log(`anchors_engine_smoke: ${checks.length} checks, ${passed} passed, ${checks.length - passed} failed`)
process.exit(passed === checks.length && checks.length > 0 ? 0 : 1)
