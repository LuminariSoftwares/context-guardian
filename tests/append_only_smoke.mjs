// Contract smoke for append-only checkpoint chains (engine.js): the compactRegion
// override, the pure plan, the goal delta and the memory delta.
// usage: node tests/append_only_smoke.mjs      (exit 0 iff all pass and N >= 16)
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { emptyMemory, mergeMemory, saveMemory } from '../cg_memory.js'
import { GOAL_CLOSE, GOAL_OPEN, extractGoal, planChainStart, renderGoal, renderGoalDelta, resolveEngineOptions } from '../engine.js'
import * as engine from '../engine.js'

const LIVE_ERROR = 'summarization produced no text summary content'
const T = (text) => ({ type: 'text', text })
const clone = (value) => (value === null ? null : JSON.parse(JSON.stringify(value)))
const summaryText = (result) => (result?.summary ?? []).map(block => block.text).join('\n')

// ── fake DSH sessions ───────────────────────────────────────────────────────

const checkpointTurn = (text) => ({ role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T(text)] })
const userTurn = (text) => ({ role: 'user', content: [T(text)] })
const assistantTurn = (text) => ({ role: 'assistant', content: [T(text)] })
const CHECKPOINT_BODY = '[context-guardian checkpoint · deterministic · 1 nodes]\nRECALL: seq numbers are pointers.'

function makeSessionOf(messages, id = 'sess-append') {
  const events = messages.map((data, index) => ({ seq: index, type: 'user/message', data }))
  return {
    id, events, surface: { nodes: events.map(event => event.seq), replaceGeneration: 0 },
    deriveEventMessage: (event) => (event?.type === 'user/message' ? clone(event.data) : null),
  }
}
/** surface = [checkpoint, user, assistant, user]: one checkpoint head, then work. */
const chainSession = () => makeSessionOf([
  checkpointTurn(CHECKPOINT_BODY), userTurn('please look at the broker'),
  assistantTurn('looking now'), userTurn('thanks'),
])
const headlessSession = () => makeSessionOf([userTurn('no checkpoint here yet'), assistantTurn('ok'), userTurn('more')])
const checkpointNode = (seq, text) => ({ seq, message: checkpointTurn(text) })
const userNode = (seq, text) => ({ seq, message: userTurn(text) })
const assistantNode = (seq, text) => ({ seq, message: assistantTurn(text) })

// ── fake cordis ctx + fake compaction-basic service ─────────────────────────

/**
 * `onShift(start, end, session)` is consulted when compactRegion is called with
 * a start that is NOT the first surface node: return an Error to throw it (the
 * real engine's balanced-boundary validation), or null to let it through.
 */
function makeCtx({ llm = 'throw', pressure = 0.5, withMeter = true, onShift = null } = {}) {
  const listeners = new Map()
  const disposers = []
  const logs = []
  const tools = new Map()
  const commands = new Map()
  const calls = { llm: 0, compactNow: 0, region: [] }
  class FakeEngine {
    async summarize() {
      calls.llm += 1
      if (llm === 'throw') throw new Error(LIVE_ERROR)
      return { summary: [T('LLM SUMMARY')], provider: 'p', model: 'm', rawOutput: 'LLM SUMMARY', llmStreamCall: true }
    }
    async compactNow() { calls.compactNow += 1; return { shadowedSeqs: [1] } }
    /** The stock engine reaches summarize() exactly this way: the region's own messages. */
    async compactRegion(start, end, agent, signal) {
      calls.region.push({ start, end })
      const session = agent?.session
      if (typeof onShift === 'function' && start !== session.surface.nodes[0]) {
        const failure = onShift(start, end, session)
        if (failure !== null && failure !== undefined) throw failure
      }
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
    // cordis: a service the plugin did not declare is not readable from its ctx.
    inject(deps, cb) { if (deps.every(dep => services[dep] !== undefined)) cb(Object.assign(Object.create(ctx), Object.fromEntries(deps.map(dep => [dep, services[dep]])))) },
  }
  const services = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name) } },
    commands: { register(def) { commands.set(def.name, def); return () => commands.delete(def.name) } },
  }
  if (withMeter) {
    const total = () => Math.round(pressure * 32768)
    services.tokenMeter = { measure: (session) => ({ totalTokens: total(), surfaceTokens: total(), baseline: { kind: 'usage', usage: { inputTokens: 20000, outputTokens: 10, cacheReadTokens: 15000 } }, nodes: session.surface.nodes.map(seq => ({ seq, tokens: 600, heuristicTokens: 600 })) }) }
  }
  const emit = (name, ...args) => { for (const fn of listeners.get(name) ?? []) fn(...args) }
  const dispose = () => { while (disposers.length > 0) disposers.pop()() }
  return { ctx, compaction, logs, tools, commands, calls, emit, dispose }
}

