// The /guardian/health dashboard's "Estimate" card: the learned calibration factor per model and the
// keep-recent mode, both already in /guardian/stats. Runs the dashboard's OWN card code, extracted from
// context_guardian.py, against sample /stats payloads -- no browser, no server.
// usage: node tests/health_estimate_smoke.mjs      (exit 0 iff "0 failed")
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const py = readFileSync(join(here, '..', 'context_guardian.py'), 'utf8')
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }

const script = (py.match(/<script>([\s\S]*?)<\/script>/) || [])[1] || ''
// Only the pure helpers: everything from `const fmt` up to the first DOM-touching function.
const fn = (name) => (script.match(new RegExp(`function ${name}\\([^)]*\\)\\{[\\s\\S]*?\\n\\}`)) || [])[0] || ''
const helpers = ['esc', 'card', 'estimateCard'].map(fn)
check('estimate_card_function_exists', helpers.every(Boolean), helpers.map(h => h.length).join(','))
let estimateCard = () => ''
try {
  estimateCard = new Function(`const fmt=n=>n==null?"-":Number(n).toLocaleString();\n${helpers.join('\n')}\nreturn estimateCard`)()
} catch (e) { check('estimate_card_compiles', false, e.message) }

const stats = {
  keep_recent_label: 'budget 20%', calibration_enabled: true, calibration_min_samples: 5,
  calibration: { 'qwen3:14b': { factor: 1.1234, samples: 12 }, 'gpt-oss:20b': { factor: 1.0, samples: 2 } },
}
const html = String(estimateCard(stats))
check('card_titled_estimate', html.includes('<h2>Estimate</h2>'), html)
check('shows_factor_per_model', html.includes('qwen3:14b') && html.includes('1.12') && html.includes('12 samples'), html)
check('shows_learning_model_as_learning', html.includes('gpt-oss:20b') && /learning[^<]*2\/5/.test(html), html)
check('shows_keep_recent_mode', html.includes('keep recent: budget 20%'), html)
const explicit = String(estimateCard({ ...stats, keep_recent_label: '6 messages (explicit)' }))
check('shows_explicit_keep_recent', explicit.includes('keep recent: 6 messages (explicit)'), explicit)
const none = String(estimateCard({ keep_recent_label: 'budget 20%', calibration_enabled: true, calibration: {} }))
check('no_models_yet', none.includes('no usage reported yet'), none)
const off = String(estimateCard({ ...stats, calibration_enabled: false }))
check('calibration_off_says_so', off.includes('calibration off'), off)
const evil = String(estimateCard({ ...stats, calibration: { '<img src=x onerror=alert(1)>': { factor: 1, samples: 9 } } }))
check('model_name_is_escaped', !evil.includes('<img') && evil.includes('&lt;img'), evil)
check('card_is_rendered', /card\("Rejected summaries"[\s\S]*estimateCard\(s\)/.test(script) || /estimateCard\(s\)/.test(script.split('$("cards").innerHTML')[1] || ''))

const p = res.filter(Boolean).length
console.log(`health_estimate_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
