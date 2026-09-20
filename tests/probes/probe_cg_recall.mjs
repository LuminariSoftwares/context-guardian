// Overseer contract probe for cg_recall.js. READ-ONLY for the module author.
// usage: node tests/probes/probe_cg_recall.mjs [path-to-cg_recall.js]
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'

const target = resolve(process.argv[2] ?? 'cg_recall.js')
let m
try { m = await import(pathToFileURL(target).href) } catch (e) {
  console.log(`  FAIL import (${e.message})`)
  console.log('probe_cg_recall: 1 checks, 0 passed, 1 failed'); process.exit(1)
}

const T = (text) => ({ type: 'text', text })
const nodes = [
  { seq: 0, message: null },
  { seq: 1, message: { role: 'user', content: [T('please open scripts/gpu_broker.py and find lease_timeout')] } },
  { seq: 2, message: { role: 'assistant', content: [{ type: 'reasoning', text: 'SECRET-THOUGHT' }, T('Reading it now.'), { type: 'tool-call', id: 'c1', name: 'read', arguments: '{"path":"scripts/gpu_broker.py"}' }] } },
  { seq: 3, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'c1', content: [T('def lease_timeout():\n    return 90  # a.b*c literal'), { type: 'image' }] }] } },
  { seq: 4, message: { role: 'assistant', content: [T('lease_timeout returns 90 in scripts/gpu_broker.py')] } },
  { seq: 5, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T('CHECKPOINT-ONE body')] } },
  { seq: 6, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'c9', isError: true, content: [T('boom')] }] } },
  { seq: 7, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T('CHECKPOINT-TWO body')] } },
]

const checks = []
const check = (name, fn) => checks.push([name, fn])
const okReq = (t, i) => { const r = m.parseRecallRequest(t, i); if (!r.ok) throw new Error('parse failed: ' + JSON.stringify(r)); return r }

