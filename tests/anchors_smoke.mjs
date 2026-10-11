// tests/anchors_smoke.mjs -- contract probe for cg_anchors.js.
// Written FROM THE CONTRACT before the module existed. No framework,
// no network, no filesystem. Prints `anchors_smoke: N checks, N passed, M failed`.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
let mod = null
let loadError = null
try {
  mod = await import('../cg_anchors.js')
} catch (error) {
  loadError = error
}

const checks = []
const check = (name, fn) => checks.push([name, fn])
const deep = (a, b) => JSON.stringify(a) === JSON.stringify(b)

check('module_loads_with_exports', () => {
  if (mod === null) return false
  for (const fn of ['extractAnchors', 'findLostAnchors', 'lostConstraints', 'renderCarried']) {
    if (typeof mod[fn] !== 'function') return false
  }
  return mod.ANCHORS_REV === 'cg-anchors-1'
})

check('module_is_pure_no_fs_no_network', () => {
  const src = readFileSync(join(HERE, '..', 'cg_anchors.js'), 'utf8')
  return !/node:fs|from 'fs'|require\(|fetch\(|node:child_process|node:net|node:http/.test(src)
})

check('extracts_each_anchor_kind', () => {
  const text = [
    'We set `MAX_RETRIES` to 5.',
    'Docs at https://example.org/guide/start.html, see there.',
    'Edited C:\\path\\to\\proj\\worker.py and src/app/main.ts today.',
    'Also touched config.yml.',
    'Then call build_index(root) again.',
  ].join('\n')
  const got = mod.extractAnchors(text)
  const want = ['MAX_RETRIES', 'https://example.org/guide/start.html', 'C:\\path\\to\\proj\\worker.py', 'src/app/main.ts', 'config.yml', 'build_index']
  return want.every(w => got.includes(w))
})

check('first_appearance_order_and_case_insensitive_dedupe', () => {
  const got = mod.extractAnchors('call load_cfg( then `Alpha_Beta` then `alpha_beta` then load_cfg(x)')
  return deep(got, ['load_cfg', 'Alpha_Beta'])
})

check('substrings_of_longer_anchors_are_dropped', () => {
  const got = mod.extractAnchors('See https://example.com/docs/a.html for details.')
  return got.includes('https://example.com/docs/a.html') && !got.some(a => a !== 'https://example.com/docs/a.html' && 'https://example.com/docs/a.html'.toLowerCase().includes(a.toLowerCase()))
})

check('trailing_punctuation_stripped_from_urls', () => {
  const got = mod.extractAnchors('Go to https://x.io/page.')
  return got.includes('https://x.io/page') && !got.includes('https://x.io/page.')
})

check('backtick_length_bounds', () => {
  const long = 'a'.repeat(121)
  const got = mod.extractAnchors('`ab` and `abc` and `' + long + '`')
  return got.includes('abc') && !got.includes('ab') && !got.includes(long)
})

check('at_most_200_anchors', () => {
  const text = Array.from({ length: 300 }, (_, i) => '`token_' + i + '`').join(' ')
  const got = mod.extractAnchors(text)
  return got.length === 200 && got[0] === 'token_0'
})

check('empty_and_non_string_input', () => deep(mod.extractAnchors(''), []) && deep(mod.extractAnchors(undefined), []))

check('find_lost_recurring_kept_lost', () => {
  const r = mod.findLostAnchors({
    regionTexts: ['Set `MAX_RETRIES` in db/schema.sql', 'helper parse_row( added'],
    laterTexts: ['now bump MAX_RETRIES and re-run db/schema.sql'],
    checkpointText: 'summary: MAX_RETRIES was raised',
    memoryTexts: [],
  })
  return deep(r.recurring, ['MAX_RETRIES', 'db/schema.sql']) && deep(r.kept, ['MAX_RETRIES']) && deep(r.lost, ['db/schema.sql'])
})

check('memory_counts_as_kept', () => {
  const r = mod.findLostAnchors({
    regionTexts: ['wrote db/schema.sql'],
    laterTexts: ['check db/schema.sql'],
    checkpointText: 'nothing relevant',
    memoryTexts: ['files: db/schema.sql'],
  })
  return deep(r.lost, []) && deep(r.kept, ['db/schema.sql'])
})

check('lost_is_capped_at_40', () => {
  // alpha_1_z is not a substring of alpha_11_z, so no name is dropped as a substring of another
  const names = Array.from({ length: 60 }, (_, i) => 'alpha_' + i + '_z(')
  const r = mod.findLostAnchors({ regionTexts: [names.join(' ')], laterTexts: [names.join(' ')], checkpointText: '', memoryTexts: [] })
  return r.recurring.length === 60 && r.lost.length === 40 && r.lost[0] === 'alpha_0_z'
})

check('lost_constraints_normalised_match', () => {
  const items = [
    { cat: 'constraints', text: 'Never   push to MAIN directly' },
    { cat: 'decisions', text: 'use sqlite for state' },
    { cat: 'todos', text: 'rewrite the indexer' },
  ]
  const lost = mod.lostConstraints({ regionItems: items, checkpointText: 'rules: never push to main directly.' })
  return deep(lost.map(i => i.text), ['use sqlite for state'])
})

check('lost_constraints_capped_at_20', () => {
  const items = Array.from({ length: 30 }, (_, i) => ({ cat: 'decisions', text: 'decision number ' + i }))
  return mod.lostConstraints({ regionItems: items, checkpointText: '' }).length === 20
})

check('render_carried_empty_is_empty_string', () => mod.renderCarried([], []) === '')

check('render_carried_exact_shape', () => {
  const out = mod.renderCarried(['db/schema.sql'], [{ cat: 'constraints', text: 'never push to main' }, { cat: 'decisions', text: 'use sqlite' }])
  const lines = out.split('\n')
  return lines[0] === '[context-guardian: carried facts -- named before this checkpoint and still referenced after it, but missing from the summary above]'
    && lines[1] === '- `db/schema.sql`'
    && lines[2] === '- constraint: never push to main'
    && lines[3] === '- decision: use sqlite'
    && lines.length === 4
})

check('render_carried_respects_max_chars', () => {
  const lost = Array.from({ length: 100 }, (_, i) => 'some/long/path/file_' + i + '.py')
  const out = mod.renderCarried(lost, [], 400)
  const lines = out.split('\n')
  const last = lines[lines.length - 1]
  const body = lines.slice(0, -1).join('\n')
  return /^- \(\+\d+ more; recall by seq\)$/.test(last) && body.length <= 400
    && Number(last.match(/\d+/)[0]) === 100 - (lines.length - 2)
})

let passed = 0
let failed = 0
if (loadError !== null) console.log(`  load error: ${loadError.message}`)
for (const [name, fn] of checks) {
  let ok = false
  try { ok = mod !== null && fn() === true } catch (error) { ok = false; console.log(`  ${name}: threw ${error.message}`) }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`)
  if (ok) passed += 1
  else failed += 1
}
console.log(`anchors_smoke: ${checks.length} checks, ${passed} passed, ${failed} failed`)
process.exit(failed === 0 && checks.length > 0 ? 0 : 1)
