// Contract probe for cg_provenance.js (C-Pin provenance), written from the contract before the module, 2026-10-03.
// Reference-checked before the module was written. Usage: node pins_provenance_smoke.mjs <dir>
import { spawnSync } from 'node:child_process'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
const D = resolve(process.argv[2] || '.')
const res = []
const check = (name, cond) => { res.push(Boolean(cond)); console.log((cond ? '  ok   ' : '  FAIL ') + name) }
const safe = (name, fn) => { try { check(name, fn()) } catch (e) { res.push(false); console.log('  FAIL ' + name + '  -- raised ' + (e && e.name) + ': ' + (e && e.message)) } }
let m = {}
try { m = await import(pathToFileURL(join(D, 'cg_provenance.js')).href) } catch (e) { console.log('  FAIL module_loads  -- ' + (e && e.message)) ; res.push(false) }
const CUES = ['Please remember these for later', 'For reference', 'Important, keep this for later in the conversation']
safe('exports', () => ['extractFacts', 'pinKey', 'applyProvenance', 'renderPinText', 'selftest'].every((k) => typeof m[k] === 'function') && JSON.stringify(m.ATTRIBUTES) === JSON.stringify(['codename', 'code', 'owner', 'version', 'port', 'path', 'name']))
safe('every_cue_plant_two_facts', () => CUES.every((c) => {
  const f = m.extractFacts(`Reading task 1. ${c}: the release codename for project Heron is MAPLE-123; and the release codename for project Kite is RIVER-456. Below is lines 1-100 of the read-only file doc01.txt.`)
  return f.length === 2 && f[0].key === 'heron codename' && f[0].value === 'MAPLE-123' && f[1].key === 'kite codename' && f[1].value === 'RIVER-456'
}))
safe('update_forms_p1_now_and_p2', () => {
  const a = m.extractFacts('For reference: the release codename for project Heron is now CEDAR-789.')
  const b = m.extractFacts("Important, keep this for later in the conversation: Update: project Heron's release codename is now CEDAR-789.")
  const c = m.extractFacts("Heron's owner is Dana.")
  return a.length === 1 && a[0].value === 'CEDAR-789' && b.length === 1 && b[0].key === 'heron codename' && b[0].value === 'CEDAR-789' && c[0].key === 'heron owner' && c[0].value === 'Dana'
})
safe('each_attribute_and_subject_lowercased', () => m.ATTRIBUTES.every((a) => { const f = m.extractFacts(`the ${a} for project OwlNest is X1`); return f.length === 1 && f[0].key === 'owlnest ' + a && f[0].value === 'X1' }))
safe('pinKey_first_fact_or_null', () => { const a = m.pinKey('the port for svc is 8790; the path for svc is logs'); const b = m.pinKey('keep this for later: I like tea'); return a.key === 'svc port' && a.value === '8790' && b.key === null && b.value === null })
const plant = (cue, p1, v1, p2, v2, seq) => ({ text: `${cue}: the release codename for project ${p1} is ${v1}; and the release codename for project ${p2} is ${v2}.`, seq, cat: 'pins' })
const upd = (cue, p, v, seq) => ({ text: `${cue}: the release codename for project ${p} is now ${v}.`, seq, cat: 'pins' })
safe('variant_R_three_updates_scores_current', () => {
  const pins = [plant(CUES[2], 'Heron', 'MAPLE-123', 'Kite', 'RIVER-456', 3), plant(CUES[1], 'Owl', 'STONE-111', 'Wren', 'FERN-222', 7), plant(CUES[0], 'Lark', 'DUNE-333', 'Swan', 'MOSS-444', 11),
    upd(CUES[2], 'Heron', 'CEDAR-789', 30), upd(CUES[1], 'Wren', 'PINE-555', 34), upd(CUES[0], 'Swan', 'REED-666', 38)]
  const r = m.applyProvenance(pins)
  const shown = r.filter((p) => !p.superseded).map((p) => m.renderPinText(p)).join('\n')
  const current = ['CEDAR-789', 'RIVER-456', 'STONE-111', 'PINE-555', 'DUNE-333', 'REED-666']
  return current.every((v) => shown.includes(v)) && !['MAPLE-123', 'FERN-222', 'MOSS-444'].some((v) => shown.includes(v)) && r.slice(0, 3).every((p) => p.superseded === false)
})
safe('full_supersede_fields', () => { const r = m.applyProvenance([upd('For reference', 'Owl', 'A-1', 4), upd('For reference', 'Owl', 'B-2', 12), upd('For reference', 'Owl', 'C-3', 20)]); return r[0].superseded && r[0].supersededBy === 12 && r[1].superseded && r[1].supersededBy === 20 && !r[2].superseded && r[2].supersededBy === null && r[2].key === 'owl codename' && r[2].value === 'C-3' })
safe('restate_same_value_any_case_keeps', () => !m.applyProvenance([upd('x', 'Owl', 'A-1', 4), upd('x', 'Owl', 'a-1', 12)])[0].superseded)
safe('unkeyed_pin_never_retired', () => { const r = m.applyProvenance([{ text: 'keep this for later: the build is green', seq: 1 }, { text: 'keep this for later: the build is red', seq: 2 }]); return r.every((p) => p.superseded === false && p.key === null && p.facts.length === 0) })
safe('render_unchanged_when_current', () => { const p = m.applyProvenance([plant(CUES[0], 'A', 'X-1', 'B', 'Y-2', 1)])[0]; return m.renderPinText(p) === p.text })
safe('input_untouched_and_extra_fields_kept', () => { const pins = [Object.assign(plant(CUES[1], 'A', 'X-1', 'B', 'Y-2', 1), { session: 's1' })]; const before = JSON.stringify(pins); const r = m.applyProvenance(pins); return JSON.stringify(pins) === before && r[0].session === 's1' && r[0].cat === 'pins' })
safe('bad_input_no_throw', () => Array.isArray(m.applyProvenance(null)) && m.extractFacts(undefined).length === 0 && m.renderPinText({}) === '')
safe('own_selftest_10_green', () => {
  const st = spawnSync(process.execPath, [join(D, 'cg_provenance.js'), '--selftest'], { encoding: 'utf8', cwd: D, timeout: 60000 })
  const out = (st.stdout || '') + (st.stderr || '')
  const line = out.split(/\r?\n/).filter((l) => l.startsWith('cg_provenance selftest:')).pop() || ''
  const n = parseInt((line.split(':')[1] || '').trim().split(' ')[0], 10)
  const ok = st.status === 0 && line.endsWith(' 0 failed') && n >= 10
  if (!ok) out.split(/\r?\n/).filter((l) => /FAIL|Error|selftest:/.test(l)).slice(-8).forEach((l) => console.log('    selftest> ' + l.slice(0, 200)))
  return ok
})
safe('import_has_no_side_effect_output', () => { const r = spawnSync(process.execPath, ['--input-type=module', '-e', `import(${JSON.stringify(pathToFileURL(join(D, 'cg_provenance.js')).href)})`], { encoding: 'utf8', cwd: D }); return r.status === 0 && (r.stdout || '').trim() === '' })
const p = res.filter(Boolean).length
console.log(`pins_provenance_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