const tmp = mkdtempSync(join(tmpdir(), 'cg-append-'))
let caseNo = 0
/** A fresh span dir + log per check, so one check's chain records cannot leak into another's. */
const baseConfig = (extra = {}) => {
  caseNo += 1
  return { spanDir: join(tmp, `spans-${caseNo}`), logPath: join(tmp, `log-${caseNo}.jsonl`), idleCompactRatio: 0, idleDelayMs: 0, ...extra }
}
const readLog = (path) => {
  try { return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) } catch { return [] }
}
const chainRecords = (path) => readLog(path).filter(entry => entry.event === 'chain')
const seedMemory = (path, item) => saveMemory(path, mergeMemory(emptyMemory(), [{ first: '2026-01-01T00:00:00.000Z', last: '2026-01-01T00:00:00.000Z', ...item }]))

const checks = []
const check = (name, fn) => checks.push([name, fn])

check('default_is_off', async () => {
  if (resolveEngineOptions({}, {}).appendOnly !== false) return false
  const h = makeCtx(); engine.apply(h.ctx, baseConfig())
  // Not own-defined: the stock prototype method is still what runs.
  if (Object.hasOwn(h.compaction, 'compactRegion')) return false
  const session = chainSession()
  const result = await h.compaction.compactRegion(0, 2, { session })
  return h.calls.region.length === 1 && h.calls.region[0].start === 0 && h.calls.region[0].end === 2 && result.start === 0
})

check('env_turns_it_on', () => {
  const defaults = resolveEngineOptions({}, {})
  if (defaults.chainMaxTokens !== 0 || defaults.chainMaxCheckpoints !== 4) return false
  if (resolveEngineOptions({}, { GUARDIAN_APPEND_ONLY: '1' }).appendOnly !== true) return false
  if (resolveEngineOptions({}, { GUARDIAN_APPEND_ONLY: 'TRUE' }).appendOnly !== true) return false
  if (resolveEngineOptions({ appendOnly: true }, { GUARDIAN_APPEND_ONLY: '0' }).appendOnly !== false) return false
  if (resolveEngineOptions({ appendOnly: true }, { GUARDIAN_APPEND_ONLY: 'False' }).appendOnly !== false) return false
  // Anything unrecognised falls to the row, which is on only for a real `true`.
  if (resolveEngineOptions({ appendOnly: true }, {}).appendOnly !== true) return false
  if (resolveEngineOptions({ appendOnly: 'true' }, {}).appendOnly !== false) return false
  if (resolveEngineOptions({}, { GUARDIAN_APPEND_ONLY: 'yes' }).appendOnly !== false) return false
  const tuned = resolveEngineOptions({ chainMaxTokens: 900, chainMaxCheckpoints: 7 }, {})
  return tuned.chainMaxTokens === 900 && tuned.chainMaxCheckpoints === 7
})

check('appends_after_checkpoint_head', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = chainSession()
  const result = await h.compaction.compactRegion(0, 2, { session })
  const appended = chainRecords(config.logPath).find(entry => entry.action === 'append')
  if (h.calls.region.length !== 1 || h.calls.region[0].start !== 1 || h.calls.region[0].end !== 2) return false
  if (result.start !== 1 || result.summary.length === 0) return false
  return appended !== undefined && appended.start === 1 && appended.originalStart === 0
    && appended.head === 1 && appended.chainTokens > 0
})

check('no_head_passes_through', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = headlessSession()
  await h.compaction.compactRegion(0, 2, { session })
  return h.calls.region.length === 1 && h.calls.region[0].start === 0 && chainRecords(config.logPath).length === 0
})

check('not_head_anchored_passes_through', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = chainSession()
  await h.compaction.compactRegion(1, 3, { session })
  const entries = chainRecords(config.logPath)
  return h.calls.region.length === 1 && h.calls.region[0].start === 1
    && entries.length === 0 && entries.every(entry => entry.action !== 'append')
})

check('nothing_after_head_rolls_up', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = makeSessionOf([checkpointTurn(CHECKPOINT_BODY), checkpointTurn('second checkpoint body'), checkpointTurn('third checkpoint body')])
  await h.compaction.compactRegion(0, 1, { session })
  const entry = chainRecords(config.logPath)[0]
  return h.calls.region.length === 1 && h.calls.region[0].start === 0
    && entry !== undefined && entry.reason === 'nothing after the head' && entry.action === 'full' && entry.head === 2
})

check('chain_budget_rolls_up', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true, chainMaxTokens: 10 }); engine.apply(h.ctx, config)
  const session = makeSessionOf([checkpointTurn(CHECKPOINT_BODY + '\n' + 'filler line. '.repeat(200)), userTurn('carry on'), assistantTurn('ok')])
  await h.compaction.compactRegion(0, 2, { session })
  const entry = chainRecords(config.logPath)[0]
  return h.calls.region.length === 1 && h.calls.region[0].start === 0
    && entry !== undefined && entry.action === 'roll-up' && entry.reason === 'roll-up: chain over budget' && entry.head === 1
})

