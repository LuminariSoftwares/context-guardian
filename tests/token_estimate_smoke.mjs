// Every chars->tokens estimate in the package must use ONE constant. cg_recall.js used chars/4 while
// cg_memory.js, engine.js and context_guardian.py used 3.5, so the same text was "worth" different
// amounts depending on which module measured it (recall budget vs checkpoint cap vs request fit).
// usage: node tests/token_estimate_smoke.mjs      (exit 0 iff "0 failed")
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as recall from '../cg_recall.js'
import * as memory from '../cg_memory.js'
import * as engine from '../engine.js'

const here = dirname(fileURLToPath(import.meta.url))
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }

check('shared_constant_exported', recall.CHARS_PER_TOKEN === 3.5, String(recall.CHARS_PER_TOKEN))

const lengths = [1, 4, 7, 35, 100, 1001]
const rows = lengths.map(n => {
  const s = 'x'.repeat(n)
  return { n, recall: recall.estTokens(s), memory: memory.estTokens(s), engine: engine.estRequestTokens(s), want: Math.ceil(n / 3.5) }
})
check('recall_estimate_matches_the_rest', rows.every(r => r.recall === r.want), JSON.stringify(rows))
check('memory_and_engine_agree', rows.every(r => r.memory === r.want && r.engine === r.want), JSON.stringify(rows))

// The Python proxy's default must be the same number.
const py = readFileSync(join(here, '..', 'context_guardian.py'), 'utf8')
const m = py.match(/GUARDIAN_CHARS_PER_TOKEN",\s*"([0-9.]+)"/)
check('python_default_matches', m !== null && Number(m[1]) === recall.CHARS_PER_TOKEN, m ? m[1] : 'not found')

// A single oversized node is cut to the budget in characters at the SAME ratio, so the cut text fits.
const big = [{ seq: 1, message: { role: 'user', content: [{ type: 'text', text: 'y'.repeat(5000) }] } }, { seq: 2, message: { role: 'user', content: [{ type: 'text', text: 'z' }] } }]
const r = recall.recall(big, { ok: true, type: 'seq', from: 1, to: 2 }, { maxTokens: 200 })
const body = r.text.split('\n[recall truncated')[0]
check('truncation_uses_the_same_ratio', r.truncated && body.length === Math.floor(200 * 3.5), `${body.length}`)

// The contract the module is written against says the same.
const contract = readFileSync(join(here, '..', 'docs', 'contracts', 'CONTRACT_cg_recall.md'), 'utf8')
check('contract_documents_3_5', contract.includes('CHARS_PER_TOKEN') && !/length \/ 4\b/.test(contract) && !/maxTokens\*4/.test(contract))

const p = res.filter(Boolean).length
console.log(`token_estimate_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
