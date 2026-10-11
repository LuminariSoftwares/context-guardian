// C-Pin provenance seam smoke (2026-10-03, written before the cg_memory.js splice -- red first):
// renderMemory never shows a pinned value a LATER pin changed, retires a pin whose every fact changed,
// keeps every pin in memory (audit), and leaves pins without keyed facts alone.
//   node tests/pins_render_smoke.mjs
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const here = dirname(fileURLToPath(import.meta.url))
const { renderMemory } = await import(pathToFileURL(join(here, '..', 'cg_memory.js')).href)
const res = []
const check = (n, c, why = '') => { res.push(Boolean(c)); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const pin = (text, seq, session = 's1') => ({ cat: 'pins', text, seq, session, first: null, last: null, count: 1, done: false })
const K = 'Important, keep this for later in the conversation'
const memory = { version: 1, updated: '', items: [
  pin(`${K}: the release codename for project Heron is MAPLE-123; and the release codename for project Kite is RIVER-456.`, 3),
  pin('For reference: the release codename for project Owl is STONE-111.', 7),
  pin('keep this for later: the build machine is gpu-box', 8),
  pin(`${K}: the release codename for project Heron is now CEDAR-789.`, 30),
  pin('For reference: the release codename for project Owl is now PINE-555.', 34),
] }
const before = JSON.stringify(memory)
const out = renderMemory(memory, { session: 's1', maxTokens: 4000 })
check('current_values_shown', ['CEDAR-789', 'RIVER-456', 'PINE-555'].every((v) => out.includes(v)), out)
check('stale_values_never_shown', !out.includes('MAPLE-123') && !out.includes('STONE-111'), out)
check('fully_superseded_pin_retired', !out.includes('project Owl is STONE'), out)
check('unkeyed_pin_kept', out.includes('the build machine is gpu-box'), out)
check('memory_untouched_for_audit', JSON.stringify(memory) === before)
const p = res.filter(Boolean).length
console.log(`pins_render_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
