// Contract probe for cg_postcheck.js (C-Recall stage b), written from the contract before the module, 2026-10-03.
// Usage: node postcheck_smoke.mjs <dir>  (dir holds cg_postcheck.js + cg_provenance.js)
import { spawnSync } from 'node:child_process'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
const D = resolve(process.argv[2] || '.')
const res = []
const check = (n, c, why = '') => { res.push(Boolean(c)); console.log((c ? '  ok   ' : '  FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const safe = (n, fn) => { try { const r = fn(); check(n, r === true, r === true ? '' : String(r).slice(0, 300)) } catch (e) { res.push(false); console.log('  FAIL ' + n + '  -- raised ' + (e && e.name) + ': ' + (e && e.message)) } }
let m = {}
try { m = await import(pathToFileURL(join(D, 'cg_postcheck.js')).href) } catch (e) { res.push(false); console.log('  FAIL module_loads  -- ' + (e && e.message)) }
const CUES = ['Please remember these for later', 'For reference', 'Important, keep this for later in the conversation']
const P = ['Heron', 'Kite', 'Owl', 'Wren', 'Lark', 'Swan']
const pins = []
P.forEach((p, i) => { if (i % 2 === 0) pins.push({ text: `${CUES[i % 3]}: the release codename for project ${p} is OLD-${i}0; and the release codename for project ${P[i + 1]} is OLD-${i + 1}0.`, seq: 4 * i + 1 }) })
pins.push({ text: `${CUES[0]}: the release codename for project Heron is now NEW-01.`, seq: 30 })
pins.push({ text: `${CUES[1]}: Update: project Wren's release codename is now NEW-03.`, seq: 34 })
safe('exports', () => ['currentFacts', 'assertions', 'staleAssertions', 'steerText', 'selftest'].every((k) => typeof m[k] === 'function') && m.STEER_TAG === '[context-guardian check]' || 'exports')
safe('current_facts_after_updates', () => { const f = m.currentFacts(pins); return (f.get('heron')[0].value === 'NEW-01' && f.get('wren')[0].value === 'NEW-03' && f.get('kite')[0].value === 'OLD-10' && f.size === 6) || JSON.stringify([...f]) })
safe('variant_R_json_answer_two_stale', () => {
  const s = m.staleAssertions('{"Heron": "OLD-00", "Kite": "OLD-10", "Owl": "OLD-20", "Wren": "OLD-30", "Lark": "OLD-40", "Swan": "OLD-50"}', pins)
  return (s.length === 2 && s[0].subject === 'heron' && s[0].current === 'NEW-01' && s[1].subject === 'wren' && s[1].said === 'OLD-30' && s[1].key === 'wren codename') || JSON.stringify(s)
})
safe('all_current_no_finding', () => m.staleAssertions('{"Heron": "NEW-01", "Kite": "OLD-10", "Wren": "new-03"}', pins).length === 0)
safe('prose_forms', () => m.staleAssertions("Heron's release codename is OLD-00. Wren is OLD-30.", pins).length === 2)
safe('null_and_words_ignored', () => m.staleAssertions('{"Heron": null, "Wren": "unknown"} Heron: see', pins).length === 0)
safe('first_assertion_per_subject_and_max_3', () => {
  const many = [{ text: 'the code for A is A-1; the code for B is B-1; the code for C is C-1; the code for D is D-1', seq: 1 }]
  const s = m.staleAssertions('A: A-9 A: A-1 B: B-9 C: C-9 D: D-9', many)
  return (s.length === 3 && s[0].said === 'A-9' && s.map((x) => x.subject).join() === 'a,b,c') || JSON.stringify(s)
})
safe('ambiguous_subject_two_current_facts_skipped', () => m.staleAssertions('Heron: X-9', [{ text: 'the owner for Heron is DANA-1; the port for Heron is 8790', seq: 1 }]).length === 0)
safe('steer_text_shape', () => {
  const t = m.steerText(m.staleAssertions('{"Heron": "OLD-00"}', pins))
  return (t.startsWith('[context-guardian check] ') && t.includes('heron codename') && t.includes('OLD-00') && t.includes('NEW-01') && t.includes('seq 30') && t.length <= 400) || t
})
safe('garbage_never_throws', () => m.staleAssertions(null, null).length === 0 && m.steerText(null) === '' && m.assertions(undefined, undefined).length === 0)
safe('own_selftest_8_green', () => {
  const st = spawnSync(process.execPath, [join(D, 'cg_postcheck.js'), '--selftest'], { encoding: 'utf8', cwd: D, timeout: 60000 })
  const out = (st.stdout || '') + (st.stderr || ''); const line = out.split(/\r?\n/).filter((l) => l.startsWith('cg_postcheck selftest:')).pop() || ''
  const n = parseInt((line.split(':')[1] || '').trim().split(' ')[0], 10); const ok = st.status === 0 && line.endsWith(' 0 failed') && n >= 8
  if (!ok) out.split(/\r?\n/).filter((l) => /FAIL|Error|selftest:/.test(l)).slice(-8).forEach((l) => console.log('    selftest> ' + l.slice(0, 200)))
  return ok || 'own selftest'
})
safe('import_prints_nothing', () => { const r = spawnSync(process.execPath, ['--input-type=module', '-e', `import(${JSON.stringify(pathToFileURL(join(D, 'cg_postcheck.js')).href)})`], { encoding: 'utf8', cwd: D }); return (r.status === 0 && (r.stdout || '').trim() === '') || r.stdout })
const p = res.filter(Boolean).length
console.log(`postcheck_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
