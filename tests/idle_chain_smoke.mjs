// Contract smoke for the idle trigger routing through the append-only chain (P36 S1):
// with appendOnly on, an idle pass must reach the chained compactRegion override instead
// of compactNow, and the manual/appendOnly-off path must still retire through compactNow.
// usage: node tests/idle_chain_smoke.mjs      (exit 0 iff all pass)
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as engine from '../engine.js'

const T = (text) => ({ type: 'text', text })
const clone = (value) => (value === null ? null : JSON.parse(JSON.stringify(value)))
const checkpointTurn = (text) => ({ role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T(text)] })
const userTurn = (text) => ({ role: 'user', content: [T(text)] })
const assistantTurn = (text) => ({ role: 'assistant', content: [T(text)] })
const CHECKPOINT_BODY = '[context-guardian checkpoint · deterministic · 1 nodes]\nRECALL: seq numbers are pointers.'

function makeSessionOf(messages, id = 'sess-idle-chain') {
  const events = messages.map((data, index) => ({ seq: index, type: 'user/message', data }))
  return {
    id, events, surface: { nodes: events.map(event => event.seq), replaceGeneration: 0 },
    deriveEventMessage: (event) => (event?.type === 'user/message' ? clone(event.data) : null),
  }
}
/** surface = [checkpoint, user, assistant, user]: one checkpoint head, then work. */
const chainSession = () => makeSessionOf([
  checkpointTurn(CHECKPOINT_BODY), userTurn('please look at the broker'), assistantTurn('looking now'), userTurn('thanks'),
])

/** `compactNowThrows`: the idle-off path must never reach compactNow when appendOnly is on. */
function makeCtx({ pressure = 0.6, compactNowThrows = false } = {}) {
  const listeners = new Map()
  const disposers = []
  const logs = []
  const tools = new Map()
  const commands = new Map()
  const calls = { compactNow: 0, region: [] }
  class FakeEngine {
    async summarize() { return { summary: [T('LLM SUMMARY')], provider: 'p', model: 'm', rawOutput: 'LLM SUMMARY', llmStreamCall: true } }
    async compactNow() {
      calls.compactNow += 1
      if (compactNowThrows) throw new Error('compactNow must not be called on this path')
      return { shadowedSeqs: [1] }
    }
    /** The stock engine reaches summarize() exactly this way: the region's own messages. */
    async compactRegion(start, end, agent, signal) {
      calls.region.push({ start, end })
      const session = agent?.session
      const surface = session.surface.nodes
      const messages = surface.slice(surface.indexOf(start), surface.indexOf(end) + 1)
        .map(seq => session.deriveEventMessage(session.events[seq]))
      const result = await this.summarize({ system: 's', tools: [], messages }, agent, signal)
      return { start, end, summary: result.summary }
    }
  }
  const compaction = new FakeEngine()
  const ctx = {
    compaction,
    logger: { info: m => logs.push(`info ${m}`), warn: m => logs.push(`warn ${m}`), error: m => logs.push(`error ${m}`), debug: () => {} },
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); return () => {} },
    effect(fn) { const dispose = fn(); if (typeof dispose === 'function') disposers.push(dispose) },
    inject(deps, cb) { if (deps.every(dep => services[dep] !== undefined)) cb(Object.assign(Object.create(ctx), Object.fromEntries(deps.map(dep => [dep, services[dep]])))) },
  }
  const services = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name) } },
    commands: { register(def) { commands.set(def.name, def); return () => commands.delete(def.name) } },
    tokenMeter: {
      measure: (session) => ({
        totalTokens: Math.round(pressure * 32768), surfaceTokens: Math.round(pressure * 32768),
        baseline: { kind: 'usage', usage: { inputTokens: 20000, outputTokens: 10, cacheReadTokens: 15000 } },
        nodes: session.surface.nodes.map(seq => ({ seq, tokens: 600, heuristicTokens: 600 })),
      }),
    },
  }
  const emit = (name, ...args) => { for (const fn of listeners.get(name) ?? []) fn(...args) }
  const dispose = () => { while (disposers.length > 0) disposers.pop()() }
  return { ctx, compaction, logs, tools, commands, calls, emit, dispose }
}

const tmp = mkdtempSync(join(tmpdir(), 'cg-idle-chain-'))
let caseNo = 0
const baseConfig = (extra = {}) => {
  caseNo += 1
  return { spanDir: join(tmp, `spans-${caseNo}`), logPath: join(tmp, `log-${caseNo}.jsonl`), idleDelayMs: 0, idleCompactRatio: 0.45, ...extra }
}
const readLog = (path) => {
  try { return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) } catch { return [] }
}
const chainRecords = (path) => readLog(path).filter(entry => entry.event === 'chain')
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))

const checks = []
const check = (name, fn) => checks.push([name, fn])

check('idle_on_appendOnly_on_routes_through_compactRegion_not_compactNow', async () => {
  const h = makeCtx({ pressure: 0.6, compactNowThrows: true })
  const config = baseConfig({ appendOnly: true })
  engine.apply(h.ctx, config)
  const session = chainSession()
  const head = session.surface.nodes[0]
  const tail = session.surface.nodes.at(-1)
  h.emit('agent/status', { agent: { session }, status: 'idle' })
  await sleep(60)
  const appended = chainRecords(config.logPath).find(entry => entry.action === 'append' || entry.action === 'roll-up')
  return h.calls.region.length === 1 && h.calls.compactNow === 0
    && appended !== undefined && appended.originalStart === head && appended.end === tail
})

check('idle_on_appendOnly_off_routes_through_compactNow', async () => {
  const h = makeCtx({ pressure: 0.6 })
  const config = baseConfig({ appendOnly: false })
  engine.apply(h.ctx, config)
  const session = chainSession()
  h.emit('agent/status', { agent: { session }, status: 'idle' })
  await sleep(60)
  return h.calls.compactNow === 1 && h.calls.region.length === 0
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false
  try {
    ok = !!(await fn())
    console.log((ok ? '  ok   ' : '  FAIL ') + name)
  } catch (error) {
    console.log(`  FAIL ${name}  -- raised ${error && error.name}: ${error && error.message}`)
  }
  if (ok) passed += 1
}
const total = checks.length
const failed = total - passed
console.log(`idle_chain_smoke: ${total} checks, ${passed} passed, ${failed} failed`)
process.exit(total > 0 && failed === 0 ? 0 : 1)