check('max_checkpoints_rolls_up', async () => {
  const h = makeCtx(); const config = baseConfig({ appendOnly: true, chainMaxCheckpoints: 2 }); engine.apply(h.ctx, config)
  const session = makeSessionOf([checkpointTurn(CHECKPOINT_BODY), checkpointTurn('second checkpoint body'), userTurn('carry on'), assistantTurn('ok')])
  await h.compaction.compactRegion(0, 3, { session })
  const entry = chainRecords(config.logPath)[0]
  return h.calls.region.length === 1 && h.calls.region[0].start === 0
    && entry !== undefined && entry.action === 'roll-up' && entry.reason === 'roll-up: chain at max checkpoints' && entry.head === 2
})

check('emergency_pressure_rolls_up', async () => {
  const h = makeCtx({ pressure: 0.95 }); const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = chainSession()
  await h.compaction.compactRegion(0, 2, { session })
  const entry = chainRecords(config.logPath)[0]
  return h.calls.region.length === 1 && h.calls.region[0].start === 0
    && entry !== undefined && entry.action === 'roll-up' && entry.reason === 'roll-up: emergency pressure'
})

check('validation_error_falls_back', async () => {
  const h = makeCtx({
    onShift: (start) => new Error(`compactRegion: start seq ${start} is not a balanced boundary (would split a tool call)`),
  })
  const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = chainSession()
  const result = await h.compaction.compactRegion(0, 2, { session })
  const entries = chainRecords(config.logPath)
  const fallback = entries.find(entry => entry.action === 'fallback')
  if (h.calls.region.length !== 2 || h.calls.region[0].start !== 1 || h.calls.region[1].start !== 0) return false
  if (result === undefined || result.start !== 0 || summaryText(result).length === 0) return false
  return fallback !== undefined && fallback.reason.includes('is not a balanced boundary')
    && !entries.some(entry => entry.action === 'append')
})

check('other_errors_rethrow', async () => {
  const h = makeCtx({ onShift: () => new Error('boom') })
  const config = baseConfig({ appendOnly: true }); engine.apply(h.ctx, config)
  const session = chainSession()
  let thrown = null
  try { await h.compaction.compactRegion(0, 2, { session }) } catch (error) { thrown = error }
  return thrown !== null && thrown.message === 'boom'
    && h.calls.region.length === 1 && h.calls.region[0].start === 1
    && chainRecords(config.logPath).every(entry => entry.action !== 'fallback')
})

check('goal_not_duplicated_in_appended_checkpoint', async () => {
  const goal = 'ship it'
  const withGoal = `${CHECKPOINT_BODY}\n${GOAL_OPEN}\n${goal}\n${GOAL_CLOSE}`
  const session = makeSessionOf([
    checkpointTurn(withGoal), userTurn('Goal: also test it'), assistantTurn('working on the tail of this session now'),
  ])
  const on = makeCtx(); engine.apply(on.ctx, baseConfig({ appendOnly: true, memoryMaxTokens: 0 }))
  const appended = summaryText(await on.compaction.compactRegion(0, 2, { session }))
  if (on.calls.region[0]?.start !== 1) return false
  if (!appended.includes(`${GOAL_OPEN} update seq 1`) || !appended.includes('also test it')) return false
  if (appended.includes(`${GOAL_OPEN}\n${goal}`)) return false

  const off = makeCtx(); engine.apply(off.ctx, baseConfig({ memoryMaxTokens: 0 }))
  const whole = summaryText(await off.compaction.compactRegion(0, 2, { session }))
  return off.calls.region[0]?.start === 0 && whole.includes(`${GOAL_OPEN}\n${goal}\n${GOAL_CLOSE}`) && whole.includes('also test it')
})

check('memory_delta_only_in_appended_checkpoint', async () => {
  const memoryPath = join(tmp, 'delta-memory.json')
  seedMemory(memoryPath, { cat: 'decisions', text: 'old decision alpha', seq: 0, session: 'sess-append', count: 1, done: false })
  const h = makeCtx(); const config = baseConfig({ appendOnly: true, memoryPath }); engine.apply(h.ctx, config)
  const session = makeSessionOf([
    checkpointTurn(CHECKPOINT_BODY), userTurn('decision: new decision beta'), assistantTurn('on it'),
  ])
  const appended = summaryText(await h.compaction.compactRegion(0, 2, { session }))
  if (h.calls.region[0]?.start !== 1) return false
  if (!appended.includes('[memory --') || !appended.includes('new decision beta')) return false
  if (appended.includes('old decision alpha')) return false
  // The durable memory itself is still updated in full; only the RENDER is a delta.
  const stored = readFileSync(memoryPath, 'utf8')
  return stored.includes('old decision alpha') && stored.includes('new decision beta')
})

