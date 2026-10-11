// Contract probe for cg_autorecall.js (C-Recall stage a), written from the contract before the module, 2026-10-03.
// Usage: node autorecall_smoke.mjs <dir>  (dir holds cg_autorecall.js + cg_recall.js)
import { spawnSync } from 'node:child_process'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
const D = resolve(process.argv[2] || '.')
const res = []
const check = (n, c, why = '') => { res.push(Boolean(c)); console.log((c ? '  ok   ' : '  FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const safe = (n, fn) => { try { const r = fn(); check(n, r === true, r === true ? '' : String(r).slice(0, 300)) } catch (e) { res.push(false); console.log('  FAIL ' + n + '  -- raised ' + (e && e.name) + ': ' + (e && e.message)) } }
let m = {}
try { m = await import(pathToFileURL(join(D, 'cg_autorecall.js')).href) } catch (e) { res.push(false); console.log('  FAIL module_loads  -- ' + (e && e.message)) }
const F = '`'.repeat(3)
let seed = 7
const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed }
const W = ['amber', 'basin', 'cedar', 'delta', 'ember', 'fjord', 'grove', 'harbor', 'island', 'juniper']
const doc = () => Array.from({ length: 100 }, (_, i) => `L${String(i + 1).padStart(3, '0')} ${W[rnd() % 10]} the ${W[rnd() % 10]} near the ${W[rnd() % 10]}; code ${W[rnd() % 10].slice(0, 3).toUpperCase()}${100 + rnd() % 900}.`)
const docs = {}
const nodes = []
for (let k = 1; k <= 8; k++) {
  docs[k] = doc()
  nodes.push({ seq: 4 * k, message: { role: 'user', content: [{ type: 'text', text: `Reading task ${k}. For reference: the release codename for project P${k} is WORD-${100 + k}; and the release codename for project Q${k} is NAME-${200 + k}. Below is lines 1-100 of the read-only file doc${String(k).padStart(2, '0')}.txt. Reply with one sentence.\n${F}\n${docs[k].join('\n')}\n${F}` }] } })
  nodes.push({ seq: 4 * k + 1, message: { role: 'assistant', content: [{ type: 'text', text: `It lists 100 lines. ${docs[k][32]} ${docs[k][41]}` }] } })
}
const ask = 'Final check, part 2. Reply with ONLY a JSON object giving the code at the end of these lines from the earlier reading tasks: {"doc01 L017": ?, "doc02 L058": ?, "doc04 L081": ?}. Use null for any you cannot recall; do not guess.'
const live = 'context-guardian checkpoint: the user ran 8 reading tasks over doc01..doc08 and pinned 16 codenames.\n' + docs[7].slice(0, 5).join('\n')
safe('exports', () => ['extractNames', 'linePairs', 'staleRecall', 'selftest'].every((k) => typeof m[k] === 'function') && m.RECALL_TAG === '[context-guardian recall]' && Array.isArray(m.STOP) && m.STOP.includes('JSON') || 'exports')
safe('bench_prompt_48_recalls_the_three_lines', () => {
  const r = m.staleRecall({ userText: ask, liveText: live, nodes })
  const want = [docs[1][16], docs[2][57], docs[4][80]]
  return (r.startsWith('[context-guardian recall] from earlier in this session (compacted out of view):\n') && want.every((w) => r.includes(w)) && r.split('\n').length === 4) || r
})
safe('hit_lines_name_doc_line_and_seq', () => { const r = m.staleRecall({ userText: ask, liveText: live, nodes }); return r.split('\n')[1].startsWith('- doc01 L017 (seq 4): L017 ') || r })
safe('line_already_live_is_skipped', () => { const r = m.staleRecall({ userText: ask, liveText: live + '\n' + docs[2][57], nodes }); return (!r.includes(docs[2][57]) && r.includes(docs[1][16])) || r })
safe('codename_name_recall', () => { const r = m.staleRecall({ userText: 'what is WORD-103 for?', liveText: live, nodes }); return (r.includes('WORD-103 (seq 12):') && r.includes('project P3')) || r })
safe('stoplist_and_live_names_not_recalled', () => m.staleRecall({ userText: 'send JSON with WORD-107', liveText: 'P7 is WORD-107', nodes }) === '')
safe('caps_hits_3_and_chars_600', () => {
  const r = m.staleRecall({ userText: 'list WORD-101 WORD-102 WORD-103 WORD-104 WORD-105', liveText: '', nodes, maxHits: 3, maxChars: 600 })
  const body = r.split('\n').slice(1)
  return (body.length === 3 && body.every((l) => l.length <= 260)) || r
})
safe('tight_char_budget_drops_hits', () => { const r = m.staleRecall({ userText: ask, liveText: live, nodes, maxChars: 60 }); return (r.split('\n').length <= 2) || r })
safe('names_order_unique_cap_12', () => { const n = m.extractNames('Alpha AlphaBeta "quoted thing" BETA-9 doc12 L001 AlphaBeta ' + Array.from({ length: 20 }, (_, i) => 'TOK' + i).join(' ')); return (n[0] === 'AlphaBeta' && n[1] === 'quoted thing' && n[2] === 'BETA-9' && n[3] === 'doc12' && n[4] === 'L001' && n.length === 12 && new Set(n).size === 12) || JSON.stringify(n) })
safe('pairs_dedup_and_case', () => JSON.stringify(m.linePairs('DOC03 l009 and doc03 L009 and doc5 L100')) === JSON.stringify([{ doc: 'DOC03', line: 'L009' }, { doc: 'doc5', line: 'L100' }]) || JSON.stringify(m.linePairs('DOC03 l009 and doc03 L009 and doc5 L100')))
safe('garbage_inputs_never_throw', () => m.staleRecall({ userText: null, liveText: undefined, nodes: [null, {}, { seq: 1 }] }) === '' && m.extractNames(undefined).length === 0 && m.linePairs(null).length === 0)
safe('own_selftest_8_green', () => {
  const st = spawnSync(process.execPath, [join(D, 'cg_autorecall.js'), '--selftest'], { encoding: 'utf8', cwd: D, timeout: 60000 })
  const out = (st.stdout || '') + (st.stderr || ''); const line = out.split(/\r?\n/).filter((l) => l.startsWith('cg_autorecall selftest:')).pop() || ''
  const n = parseInt((line.split(':')[1] || '').trim().split(' ')[0], 10); const ok = st.status === 0 && line.endsWith(' 0 failed') && n >= 8
  if (!ok) out.split(/\r?\n/).filter((l) => /FAIL|Error|selftest:/.test(l)).slice(-8).forEach((l) => console.log('    selftest> ' + l.slice(0, 200)))
  return ok || 'own selftest'
})
safe('import_prints_nothing', () => { const r = spawnSync(process.execPath, ['--input-type=module', '-e', `import(${JSON.stringify(pathToFileURL(join(D, 'cg_autorecall.js')).href)})`], { encoding: 'utf8', cwd: D }); return (r.status === 0 && (r.stdout || '').trim() === '') || r.stdout })
const p = res.filter(Boolean).length
console.log(`autorecall_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
