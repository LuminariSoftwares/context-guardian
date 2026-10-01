// Regression for bench1001 (2026-10-01): user-declared "keep this for later" facts must be pinned, scoped to their
// session, rendered first, survive the checkpoint cap, and the idle trigger must be off by default.
import assert from 'node:assert/strict'
import { extractMemory, mergeMemory, emptyMemory, renderMemory } from '../cg_memory.js'
import { DEFAULTS, capSummaryBlocks, RECOVERY_NOTE } from '../engine.js'

let n = 0
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1 }
const node = (seq, text, role = 'user') => ({ seq, message: { role, content: [{ type: 'text', text }] } })

const bench = 'Reading task 1. Important, keep this for later in the conversation: the release codename for project Heron is TUNNEL-611; and the release codename for project Basalt is BASKET-229. Below is lines 1-100 of the read-only file doc01.txt.'
const other = 'Please remember this: the staging database listens on port 6543 and the user is svc_reader.'
const noise = 'This part is important to the overall design, so read it carefully.'
const items = extractMemory([node(1, bench), node(2, other), node(3, noise), node(4, 'keep this for later: X-1', 'assistant')], { session: 'session-A' })
const pins = items.filter(it => it.cat === 'pins')
ok(pins.some(it => it.text.includes('TUNNEL-611') && it.text.includes('BASKET-229')), 'bench phrasing pinned with both facts')
ok(pins.some(it => it.text.includes('6543')), 'a different phrasing ("remember this") is pinned too')
ok(!pins.some(it => it.text.includes('overall design')), 'a line that merely says "important" is not a pin')
ok(!pins.some(it => it.text.includes('X-1')), 'only USER lines are pinned')
ok(pins.every(it => !it.text.startsWith('Reading task')), 'the pin starts at the sentence holding the request')

const mem = mergeMemory(emptyMemory(), items, { now: '2026-01-01T00:00:00Z' })
const a = renderMemory(mem, { session: 'session-A', maxTokens: 1200 })
ok(a.includes('TUNNEL-611') && a.includes('6543'), 'rendered for its own session')
ok(a.indexOf('pinned by the user') !== -1 && a.indexOf('pinned by the user') < (a.indexOf('constraints:') === -1 ? Infinity : a.indexOf('constraints:')), 'pins render first')
const b = renderMemory(mem, { session: 'session-B', maxTokens: 1200 })
ok(!b.includes('TUNNEL-611'), 'no leak into another session')
const merged = mergeMemory(mem, extractMemory([node(9, bench)], { session: 'session-B' }), { now: '2026-01-02T00:00:00Z' })
ok(merged.items.filter(it => it.cat === 'pins' && it.text.includes('TUNNEL-611')).length === 2, 'the same pin in two sessions is two items')

const capped = capSummaryBlocks([{ type: 'text', text: 'PIN: TUNNEL-611' }, { type: 'text', text: RECOVERY_NOTE }, { type: 'text', text: 'x'.repeat(100000) }], 300)
ok(capped[0].text.includes('TUNNEL-611') && capped[1].text === RECOVERY_NOTE, 'cap keeps the leading blocks')
ok(capped.map(x => x.text).join('').length <= 300 * 3.5 + 120, 'cap truncates the rest')
ok(DEFAULTS.idleCompactRatio === 0 && DEFAULTS.idleCooldownMs > 0, 'idle trigger off by default, cooldown set')
console.log(`pins_smoke: ${n} checks, ${n} passed`)
