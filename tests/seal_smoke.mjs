// Contract smoke for cg_seal.js: the DSH engine's half of the sealed archive.
// The chain it writes must be byte-for-byte the format cg_archive.py verifies, so the
// canonical form is pinned to the SAME golden hash tests/test_seal.py pins.
// usage: node tests/seal_smoke.mjs      (exit 0 iff "0 failed")
import { createHash } from 'node:crypto'
import { existsSync, mkdtempSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as seal from '../cg_seal.js'

const tmp = mkdtempSync(join(tmpdir(), 'cg-seal-'))
let passed = 0; let failed = 0
const check = (name, fn) => {
  try { if (fn()) { passed += 1; console.log(`ok   ${name}`) } else { failed += 1; console.log(`FAIL ${name}`) } } catch (error) { failed += 1; console.log(`FAIL ${name}: ${error.stack}`) }
}
const chain = (dir) => readFileSync(join(dir, 'chain.jsonl'), 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line))
const sha = (buf) => createHash('sha256').update(buf).digest('hex')

check('canonical_form_matches_python', () => {
  const entry = { seq: 1, type: 'span', file: '0001.json', sha256: 'ab'.repeat(32), prev: 'genesis:20261023-120000', ts: '2026-10-23T12:00:00-05:00' }
  return seal.canonical(entry) === `{"file":"0001.json","prev":"genesis:20261023-120000","seq":1,"sha256":"${'ab'.repeat(32)}","ts":"2026-10-23T12:00:00-05:00","type":"span"}`
    && seal.entryHash(entry) === '46fc20f69dc23fcda35e4c1688428bd5f1a3e8ac5a44f3ea19d5227de02f418d'
})

check('canonical_escapes_non_ascii_like_python', () => seal.canonical({ a: 'é ' }) === '{"a":"\\u00e9\\u2028"}')

check('three_spans_chain_and_hash_the_bytes_on_disk', () => {
  const spanDir = join(tmp, 'a')
  const paths = [1, 2, 3].map(i => seal.writeSealedSpan({ spanDir, runId: 'dsh-run', startIndex: i, payload: index => ({ index, messages: [{ role: 'user', content: `m${index}` }] }) }).path)
  const run = join(spanDir, 'dsh-run')
  const entries = chain(run)
  return entries.length === 3
    && entries[0].prev === 'genesis:dsh-run'
    && entries[1].prev === entries[0].entry_hash && entries[2].prev === entries[1].entry_hash
    && entries.every((e, i) => e.seq === i + 1 && e.type === 'span' && e.sha256 === sha(readFileSync(paths[i])) && e.entry_hash === seal.entryHash(e))
    && !existsSync(join(run, 'chain.lock'))
})

check('prune_is_recorded_before_the_file_goes', () => {
  const spanDir = join(tmp, 'b')
  for (const i of [1, 2, 3]) seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: i, payload: index => ({ index }) })
  seal.pruneSealed({ spanDir, keepSpans: 2 })
  const entries = chain(join(spanDir, 'r'))
  return !existsSync(join(spanDir, 'r', '0001.json')) && entries.at(-1).type === 'pruned' && entries.at(-1).file === '0001.json' && entries.at(-1).seq === 4
})

check('anchor_gets_every_head', () => {
  const spanDir = join(tmp, 'c'); const anchorPath = join(tmp, 'elsewhere', 'anchor.jsonl')
  for (const i of [1, 2]) seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: i, anchorPath, payload: index => ({ index }) })
  const heads = readFileSync(anchorPath, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line))
  const entries = chain(join(spanDir, 'r'))
  return heads.length === 2 && heads[1].head === entries[1].entry_hash && heads[1].run === 'r' && heads[1].seq === 2
})

check('a_seal_that_cannot_be_written_still_returns_the_span', () => {
  const spanDir = join(tmp, 'd'); mkdirSync(join(spanDir, 'r', 'chain.jsonl'), { recursive: true })
  const warnings = []
  const out = seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: 1, payload: index => ({ index }), warn: m => warnings.push(m) })
  return out.path !== null && existsSync(out.path) && out.sealed === false && warnings.some(m => m.includes('seal'))
})

check('seal_off_writes_no_chain', () => {
  const spanDir = join(tmp, 'e')
  const out = seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: 1, seal: false, payload: index => ({ index }) })
  return existsSync(out.path) && !existsSync(join(spanDir, 'r', 'chain.jsonl'))
})

check('a_failed_write_leaves_no_file_and_frees_the_index', () => {
  const spanDir = join(tmp, 'f')
  let threw = false
  try { seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: 1, payload: () => { throw new Error('boom') } }) } catch { threw = true }
  const again = seal.writeSealedSpan({ spanDir, runId: 'r', startIndex: 1, payload: index => ({ index }) })
  return threw && again.path.endsWith('0001.json')
})

writeFileSync(join(tmp, '.done'), '')
console.log(`seal_smoke: ${passed + failed} checks, ${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