check('plan_chain_start_is_pure_and_safe', () => {
  const opts = { chainMaxTokens: 1000, chainMaxCheckpoints: 4, pressure: 0.5 }
  const none = planChainStart(null, 1, 2, opts)
  if (none.start !== 1 || typeof none.reason !== 'string' || !Array.isArray(none.headSeqs) || none.chainTokens !== 0) return false
  if (planChainStart(undefined, 4, 4, opts).start !== 4) return false
  const broken = { surface: { nodes: [0, 1, 2] }, events: [{}, {}, {}], deriveEventMessage() { throw new Error('derive blew up') } }
  const failed = planChainStart(broken, 0, 2, opts)
  return failed.start === 0 && failed.reason.startsWith('error:') && failed.reason.includes('derive blew up')
    && Array.isArray(failed.headSeqs) && failed.chainTokens === 0
})

check('render_goal_delta_rules', () => {
  const request = [userNode(1, 'the first request'), assistantNode(2, 'ok')]
  // No goal in the head: nothing to point at, so the full block as today.
  if (renderGoalDelta([], request) !== renderGoal(extractGoal(request))) return false
  const head = [checkpointNode(3, `${CHECKPOINT_BODY}\n${GOAL_OPEN}\nship it\n${GOAL_CLOSE}`)]
  // The head already carries the goal and nothing is new.
  if (renderGoalDelta(head, head) !== '') return false
  const fresh = renderGoalDelta(head, [...head, userNode(4, 'Goal: also test it')])
  if (!fresh.includes('[pinned goal: unchanged, see the first checkpoint above; new \'goal:\' updates follow]')) return false
  if (!fresh.includes(`${GOAL_OPEN} update seq 4\nGoal: also test it\n${GOAL_CLOSE}`)) return false
  return !fresh.includes(`${GOAL_OPEN}\nship it`)
})

check('guardian_status_shows_append_only', async () => {
  const off = makeCtx(); engine.apply(off.ctx, baseConfig())
  const on = makeCtx(); engine.apply(on.ctx, baseConfig({ appendOnly: true }))
  const budgeted = makeCtx(); engine.apply(budgeted.ctx, baseConfig({ appendOnly: true, chainMaxCheckpoints: 6, chainMaxTokens: 800 }))
  const session = chainSession()
  const report = async (h) => (await h.commands.get('guardian').handler({ rawInput: '', agent: { session } })).text
  const offText = await report(off)
  const onText = await report(on)
  const budgetedText = await report(budgeted)
  if (!offText.includes('append-only: off') || onText.includes('append-only: off')) return false
  if (!onText.includes('append-only: on (chain max 4 checkpoints, auto tokens)')) return false
  if (!budgetedText.includes('append-only: on (chain max 6 checkpoints, 800 tokens)')) return false
  // The line belongs right after the memory line.
  return offText.indexOf('memory:') < offText.indexOf('append-only: off')
    && offText.indexOf('append-only: off') < offText.indexOf('spans archived:')
})

check('llm_branch_appends_the_same_delta', async () => {
  const goal = 'ship it'
  const session = makeSessionOf([
    checkpointTurn(`${CHECKPOINT_BODY}\n${GOAL_OPEN}\n${goal}\n${GOAL_CLOSE}`),
    userTurn('Goal: also test it'), assistantTurn('working on the tail of this session now'),
  ])
  const h = makeCtx({ llm: 'ok' }); engine.apply(h.ctx, baseConfig({ appendOnly: true, memoryMaxTokens: 0 }))
  const result = await h.compaction.compactRegion(0, 2, { session })
  const texts = result.summary.map(block => block.text)
  if (h.calls.region[0]?.start !== 1) return false
  // The stock LLM summary ran and is still the last block; the goal delta is prepended to it.
  if (h.calls.llm !== 1 || texts[texts.length - 1] !== 'LLM SUMMARY') return false
  if (!texts[0].includes(`${GOAL_OPEN} update seq 1`) || !texts[0].includes('also test it')) return false
  return !texts.some(text => text.includes(`${GOAL_OPEN}\n${goal}`))
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false, err = ''
  try { ok = (await fn()) === true } catch (error) { err = ` (error: ${error.message})` }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${err}`)
  if (ok) passed += 1
}
try { rmSync(tmp, { recursive: true, force: true }) } catch { /* temp dir */ }
console.log(`append_only_smoke: ${checks.length} checks, ${passed} passed, ${checks.length - passed} failed`)
process.exit(passed === checks.length && checks.length >= 16 ? 0 : 1)
