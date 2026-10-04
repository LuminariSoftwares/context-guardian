// P44 A3 seam (2026-10-03, written before the engine.js splice -- red first): with staleRecall on, agent/pre-step
// appends ONE recall message for a doc line that lives only in compacted turns; off by default; never breaks a step.
// usage: node tests/autorecall_engine_smoke.mjs   (exit 0 iff "0 failed")
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
const doc1 = Array.from({ length: 100 }, (_, i) => `L${String(i + 1).padStart(3, '0')} line ${i + 1}; code K${1000 + i}.`).join('\n')
function makeSession() {
  const events = [
    { seq: 0, type: 'user/message', data: { role: 'user', content: [T('Reading task 1. Below is lines 1-100 of the read-only file doc01.txt.\n' + doc1)] } },
    { seq: 1, type: 'user/message', data: { role: 'assistant', content: [T('It is a list.')] } },
    { seq: 2, type: 'user/message', data: { role: 'user', content: [T('[checkpoint] reading task 1 done')], source: { kind: 'plugin', plugin: 'compact' } } },
  ]
  return { id: 's-ar', events, surface: { nodes: [2], replaceGeneration: 1 }, deriveEventMessage: (e) => (e?.type === 'user/message' ? e.data : null) }
}
const ask = { id: 'm9', role: 'user', content: [T('Final check, part 2: {"doc01 L017": ?}')], source: { kind: 'user' } }
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const tmp = mkdtempSync(join(tmpdir(), 'cg-ar-'))
const run = async (cfg) => {
  const h = makeCtx()
  engine.apply(h.ctx, Object.assign({ spanDir: join(tmp, 'spans'), logPath: join(tmp, 'log.jsonl'), memoryPath: join(tmp, 'm.json'), idleDelayMs: 0 }, cfg))
  const hooks = h.hooks('agent/pre-step')
  const payload = { agent: { session: makeSession() }, messages: [ask], turn: 3, step: 1, signal: new AbortController().signal }
  let decision = { kind: 'enter', messages: [ask] }
  for (const fn of hooks) { const prev = decision; decision = await fn(payload, async () => prev) }
  return { hooks, decision }
}
try {
  delete process.env.GUARDIAN_STALE_RECALL
  // 0.1.0-alpha.9 (2026-10-04): ON by default (P44 bench); `staleRecall: false` is the opt-out.
  const dflt = await run({})
  check('default_is_on_and_appends_one_recall', dflt.decision.messages.length === 2)
  const off = await run({ staleRecall: false })
  check('explicit_false_registers_nothing_or_passes_through', off.decision.messages.length === 1)
  const on = await run({ staleRecall: true })
  check('on_registers_a_pre_step_hook', on.hooks.length === 1, String(on.hooks.length))
  const msgs = on.decision.messages
  const extra = msgs[1]
  check('one_recall_message_appended_after_the_ask', msgs.length === 2 && msgs[0] === ask && extra?.role === 'user')
  const text = extra?.content?.[0]?.text ?? ''
  check('recall_carries_the_compacted_line', text.startsWith('[context-guardian recall]') && text.includes('L017 line 17; code K1016.'), text.slice(0, 200))
  check('recall_message_is_plugin_sourced_with_id', extra?.source?.kind === 'plugin' && extra?.source?.plugin === 'context-guardian' && extra?.source?.form === 'recall' && typeof extra?.id === 'string' && extra.id.length > 0)
  process.env.GUARDIAN_STALE_RECALL = '1'
  const env = await run({})
  check('env_turns_it_on', env.decision.messages.length === 2)
  delete process.env.GUARDIAN_STALE_RECALL
  const h = makeCtx()
  engine.apply(h.ctx, { spanDir: join(tmp, 'spans'), logPath: join(tmp, 'log.jsonl'), memoryPath: join(tmp, 'm.json'), staleRecall: true })
  const fn = h.hooks('agent/pre-step')[0]
  const rejected = fn ? await fn({ agent: { session: makeSession() } }, async () => ({ kind: 'reject' })) : null
  const broken = fn ? await fn({ agent: { session: { events: null } } }, async () => ({ kind: 'enter', messages: [ask] })) : null
  check('reject_and_broken_session_pass_through', rejected?.kind === 'reject' && broken?.messages?.length === 1)
} finally { rmSync(tmp, { recursive: true, force: true }) }
const p = res.filter(Boolean).length
console.log(`autorecall_engine_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
