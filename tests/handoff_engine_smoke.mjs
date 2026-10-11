// C-Handoff seam: the engine writes the hand-off files at compaction/end (C-Handoff), 2026-10-03.
// usage: node tests/handoff_engine_smoke.mjs   (exit 0 iff "0 failed")
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { emptyMemory, mergeMemory, saveMemory } from '../cg_memory.js'
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
  return { ctx, emit: (name, ...a) => { for (const fn of listeners.get(name) ?? []) fn(...a) } }
}
function makeSession(id) {
  const events = [{ seq: 0, type: 'user/message', data: { role: 'user', content: [T('Build the parser for text tool calls')] } },
                  { seq: 1, type: 'user/message', data: { role: 'assistant', content: [T('ok')] } }]
  return { id, events, surface: { nodes: [0, 1], replaceGeneration: 0 }, deriveEventMessage: (e) => (e?.type === 'user/message' ? e.data : null) }
}
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const tmp = mkdtempSync(join(tmpdir(), 'cg-handoff-'))
try {
  const mem = join(tmp, 'mem', 'memory.json')
  saveMemory(mem, mergeMemory(emptyMemory(), [
    { cat: 'pins', text: 'port 8797 is control', seq: 3, session: 'sess-h', first: 'x', last: 'x' },
    { cat: 'decisions', text: 'keep hints ON', seq: 4, session: 'sess-h', first: 'x', last: 'x' }]))
  const h = makeCtx()
  engine.apply(h.ctx, { spanDir: join(tmp, 'spans'), logPath: join(tmp, 'log.jsonl'), memoryPath: mem, idleDelayMs: 0 })
  const s = makeSession('sess-h')
  h.emit('session/event', s, { seq: 5, type: 'compaction/start', data: { compactionId: 'c1' } })
  h.emit('session/event', s, { seq: 6, type: 'compaction/end', data: { compactionId: 'c1' } })
  const latest = join(tmp, 'mem', 'handoff_latest.json')
  check('latest_written_beside_memory', existsSync(latest))
  const j = existsSync(latest) ? JSON.parse(readFileSync(latest, 'utf8')) : {}
  check('session_and_goal', j.session === 'sess-h' && j.goal === 'Build the parser for text tool calls', JSON.stringify(j).slice(0, 200))
  check('pins_and_decisions_carried', (j.pins ?? []).some(p => p.text === 'port 8797 is control') && (j.decisions ?? []).includes('keep hints ON'))
  check('session_files_written', existsSync(join(tmp, 'mem', 'handoff_sess-h.json')) && existsSync(join(tmp, 'mem', 'handoff_sess-h.md')))
  const h2 = makeCtx(); const mem2 = join(tmp, 'mem2', 'memory.json'); saveMemory(mem2, emptyMemory())
  engine.apply(h2.ctx, { spanDir: join(tmp, 'spans2'), logPath: join(tmp, 'log2.jsonl'), memoryPath: mem2, idleDelayMs: 0 })
  h2.emit('session/event', makeSession('sess-e'), { seq: 6, type: 'compaction/end', data: { compactionId: 'c2', error: 'boom' } })
  check('errored_compaction_writes_nothing', !existsSync(join(tmp, 'mem2', 'handoff_latest.json')))
  process.env.GUARDIAN_HANDOFF = '0'
  const h3 = makeCtx(); const mem3 = join(tmp, 'mem3', 'memory.json'); saveMemory(mem3, emptyMemory())
  engine.apply(h3.ctx, { spanDir: join(tmp, 'spans3'), logPath: join(tmp, 'log3.jsonl'), memoryPath: mem3, idleDelayMs: 0 })
  h3.emit('session/event', makeSession('sess-o'), { seq: 6, type: 'compaction/end', data: { compactionId: 'c3' } })
  check('GUARDIAN_HANDOFF_0_turns_it_off', !existsSync(join(tmp, 'mem3', 'handoff_latest.json')))
  delete process.env.GUARDIAN_HANDOFF
} finally { rmSync(tmp, { recursive: true, force: true }) }
const p = res.filter(Boolean).length
console.log(`handoff_engine_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
