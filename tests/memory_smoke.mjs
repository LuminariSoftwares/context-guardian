// tests/memory_smoke.mjs -- plain node smoke test for cg_memory.js. No framework, no network.
import { mkdtempSync, readdirSync, rmSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import * as mem from '../cg_memory.js'

const {
  MEMORY_REV,
  MEMORY_VERSION,
  CATEGORIES,
  DEFAULT_MAX_PER_CATEGORY,
  emptyMemory,
  normalizeText,
  extractMemory,
  mergeMemory,
  renderMemory,
  memoryStats,
  itemsForSession,
  loadMemory,
  saveMemory,
  estTokens,
} = mem

const checks = []
const check = (name, fn) => checks.push([name, fn])

const T = (text) => ({ type: 'text', text })
const R = (text) => ({ type: 'reasoning', text })
const CALL = (name, args) => ({ type: 'tool-call', id: 'c' + name, name, arguments: args })
const RESULT = (texts, isError) => ({
  type: 'tool-result',
  toolCallId: 'c1',
  content: texts.map(T),
  isError,
})
const userNode = (seq, text, source) => ({ seq, message: { role: 'user', content: [T(text)], source } })
const asstNode = (seq, text) => ({ seq, message: { role: 'assistant', content: [T(text)] } })
const cpNode = (seq, text) => ({
  seq,
  message: { role: 'user', content: [T(text)], source: { kind: 'plugin', plugin: 'compact' } },
})
const item = (cat, text, seq, session = '', done = false) => ({ cat, text, seq, session, first: null, last: null, count: 1, done })
const cats = (items) => items.map((it) => it.cat)
const texts = (items) => items.map((it) => it.text)
const deep = (a, b) => JSON.stringify(a) === JSON.stringify(b)

const tmp = mkdtempSync(join(tmpdir(), 'cg-mem-'))

// --- the 21 contract checks -------------------------------------------------------

check('exports_exact', () => {
  const fns = ['emptyMemory', 'normalizeText', 'extractMemory', 'mergeMemory', 'renderMemory',
    'memoryStats', 'itemsForSession', 'loadMemory', 'saveMemory', 'estTokens']
  if (fns.some((n) => typeof mem[n] !== 'function')) return false
  if (MEMORY_REV !== 'cg-memory-1' || MEMORY_VERSION !== 1) return false
  if (!Array.isArray(CATEGORIES) || CATEGORIES.length !== 6) return false
  if (!deep(CATEGORIES.slice(), ['decisions', 'constraints', 'files', 'todos', 'errors', 'preferences'])) return false
  if (!Object.isFrozen(CATEGORIES) || !Object.isFrozen(DEFAULT_MAX_PER_CATEGORY)) return false
  if (!deep(emptyMemory(), { version: 1, updated: null, items: [] })) return false
  if (estTokens('abcd') !== Math.ceil(4 / 3.5)) return false
  return typeof DEFAULT_MAX_PER_CATEGORY.files === 'number'
})

check('explicit_prefixes_all_categories', () => {
  const cases = [
    ['decision: use sqlite for state', 'decisions', 'use sqlite for state'],
    ['decided: keep the old parser', 'decisions', 'keep the old parser'],
    ['remember: the staging host is eu-1', 'decisions', 'the staging host is eu-1'],
    ['constraint: never touch main directly', 'constraints', 'never touch main directly'],
    ['rule: every test needs a fixture', 'constraints', 'every test needs a fixture'],
    ['todo: rewrite the indexer', 'todos', 'rewrite the indexer'],
    ['next: bump the version string', 'todos', 'bump the version string'],
    ['next step: run the smoke suite', 'todos', 'run the smoke suite'],
    ['error: the build timed out again', 'errors', 'the build timed out again'],
    ['preference: short focused commits', 'preferences', 'short focused commits'],
    ['prefer: tabs over spaces here', 'preferences', 'tabs over spaces here'],
  ]
  const body = cases.map((c) => c[0]).join('\n')
  const got = extractMemory([userNode(1, body)])
  if (got.length !== cases.length) return false
  for (let i = 0; i < cases.length; i++) {
    if (got[i].cat !== cases[i][1] || got[i].text !== cases[i][2]) return false
    if (got[i].seq !== 1 || got[i].session !== '' || got[i].count !== 1) return false
    if (got[i].first !== null || got[i].last !== null || got[i].done !== false) return false
  }
  return true
})

check('assistant_prefix_counts', () => {
  const got = extractMemory([asstNode(4, 'decision: use sqlite')])
  if (got.length !== 1) return false
  const it = got[0]
  return it.cat === 'decisions' && it.text === 'use sqlite' && it.seq === 4 && it.count === 1
})

check('user_strong_patterns', () => {
  const body = 'Never push to main\nI prefer tabs\nLet\'s use pnpm'
  const u = extractMemory([userNode(2, body)])
  const a = extractMemory([asstNode(3, body)])
  if (!deep(cats(u), ['constraints', 'preferences', 'decisions'])) return false
  if (!deep(texts(u), ['Never push to main', 'I prefer tabs', "Let's use pnpm"])) return false
  return a.length === 0
})

check('checkbox_todos', () => {
  const open = extractMemory([userNode(1, '- [ ] write docs for the module')])
  const done = extractMemory([userNode(2, '- [x] write docs for the module')])
  if (open.length !== 1 || open[0].cat !== 'todos' || open[0].done !== false) return false
  if (done.length !== 1 || done[0].done !== true) return false
  const m = mergeMemory(mergeMemory(emptyMemory(), open, { now: '2026-01-01T00:00:00.000Z' }), done, { now: '2026-01-02T00:00:00.000Z' })
  if (m.items.length !== 1) return false
  return m.items[0].done === true && m.items[0].count === 2 && m.items[0].seq === 2
})

check('injected_user_ignored', () => {
  const n = userNode(1, 'decision: rebuild the container image', { kind: 'agent-instructions' })
  if (extractMemory([n]).length !== 0) return false
  const n2 = userNode(1, '- [ ] a listed task item', { plugin: 'harness' })
  return extractMemory([n2]).length === 0
})

check('tool_error_extracted', () => {
  const nodes = [
    { seq: 10, message: { role: 'user', content: [RESULT(['Traceback (most recent call last):', '  File "x.py", line 3'])] } },
    { seq: 11, message: { role: 'user', content: [RESULT(['', '   ', 'could not open the socket'], true)] } },
    { seq: 12, message: { role: 'user', content: [RESULT(['all good', '3 files written'])] } },
  ]
  const got = extractMemory(nodes)
  if (got.length !== 2) return false
  if (got[0].cat !== 'errors' || got[0].text !== 'Traceback (most recent call last):' || got[0].seq !== 10) return false
  return got[1].cat === 'errors' && got[1].text === 'could not open the socket' && got[1].seq === 11
})

check('files_from_tool_calls', () => {
  const node = {
    seq: 7,
    message: {
      role: 'assistant',
      content: [
        CALL('edit_file', '{"path":"src/a.py"}'),
        CALL('write_file', { file_path: 'b/c.js' }),
        CALL('create_thing', '{"target":"D/d.md"}'),
        CALL('patch_file', '{not json'),
        CALL('read_file', '{"path":"never/seen.py"}'),
      ],
    },
  }
  const got = extractMemory([node])
  if (got.length !== 3) return false
  if (!deep(texts(got), ['src/a.py', 'b/c.js', 'D/d.md'])) return false
  return got.every((it) => it.cat === 'files' && it.seq === 7)
})

check('files_from_checkpoint', () => {
  const text = 'FILES WRITTEN (latest last): a/x.py (seq 5), b/y.js (seq 9)\nfiles: c/z.md, d/w.txt\ndecision: never re-extract this'
  const got = extractMemory([cpNode(20, text)])
  if (got.length !== 4) return false
  if (!deep(cats(got), ['files', 'files', 'files', 'files'])) return false
  if (!deep(texts(got), ['a/x.py', 'b/y.js', 'c/z.md', 'd/w.txt'])) return false
  return deep(got.map((it) => it.seq), [5, 9, 20, 20])
})

check('normalize_and_dedupe', () => {
  if (normalizeText('  Use   PNPM. ') !== 'use pnpm') return false
  if (normalizeText('A\tsplit\nline!') !== 'a split line') return false
  const a = extractMemory([userNode(1, 'decision: Use PNPM.')])
  const b = extractMemory([userNode(2, 'decision: use pnpm')])
  const m = mergeMemory(mergeMemory(emptyMemory(), a, { now: '2026-01-01T00:00:00.000Z' }), b, { now: '2026-01-02T00:00:00.000Z' })
  if (m.items.length !== 1 || m.items[0].count !== 2) return false
  const calls = {
    seq: 3,
    message: { role: 'assistant', content: [CALL('write_file', '{"path":"A.py"}'), CALL('write_file', '{"path":"a.py"}')] },
  }
  return mergeMemory(emptyMemory(), extractMemory([calls])).items.length === 2
})

check('merge_is_pure', () => {
  const before = mergeMemory(emptyMemory(), [item('decisions', 'keep this decision', 1, 's1')], { now: '2026-01-01T00:00:00.000Z' })
  const snapshot = JSON.stringify(before)
  const incoming = [item('decisions', 'keep this decision', 2, 's1'), item('todos', 'new open task', 3, 's1')]
  const incomingSnap = JSON.stringify(incoming)
  const out = mergeMemory(before, incoming, { now: '2026-01-05T00:00:00.000Z' })
  if (JSON.stringify(before) !== snapshot) return false
  if (JSON.stringify(incoming) !== incomingSnap) return false
  if (out === before || out.items === before.items) return false
  if (out.items[0].count !== 2) return false
  const again = mergeMemory(before, incoming, { now: '2026-01-05T00:00:00.000Z' })
  return deep(again, out)
})

check('cap_drops_oldest', () => {
  let m = emptyMemory()
  m = mergeMemory(m, [item('decisions', 'alpha fact here', 1)], { now: '2026-01-01T00:00:00.000Z', maxPerCategory: { decisions: 2 } })
  m = mergeMemory(m, [item('decisions', 'bravo fact here', 2)], { now: '2026-01-02T00:00:00.000Z', maxPerCategory: { decisions: 2 } })
  m = mergeMemory(m, [item('decisions', 'delta fact here', 3)], { now: '2026-01-03T00:00:00.000Z', maxPerCategory: { decisions: 2 } })
  if (m.items.length !== 2) return false
  if (m.updated !== '2026-01-03T00:00:00.000Z' || m.version !== 1) return false
  return deep(texts(m.items), ['bravo fact here', 'delta fact here'])
})

check('render_order_and_omit', () => {
  const items = [
    item('errors', 'the build timed out', 5, '', false),
    item('files', 'src/a.py', 6, '', false),
    item('todos', 'ship the release', 7, '', false),
    item('todos', 'already done thing', 8, '', true),
    item('decisions', 'use pnpm', 9, '', false),
    item('preferences', 'short commits', 10, '', false),
    item('constraints', 'never force push', 11, '', false),
  ]
  const txt = renderMemory(mergeMemory(emptyMemory(), items, { now: '2026-01-01T00:00:00.000Z' }))
  const lines = txt.split('\n')
  if (lines[0] !== '[memory -- durable notes kept across compactions and sessions by context-guardian; newest last]') return false
  const at = (s) => txt.indexOf(s)
  if (at('constraints:') < 0 || at('preferences:') < 0 || at('decisions:') < 0) return false
  if (!(at('constraints:') < at('preferences:') && at('preferences:') < at('decisions:'))) return false
  if (!(at('decisions:') < at('todos:') && at('todos:') < at('files:') && at('files:') < at('errors (latest):'))) return false
  if (txt.includes('already done thing') || txt.includes('- [x]')) return false
  if (!txt.includes('- [ ] ship the release')) return false
  if (lines.filter((l) => l.startsWith('files:')).length !== 1) return false
  if (!txt.includes('files: src/a.py')) return false
  if (!txt.includes('- the build timed out (seq 5)')) return false
  const only = renderMemory(mergeMemory(emptyMemory(), [item('constraints', 'never force push', 1)], { now: '2026-01-01T00:00:00.000Z' }))
  if (only.includes('decisions:') || only.includes('todos:') || only.includes('errors')) return false
  return only.split('\n').length === 3
})

check('render_budget_priority', () => {
  const items = [
    item('constraints', 'never force push to main', 1, '', false),
    item('constraints', 'never rewrite the changelog', 2, '', false),
    item('constraints', 'never commit generated bundles', 3, '', false),
    item('preferences', 'prefers extremely long preference lines that blow the budget', 4, '', false),
    item('decisions', 'decided to use a very long decision line indeed', 5, '', false),
    item('errors', 'the test suite failed again', 6, '', false),
  ]
  const txt = renderMemory(mergeMemory(emptyMemory(), items, { now: '2026-01-01T00:00:00.000Z' }), { maxTokens: 60 })
  if (estTokens(txt) > 60) return false
  if (!txt.includes('never force push to main')) return false
  if (txt.includes('errors') || txt.includes('test suite failed')) return false
  return txt.split('\n')[1] === 'constraints:'
})

check('render_budget_never_exceeded', () => {
  const items = []
  for (let i = 0; i < 8; i++) {
    items.push(item('constraints', 'standing constraint number ' + i, i + 1))
    items.push(item('preferences', 'a stated preference number ' + i, i + 2))
    items.push(item('decisions', 'a recorded decision number ' + i, i + 3))
    items.push(item('todos', 'an open task number ' + i, i + 4))
    items.push(item('todos', 'a closed task number ' + i, i + 5, '', true))
    items.push(item('files', 'pkg/mod/file' + i + '.js', i + 6))
    items.push(item('errors', 'something failed on step ' + i, i + 7))
  }
  const m = mergeMemory(emptyMemory(), items, {
    now: '2026-01-07T00:00:00.000Z',
    maxPerCategory: { constraints: 100, preferences: 100, decisions: 100, todos: 100, files: 100, errors: 100 },
  })
  for (let budget = 0; budget <= 220; budget += 7) {
    const txt = renderMemory(m, { maxTokens: budget })
    if (estTokens(txt) > budget) return false
    if (txt !== '' && !txt.startsWith('[memory --')) return false
  }
  const wide = renderMemory(m)
  if (estTokens(wide) > 1200) return false
  if (!wide.includes('files: pkg/mod/')) return false
  if (wide.includes('a closed task number')) return false
  return wide.split('\n').filter((l) => l.startsWith('files:')).length === 1
})

check('render_empty', () => {
  if (renderMemory(emptyMemory()) !== '') return false
  if (renderMemory(null) !== '') return false
  const doneOnly = mergeMemory(emptyMemory(), [item('todos', 'the finished task', 1, '', true)], { now: '2026-01-01T00:00:00.000Z' })
  if (renderMemory(doneOnly) !== '') return false
  return renderMemory(mergeMemory(emptyMemory(), [item('decisions', 'use pnpm', 1)], { now: '2026-01-01T00:00:00.000Z' }), { maxTokens: 1 }) === ''
})

check('save_load_roundtrip', () => {
  const dir = join(tmp, 'round')
  const p = join(dir, 'nested', 'memory.json')
  const m = mergeMemory(emptyMemory(), [
    item('decisions', 'use pnpm', 4, 's1'),
    item('todos', 'write the docs', 5, 's1', false),
    item('files', 'src/a.py', 6, 's2'),
  ], { now: '2026-01-07T00:00:00.000Z' })
  if (saveMemory(p, m) !== true) return false
  const back = loadMemory(p)
  if (back.status !== 'loaded') return false
  if (!deep(back.memory.items, m.items)) return false
  if (back.memory.updated !== '2026-01-07T00:00:00.000Z' || back.memory.version !== 1) return false
  if (readdirSync(dir).some((f) => f.includes('.tmp-'))) return false
  return saveMemory(p, m) === true && readdirSync(dir).length === 1
})

check('load_missing', () => {
  const r = loadMemory(join(tmp, 'nope', 'missing.json'))
  if (r.status !== 'missing') return false
  return deep(r.memory, emptyMemory())
})

check('load_corrupt_quarantined', () => {
  const p = join(tmp, 'corrupt.json')
  writeFileSync(p, '{not json', 'utf8')
  const r = loadMemory(p)
  if (r.status !== 'corrupt') return false
  if (!deep(r.memory, emptyMemory())) return false
  if (typeof r.error !== 'string') return false
  const names = readdirSync(tmp)
  return names.some((f) => f.startsWith('corrupt.json.corrupt-'))
})

check('save_never_throws', () => {
  const parent = join(tmp, 'iam-a-file')
  writeFileSync(parent, 'not a directory', 'utf8')
  let threw = false
  let ok = true
  try { ok = saveMemory(join(parent, 'memory.json'), emptyMemory()) } catch { threw = true }
  if (threw || ok !== false) return false
  if (saveMemory(null, emptyMemory()) !== false) return false
  return saveMemory('', emptyMemory()) === false
})

check('stats_and_session_filter', () => {
  const m = mergeMemory(emptyMemory(), [
    item('decisions', 'use pnpm', 1, 's1'),
    item('constraints', 'never force push', 2, 's1'),
    item('todos', 'write the docs', 3, 's1', false),
    item('todos', 'old finished thing', 4, 's1', true),
    item('files', 'src/a.py', 5, 's2'),
  ], { now: '2026-01-07T00:00:00.000Z' })
  const st = memoryStats(m)
  if (st.total !== 5) return false
  if (st.openTodos !== 1) return false
  const want = { decisions: 1, constraints: 1, files: 1, todos: 2, errors: 0, preferences: 0 }
  if (JSON.stringify(st.byCategory) !== JSON.stringify(want)) return false
  const s1 = itemsForSession(m, 's1')
  if (s1.length !== 4 || !s1.every((it) => it.session === 's1')) return false
  if (itemsForSession(m, 's2').length !== 1) return false
  if (itemsForSession(m, 'nope').length !== 0) return false
  return memoryStats(emptyMemory()).total === 0
})

check('default_opts_resolve', () => {
  const many = []
  for (let i = 0; i < 200; i++) {
    many.push(item('constraints', 'repeat the standing constraint ' + String(i).padStart(3, '0'), i + 1))
  }
  const big = mergeMemory(emptyMemory(), many, { now: '2026-01-07T00:00:00.000Z', maxPerCategory: { constraints: 1000 } })
  const full = renderMemory(big, { maxTokens: 100000 })
  if (estTokens(full) < 1800) return false
  const txt = renderMemory(big)
  const tk = estTokens(txt)
  if (!(tk >= 1000 && tk <= 1200)) return false
  const bare = extractMemory([userNode(1, 'decision: use pnpm for every install')])
  if (bare.length !== 1 || bare[0].session !== '') return false
  return renderMemory(emptyMemory(), {}) === ''
})

// --- extra hardening checks -------------------------------------------------------

check('null_message_and_reasoning_ignored', () => {
  const nodes = [
    { seq: 1, message: null },
    { seq: 2, message: { role: 'assistant', content: [R('decision: this is only reasoning')] } },
    { seq: 3, message: { role: 'user' } },
    { seq: 4, message: { role: 'user', content: [] } },
    { seq: 5, message: { role: 'user', content: [T('ok')] } },
  ]
  if (extractMemory(nodes).length !== 0) return false
  if (extractMemory(null).length !== 0) return false
  if (extractMemory([userNode(1, 'decision: a real decision')], { session: 'sess-9' })[0].session !== 'sess-9') return false
  return true
})

check('text_clamped_to_limits', () => {
  const long = 'x'.repeat(400)
  const d = extractMemory([userNode(1, 'decision: ' + long)])[0]
  if (d.text.length !== 300 || !d.text.endsWith('...')) return false
  const e = extractMemory([{
    seq: 2,
    message: { role: 'user', content: [RESULT(['ENOENT: no such file ' + long])] },
  }])[0]
  if (e.cat !== 'errors' || e.text.length !== 200 || !e.text.endsWith('...')) return false
  return true
})

check('never_throws_on_garbage_inputs', () => {
  renderMemory(undefined)
  renderMemory({ items: [null, 3, { cat: 'nope', text: 'x' }] })
  memoryStats(null)
  itemsForSession(null, 'a')
  mergeMemory(null, null, null)
  mergeMemory({ items: 'no' }, [null, {}, { cat: 'decisions', text: '   ' }], { maxPerCategory: { decisions: -1 } })
  extractMemory([null, {}, { message: { role: 'user', content: [null, 5, { type: 'text' }] } }])
  loadMemory(join(tmp, 'a-file.json'))
  saveMemory(join(tmp, 'ok.json'), emptyMemory())
  return true
})

// --- runner -----------------------------------------------------------------------

async function main() {
  let passed = 0
  for (const [name, fn] of checks) {
    let ok = false
    let errMsg = ''
    try {
      ok = (await fn()) === true
      if (!ok) errMsg = 'check returned false'
    } catch (err) {
      ok = false
      errMsg = String((err && err.message) || err)
    }
    console.log(ok ? `  ok   ${name}` : `  FAIL ${name} (error: ${errMsg})`)
    if (ok) passed += 1
  }
  const failed = checks.length - passed
  console.log(`memory_smoke: ${checks.length} checks, ${passed} passed, ${failed} failed`)
  rmSync(tmp, { recursive: true, force: true })
  process.exit(failed === 0 && checks.length >= 21 ? 0 : 1)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
