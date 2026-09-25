/**
 * Plain-node tests for the guardians' once-a-day update notice.
 * No framework, no network: every fetch is a fake, every cache file is in a temp dir.
 * Run: node tests/update_check.test.mjs   (exit 0 = all passed)
 */
import { readFile, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compareVersions, pickUpdate, noticeLine, checkForUpdate } from '../update_check.js'

let passed = 0
let failed = 0

function check(label, ok, detail = '') {
  if (ok) {
    passed += 1
  } else {
    failed += 1
    console.error(`FAIL: ${label}${detail === '' ? '' : ` -- ${detail}`}`)
  }
}

const eq = (label, actual, expected) =>
  check(label, actual === expected, `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)

const HOUR = 60 * 60 * 1000
const PKG = 'dsh-context-guardian'
const CURRENT = '0.1.0-alpha.4'
const TMP = await mkdtemp(join(tmpdir(), 'dsh-update-check-'))
const cacheFile = (name) => join(TMP, name)

/** A fetch that records whether it was called, and answers with a fixed body. */
function fakeFetch(body, { status = 200, ok = status >= 200 && status < 300 } = {}) {
  const calls = []
  const impl = async (url, options) => {
    calls.push({ url, options })
    if (body instanceof Error) throw body
    return { ok, status, json: async () => body }
  }
  impl.calls = calls
  return impl
}

try {
  // ── compareVersions: the full precedence chain, equality, and garbage ─────────
  const CHAIN = [
    '0.1.0-alpha.4',
    '0.1.0-alpha.10',
    '0.1.0',
    '0.1.1',
  ]
  check('chain is ordered', compareVersions(CHAIN[0], CHAIN[1]) < 0 && compareVersions(CHAIN[1], CHAIN[2]) < 0 && compareVersions(CHAIN[2], CHAIN[3]) < 0, 'sanity')
  for (let i = 0; i < CHAIN.length; i += 1) {
    for (let j = 0; j < CHAIN.length; j += 1) {
      const expected = i < j ? -1 : i > j ? 1 : 0
      eq(`compare ${CHAIN[i]} vs ${CHAIN[j]}`, compareVersions(CHAIN[i], CHAIN[j]), expected)
      eq(`compare ${CHAIN[j]} vs ${CHAIN[i]} (symmetric)`, compareVersions(CHAIN[j], CHAIN[i]), -expected)
    }
  }
  eq('equal versions', compareVersions('1.2.3', '1.2.3'), 0)
  eq('build metadata ignored (equal)', compareVersions('1.2.3+build.1', '1.2.3+build.2'), 0)
  eq('major wins', compareVersions('2.0.0', '1.9.9'), 1)
  eq('minor wins', compareVersions('1.10.0', '1.9.0'), 1)
  eq('patch wins', compareVersions('1.0.10', '1.0.9'), 1)
  eq('numeric prerelease id by value', compareVersions('1.0.0-2', '1.0.0-10'), -1)
  eq('numeric beats alphanumeric', compareVersions('1.0.0-1', '1.0.0-alpha'), -1)
  eq('alphanumeric by ASCII', compareVersions('1.0.0-alpha', '1.0.0-beta'), -1)
  eq('fewer identifiers is lower', compareVersions('1.0.0-alpha', '1.0.0-alpha.1'), -1)
  eq('invalid input is 0 (a)', compareVersions('not-a-version', '1.0.0'), 0)
  eq('invalid input is 0 (b)', compareVersions('1.0.0', ''), 0)
  eq('invalid input is 0 (c)', compareVersions('1.0', '1.0.0'), 0)
  eq('invalid input is 0 (d)', compareVersions(undefined, null), 0)
  eq('invalid input is 0 (e)', compareVersions('01.0.0', '1.0.0'), 0)

  // ── pickUpdate ──────────────────────────────────────────────────────────────
  eq('alpha user sees a newer alpha', pickUpdate('0.1.0-alpha.4', { latest: '0.1.0-alpha.4', alpha: '0.1.0-alpha.9' }), '0.1.0-alpha.9')
  eq('alpha user sees a newer stable', pickUpdate('0.1.0-alpha.4', { latest: '0.1.0', alpha: '0.1.0-alpha.4' }), '0.1.0')
  eq('alpha user gets the highest of both', pickUpdate('0.1.0-alpha.4', { latest: '0.1.0', alpha: '0.1.0-alpha.10' }), '0.1.0')
  eq('stable user is not shown an alpha', pickUpdate('0.1.0', { latest: '0.1.0', alpha: '0.1.1-alpha.1' }), null)
  eq('stable user gets the newer stable', pickUpdate('0.1.0', { latest: '0.1.1', alpha: '0.2.0-alpha.1' }), '0.1.1')
  eq('nothing newer -> null', pickUpdate('1.2.3', { latest: '1.2.3' }), null)
  eq('empty dist-tags -> null', pickUpdate('1.2.3', {}), null)
  eq('junk dist-tags -> null', pickUpdate('1.2.3', { latest: 'nope' }), null)
  eq('garbage dist-tags object -> null', pickUpdate('1.2.3', null), null)

  // ── noticeLine, byte for byte ───────────────────────────────────────────────
  eq('noticeLine exact', noticeLine('dsh-tool-guardian', '0.3.0-alpha.2', '0.3.1'),
    'dsh-tool-guardian: 0.3.1 is available (you have 0.3.0-alpha.2). Update: dsh plugin update dsh-tool-guardian  ·  silence: GUARDIAN_NO_UPDATE_CHECK=1')

  // ── checkForUpdate: opt-out short-circuits before any fetch ──────────────────
  {
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: cacheFile('optout.json'),
      env: { GUARDIAN_NO_UPDATE_CHECK: '1' }, fetchImpl, log: () => {},
    })
    eq('GUARDIAN_NO_UPDATE_CHECK=1 -> null', result, null)
    eq('GUARDIAN_NO_UPDATE_CHECK=1 -> no fetch', fetchImpl.calls.length, 0)
  }
  for (const [key, value] of [['NO_UPDATE_NOTIFIER', 'yes'], ['CI', 'true']]) {
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: cacheFile('optout.json'),
      env: { [key]: value }, fetchImpl, log: () => {},
    })
    eq(`${key}=${value} -> null`, result, null)
    eq(`${key}=${value} -> no fetch`, fetchImpl.calls.length, 0)
  }
  {
    // "0" and "false" are the explicit ways to say no to the opt-out, not opt-outs.
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: cacheFile('opt0.json'),
      env: { GUARDIAN_NO_UPDATE_CHECK: '0', CI: 'false' }, fetchImpl, log: () => {},
    })
    eq('opt-out "0"/"false" still checks', fetchImpl.calls.length, 1)
    check('opt-out "0"/"false" still notices', typeof result === 'string')
  }

  // ── checkForUpdate: a fresh cache answers without the network ───────────────
  {
    const file = cacheFile('fresh.json')
    const nowMs = 1_700_000_000_000
    await writeFile(file, JSON.stringify({ checkedAt: nowMs - HOUR, next: '0.1.0-alpha.9' }))
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const logged = []
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, now: () => nowMs, log: (line) => logged.push(line),
    })
    eq('fresh cache -> no fetch', fetchImpl.calls.length, 0)
    eq('fresh cache -> logs once', logged.length, 1)
    eq('fresh cache -> returns the cached notice', result, noticeLine(PKG, CURRENT, '0.1.0-alpha.9'))
  }
  {
    // Fresh, but nothing newer in it: still no network, still silent.
    const file = cacheFile('fresh-null.json')
    const nowMs = 1_700_000_000_000
    await writeFile(file, JSON.stringify({ checkedAt: nowMs - HOUR, next: null }))
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const logged = []
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, now: () => nowMs, log: (line) => logged.push(line),
    })
    eq('fresh null cache -> null', result, null)
    eq('fresh null cache -> no fetch', fetchImpl.calls.length, 0)
    eq('fresh null cache -> silent', logged.length, 0)
  }
  {
    // A cached "next" the user has since passed is not news.
    const file = cacheFile('fresh-old.json')
    const nowMs = 1_700_000_000_000
    await writeFile(file, JSON.stringify({ checkedAt: nowMs - HOUR, next: '0.0.9' }))
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, now: () => nowMs, log: () => {},
    })
    eq('cached older version -> null, no fetch', result, null)
    eq('cached older version -> no fetch', fetchImpl.calls.length, 0)
  }

  // ── checkForUpdate: a stale cache is refetched and rewritten ────────────────
  {
    const file = cacheFile('stale.json')
    const nowMs = 1_700_000_000_000
    await writeFile(file, JSON.stringify({ checkedAt: nowMs - 25 * HOUR, next: '0.1.0-alpha.9' }))
    const fetchImpl = fakeFetch({ latest: '0.1.0', alpha: '0.1.0-alpha.9' })
    const logged = []
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, now: () => nowMs, log: (line) => logged.push(line),
    })
    eq('stale cache -> fetch called once', fetchImpl.calls.length, 1)
    eq('stale cache -> dist-tags URL', fetchImpl.calls[0].url, 'https://registry.npmjs.org/-/package/dsh-context-guardian/dist-tags')
    eq('stale cache -> accept header', fetchImpl.calls[0].options.headers.accept, 'application/json')
    check('stale cache -> carries a timeout signal', fetchImpl.calls[0].options.signal instanceof AbortSignal)
    eq('stale cache -> highest newer wins', result, noticeLine(PKG, CURRENT, '0.1.0'))
    eq('stale cache -> logs once', logged.length, 1)
    const rewritten = JSON.parse(await readFile(file, 'utf8'))
    eq('stale cache -> rewritten checkedAt', rewritten.checkedAt, nowMs)
    eq('stale cache -> rewritten next', rewritten.next, '0.1.0')
  }

  // ── checkForUpdate: every failure is silent and writes nothing ──────────────
  {
    const file = cacheFile('throws.json')
    const fetchImpl = fakeFetch(new Error('network is down'))
    const logged = []
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, log: (line) => logged.push(line),
    })
    eq('fetch throws -> null', result, null)
    eq('fetch throws -> silent', logged.length, 0)
    let wrote = true
    try { await readFile(file, 'utf8') } catch { wrote = false }
    check('fetch throws -> no cache write', wrote === false)
  }
  {
    const file = cacheFile('notfound.json')
    const fetchImpl = fakeFetch({ error: 'not found' }, { status: 404, ok: false })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, log: () => {},
    })
    eq('non-2xx -> null', result, null)
    let wrote = true
    try { await readFile(file, 'utf8') } catch { wrote = false }
    check('non-2xx -> no cache write', wrote === false)
  }
  {
    const file = cacheFile('badjson.json')
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url, options })
      return { ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <') } }
    }
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, log: () => {},
    })
    eq('bad JSON -> null', result, null)
    let wrote = true
    try { await readFile(file, 'utf8') } catch { wrote = false }
    check('bad JSON -> no cache write', wrote === false)
  }
  {
    // An unwritable cache directory is a cache miss, not a crash.
    const file = join(cacheFile('nope'), 'denied', 'cache.json')
    const fetchImpl = fakeFetch({ latest: '9.9.9' })
    const result = await checkForUpdate({
      pkg: PKG, current: CURRENT, cacheFile: file,
      env: {}, fetchImpl, log: () => {},
    })
    check('unwritable cache still returns a notice', typeof result === 'string')
  }

  // ── the two shipped copies must not drift ───────────────────────────────────
  {
    // In the repo: compare with the sibling tool-guardian checkout when it sits next to this one (skipped otherwise).
    const cg = await readFile(fileURLToPath(new URL('../update_check.js', import.meta.url)), 'utf8')
    const tg = await readFile(fileURLToPath(new URL('../../tool-guardian/update_check.js', import.meta.url)), 'utf8').catch(() => null)
    if (tg !== null) check('context-guardian and tool-guardian update_check.js are byte-identical', cg === tg, `cg ${cg.length} bytes, tg ${tg.length} bytes`)
  }
} finally {
  await rm(TMP, { recursive: true, force: true })
}

console.log(`update_check: ${passed + failed} checks, ${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
