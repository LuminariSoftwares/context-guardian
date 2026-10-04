// P44 A4 seam (2026-10-03, written before the engine.js splice -- red first): with postAnswerCheck on, a final answer that
// states an OLD value for a pinned subject is steered exactly once per turn; current answers and the default (off) are silent.
// usage: node tests/autorecall_steer_smoke.mjs   (exit 0 iff "0 failed")
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as engine from '../engine.js'

const T = (text) => ({ type: 'text', text })
function makeCtx() {
  const listeners = new Map()
  const ctx = {
    compaction: { async summarize() { throw new Error('no llm') }, async compactNow() { return {} } },
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); return () => {} },
    effect(fn) { fn() },
    inject() {},
  }
  return { ctx, hooks: (name) => listeners.get(name) ?? [] }
}
const K = 'Important, keep this for later in the conversation'
function makeAgent(finalText) {
  const events = [
    { seq: 0, type: 'user/message', data: { role: 'user', content: [T(`${K}: the release codename for project Heron is MAPLE-123; and the release codename for project Kite is RIVER-456.`)] } },
    { seq: 1, type: 'user/message', data: { role: 'assistant', content: [T('Noted.')] } },
    { seq: 2, type: 'user/message', data: { role: 'user', content: [T(`${K}: the release codename for project Heron is now CEDAR-789.`)] } },
    { seq: 3, type: 'user/message', data: { role: 'assistant', content: [T(finalText)] } },
  ]
  const steered = []
  const session = { id: 's-steer', events, surface: { nodes: [0, 1, 2, 3], replaceGeneration: 0 }, deriveEventMessage: (e) => (e?.type === 'user/message' ? e.data : null) }
  return { agent: { session, steer: (m) => steered.push(m) }, steered }
}
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const tmp = mkdtempSync(join(tmpdir(), 'cg-steer-'))
const boot = (cfg) => { const h = makeCtx(); engine.apply(h.ctx, Object.assign({ spanDir: join(tmp, 'spans'), logPath: join(tmp, 'log.jsonl'), memoryPath: join(tmp, 'm.json'), idleDelayMs: 0 }, cfg)); return h.hooks('agent/turn-stopping') }
const fire = async (hooks, agent, turn) => { for (const fn of hooks) await fn({ agent, turn, signal: new AbortController().signal }) }
try {
  delete process.env.GUARDIAN_POST_ANSWER_CHECK
  const off = makeAgent('{"Heron": "MAPLE-123", "Kite": "RIVER-456"}')
  await fire(boot({}), off.agent, 1)
  check('default_off_never_steers', off.steered.length === 0)
  const hooks = boot({ postAnswerCheck: true })
  const stale = makeAgent('{"Heron": "MAPLE-123", "Kite": "RIVER-456"}')
  await fire(hooks, stale.agent, 7)
  const msg = stale.steered[0]
  const text = msg?.content?.[0]?.text ?? ''
  check('stale_answer_steered_once', stale.steered.length === 1, String(stale.steered.length))
  check('steer_names_old_and_current', text.startsWith('[context-guardian check]') && text.includes('MAPLE-123') && text.includes('CEDAR-789'), text)
  check('steer_message_shape', msg?.role === 'user' && msg?.source?.kind === 'plugin' && msg?.source?.plugin === 'context-guardian' && typeof msg?.id === 'string')
  await fire(hooks, stale.agent, 7)
  check('never_twice_in_one_turn', stale.steered.length === 1)
  const good = makeAgent('{"Heron": "CEDAR-789", "Kite": "RIVER-456"}')
  await fire(hooks, good.agent, 8)
  check('current_answer_silent', good.steered.length === 0)
  process.env.GUARDIAN_POST_ANSWER_CHECK = '1'
  const envOn = makeAgent('Heron is MAPLE-123')
  await fire(boot({}), envOn.agent, 2)
  check('env_turns_it_on', envOn.steered.length === 1)
  delete process.env.GUARDIAN_POST_ANSWER_CHECK
  let threw = false
  try { await fire(hooks, { session: { events: null }, steer() { throw new Error('x') } }, 1) } catch { threw = true }
  check('broken_session_never_throws', !threw)
} finally { rmSync(tmp, { recursive: true, force: true }) }
const p = res.filter(Boolean).length
console.log(`autorecall_steer_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