check('rev_and_estTokens', () => m.RECALL_REV === 'cg-recall-1' && m.estTokens('abcde') === 2 && m.estTokens('') === 0 && m.estTokens(null) === 0)
check('parse_default_type_and_range', () => {
  const a = m.parseRecallRequest(undefined, '3-7'), b = m.parseRecallRequest(' SEQ ', ' 7 - 3 '), c = m.parseRecallRequest('seq', 4), d = m.parseRecallRequest('seq', '3..7')
  return a.ok && a.type === 'seq' && a.from === 3 && a.to === 7 && b.ok && b.from === 3 && b.to === 7 && c.ok && c.from === 4 && c.to === 4 && d.ok && d.to === 7
})
check('parse_rejects_bad', () => {
  const bad = [m.parseRecallRequest('seq', ''), m.parseRecallRequest('seq', '-3'), m.parseRecallRequest('seq', '1.5'), m.parseRecallRequest('files', '3'), m.parseRecallRequest('result', '3-4'), m.parseRecallRequest('checkpoint', '1-2'), m.parseRecallRequest('seq', 'abc')]
  return bad.every(r => r.ok === false && typeof r.error === 'string') && bad[0].error.includes('seq "3-7"')
})
check('parse_command', () => {
  const a = m.parseRecallCommand('  seq   3-7 '), b = m.parseRecallCommand('3-7'), c = m.parseRecallCommand('result 3'), d = m.parseRecallCommand('checkpoint 1'), e = m.parseRecallCommand('   '), f = m.parseRecallCommand('7')
  return a.ok && a.from === 3 && a.to === 7 && b.ok && b.type === 'seq' && c.ok && c.type === 'result' && c.from === 3 && d.ok && d.type === 'checkpoint' && e.ok === false && f.ok && f.from === 7
})
check('render_skips_reasoning_and_formats_call', () => {
  const s = m.renderMessage(nodes[2].message)
  return !s.includes('SECRET-THOUGHT') && s.includes('Reading it now.') && s.includes('* read({"path":"scripts/gpu_broker.py"})') && m.renderMessage(null) === ''
})
check('render_tool_result_and_error', () => {
  const s = m.renderMessage(nodes[3].message), e = m.renderMessage(nodes[6].message)
  return s.startsWith('[result of c1]\n') && s.includes('return 90') && s.includes('[image]') && e.startsWith('[ERROR result of c9]') && e.includes('boom')
})
check('recall_seq_range', () => {
  const r = m.recall(nodes, okReq('seq', '0-4'))
  return r.ok && JSON.stringify(r.seqs) === '[1,2,3,4]' && r.text.includes('[seq 1 user]') && r.text.includes('[seq 4 assistant]') && r.truncated === false && r.tokens === m.estTokens(r.text)
})
check('recall_budget_truncates_with_next_pointer', () => {
  const r = m.recall(nodes, okReq('seq', '1-4'), { maxTokens: 40 })
  return r.ok && r.truncated === true && r.seqs.length >= 1 && r.seqs.length < 4 && r.seqs[0] === 1 &&
    r.text.trimEnd().endsWith(`[recall truncated — next: recall(type="seq", id="${r.seqs[r.seqs.length - 1] + 1}-4")]`)
})
check('recall_first_node_too_big_is_cut_not_dropped', () => {
  const big = [{ seq: 1, message: { role: 'user', content: [T('x'.repeat(5000))] } }, { seq: 2, message: { role: 'user', content: [T('y')] } }]
  const r = m.recall(big, okReq('seq', '1-2'), { maxTokens: 100 })
  return r.ok && r.truncated === true && r.seqs[0] === 1 && r.text.includes('xxxx') && r.text.length < 1000
})
check('recall_result_by_result_seq_and_by_call_seq', () => {
  const a = m.recall(nodes, okReq('result', 3)), b = m.recall(nodes, okReq('result', 2)), c = m.recall(nodes, okReq('result', 4))
  return a.ok && a.text.includes('return 90') && b.ok && JSON.stringify(b.seqs) === '[3]' && c.ok === false && c.text.startsWith('NOT FOUND')
})
check('recall_checkpoint_ordinal', () => {
  const a = m.recall(nodes, okReq('checkpoint', 1)), b = m.recall(nodes, okReq('checkpoint', 2)), c = m.recall(nodes, okReq('checkpoint', 3))
  return a.ok && a.text.includes('CHECKPOINT-ONE') && !a.text.includes('CHECKPOINT-TWO') && b.ok && b.text.includes('CHECKPOINT-TWO') && c.ok === false
})
check('recall_not_found_names_range', () => {
  const r = m.recall(nodes, okReq('seq', '50-60'))
  return r.ok === false && r.text.startsWith('NOT FOUND') && r.text.includes('seqs 1-7') && r.seqs.length === 0 && r.truncated === false
})
check('search_literal_is_not_regex_and_case_insensitive', () => {
  const a = m.search(nodes, 'A.B*C'), b = m.search(nodes, 'LEASE_TIMEOUT')
  return a.ok && a.total === 1 && a.hits[0].seq === 3 && b.ok && b.total === 3 && JSON.stringify(b.hits.map(h => h.seq)) === '[1,3,4]' && b.hits[0].role === 'user'
})
check('search_regex_and_invalid_regex', () => {
  const a = m.search(nodes, 'return\\s+\\d+', { regex: true }), b = m.search(nodes, '([', { regex: true }), c = m.search(nodes, '   ')
  return a.ok && a.total === 1 && a.hits[0].seq === 3 && b.ok === false && typeof b.error === 'string' && c.ok === false
})
check('search_cap_and_snippet', () => {
  const many = Array.from({ length: 60 }, (_, i) => ({ seq: i + 1, message: { role: 'assistant', content: [T('a'.repeat(300) + '\n\tNEEDLE\n' + 'b'.repeat(300))] } }))
  const r = m.search(many, 'needle', { maxHits: 5, contextChars: 10 })
  const s = r.hits[0].snippet
  return r.ok && r.total === 60 && r.hits.length === 5 && r.capped === true && !/[\n\t]/.test(s) && s.includes('NEEDLE') && s.startsWith('…') && s.endsWith('…') && s.length <= 40
})
check('search_skips_reasoning', () => m.search(nodes, 'SECRET-THOUGHT').total === 0)
check('render_search', () => {
  const r = m.search(nodes, 'lease_timeout'), s = m.renderSearch(r, 'lease_timeout'), lines = s.trimEnd().split('\n')
  const z = m.renderSearch(m.search(nodes, 'zzzznope'), 'zzzznope'), e = m.renderSearch(m.search(nodes, '([', { regex: true }), '([')
  return lines[lines.length - 1] === 'NEXT STEP: recall(type="seq", id="1")' && s.includes('3 hits for "lease_timeout"') && s.includes('seq 3 [user]') && z.includes('0 hits') && z.includes('zzzznope') && e.startsWith('search error: ')
})
check('keyword_index', () => {
  const idx = m.foldKeywordIndex(nodes)
  const terms = idx.map(x => x.term)
  const gb = idx.find(x => x.term === 'scripts/gpu_broker.py')
  const lt = idx.find(x => x.term === 'lease_timeout')
  return !!gb && JSON.stringify(gb.seqs) === '[1,2,4]' && !!lt && JSON.stringify(lt.seqs) === '[1,3,4]' && !terms.includes('returns') && !terms.includes('this') &&
    terms.indexOf('lease_timeout') < terms.indexOf('scripts/gpu_broker.py')
})
check('keyword_index_limits_and_render', () => {
  const many = Array.from({ length: 30 }, (_, i) => ({ seq: i + 1, message: { role: 'user', content: [T('commonterm word' + (i % 3) + 'zz')] } }))
  const idx = m.foldKeywordIndex(many, { maxTerms: 2, maxSeqsPerTerm: 3 })
  const txt = m.renderKeywordIndex(idx)
  return idx.length === 2 && idx[0].term === 'commonterm' && JSON.stringify(idx[0].seqs) === '[1,2,3]' && txt.split('\n')[0] === 'KEYWORD INDEX (term → seqs; use recall):' && txt.includes('commonterm: 1, 2, 3') && m.renderKeywordIndex([]) === ''
})
check('rewrite_cost', () => {
  const a = m.rewriteCost({ surfaceTokens: 30000, shadowedTokens: 20000, replacementTokens: 2000, window: 32768, cachedPrefixTokens: 25000 })
  const b = m.rewriteCost({ surfaceTokens: 8000, shadowedTokens: 3000, replacementTokens: 1000, window: 32768 })
  const c = m.rewriteCost({ surfaceTokens: 1, shadowedTokens: 1, replacementTokens: 0, window: 0 })
  return a.saved === 18000 && a.tier === 'emergency' && a.worthIt === true && a.pressureBefore === 0.9155 && a.pressureAfter === 0.3662 && a.reprefillTokens === 7000 &&
    b.tier === 'none' && b.worthIt === false && b.saved === 2000 && b.reprefillTokens === 6000 && typeof c.error === 'string'
})
check('tiers', () => {
  const t = (s) => m.rewriteCost({ surfaceTokens: s, shadowedTokens: 10, replacementTokens: 5, window: 1000 }).tier
  return t(299) === 'none' && t(300) === 'watch' && t(500) === 'idle' && t(700) === 'compact' && t(900) === 'emergency'
})
check('never_throws_on_garbage', () => {
  m.renderMessage({}); m.renderMessage({ role: 'user' }); m.recall([], okReq('seq', 1)); m.recall(null, okReq('seq', 1)); m.search(null, 'x'); m.foldKeywordIndex(null); m.recall(nodes, { ok: false })
  return true
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false, err = ''
  try { ok = fn() === true } catch (e) { err = ` (error: ${e.message})` }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${err}`)
  if (ok) passed += 1
}
console.log(`probe_cg_recall: ${checks.length} checks, ${passed} passed, ${checks.length - passed} failed`)
process.exit(passed === checks.length ? 0 : 1)
