// cg_seal.js — the DSH engine's half of the sealed span archive.
//
// The Python proxy seals every span it writes into <span_dir>/<run_id>/chain.jsonl (cg_archive.py), so
// `context-guardian-recall --verify` can show the archive still holds what compaction removed. The engine writes
// spans in the same format into the same kind of directory, and an unsealed engine span would read as a break to
// that verifier. So this file writes the SAME chain, byte for byte: the canonical form is pinned to one golden hash
// in tests/seal_smoke.mjs and tests/test_seal.py, and tests/test_seal_js.py verifies Node-written spans in Python.
// Format: docs/ARCHIVE_FORMAT.md.
//
// Tamper-EVIDENT, not tamper-proof: anyone who can write the directory can rehash a whole chain. GUARDIAN_SEAL_ANCHOR_PATH
// appends every new head to a file meant to live elsewhere, and the verifier checks against it.
//
// A seal that fails is logged and the span is still returned: evidence about the archive must never cost the archive.
// ESM, Node >= 20, zero third-party imports. MIT licensed.
import { createHash } from 'node:crypto'
import { chmodSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, renameSync, statSync, unlinkSync, writeSync, fstatSync } from 'node:fs'
import { dirname, join, basename } from 'node:path'

export const CHAIN_FILE = 'chain.jsonl'
export const LOCK_FILE = 'chain.lock'
export const SPAN_NAME_RE = /^\d{4,}\.json$/
const LOCK_TIMEOUT_MS = 2000
const LOCK_STALE_MS = 30000
const LOCK_POLL_MS = 5
const TAIL_BYTES = 8192
const MAX_PROBE = 50
const FILE_MODE = 0o600
const DIR_MODE = 0o700

/** Sleep without an event loop turn: the engine's span write is synchronous, and so is this lock. */
const sleepSync = (ms) => { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms) }

const escapeNonAscii = (text) => text.replace(/[\u0080-￿]/g, ch => '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0'))

/**
 * The bytes an entry is hashed over: keys sorted, no whitespace, non-ASCII escaped — exactly Python's
 * json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).
 */
export function canonical (value) {
  if (value === null || typeof value !== 'object') return escapeNonAscii(JSON.stringify(value))
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  return '{' + Object.keys(value).sort().map(key => escapeNonAscii(JSON.stringify(key)) + ':' + canonical(value[key])).join(',') + '}'
}

/** sha256 over the canonical JSON of every field except entry_hash. */
export function entryHash (fields) {
  const body = { ...fields }
  delete body.entry_hash
  return createHash('sha256').update(canonical(body), 'ascii').digest('hex')
}

/** Hex digest of a file's bytes as they are on disk. */
export function sha256File (path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

/** Local time with its UTC offset, e.g. 2026-10-23T12:00:00-05:00 (what Python's isoformat writes). */
export function nowIso (date = new Date()) {
  const pad = (n) => String(Math.trunc(Math.abs(n))).padStart(2, '0')
  const offset = -date.getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${sign}${pad(offset / 60)}:${pad(offset % 60)}`
}

const privateMkdir = (dir) => {
  const missing = []
  for (let d = dir; !existsSync(d); d = dirname(d)) { missing.push(d); if (dirname(d) === d) break }
  mkdirSync(dir, { recursive: true, mode: DIR_MODE })
  for (const made of missing) { try { chmodSync(made, DIR_MODE) } catch { /* not POSIX */ } }
}

const fsyncDir = (dir) => {
  if (process.platform === 'win32') return
  let fd
  try { fd = openSync(dir, 'r'); fsyncSync(fd) } catch { /* best effort */ } finally { if (fd !== undefined) closeSync(fd) }
}

/**
 * Run fn holding the run's chain lock: a lock FILE taken with an exclusive create, which Python (cg_archive.ChainLock)
 * takes the same way, on every OS. A lock older than LOCK_STALE_MS was left by a writer that died and is broken.
 */
export function withChainLock (runDir, fn, { timeoutMs = LOCK_TIMEOUT_MS } = {}) {
  const path = join(runDir, LOCK_FILE)
  const deadline = Date.now() + timeoutMs
  for (;;) {
    let fd
    try {
      fd = openSync(path, 'wx', FILE_MODE)
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error
      try { if (Date.now() - statSync(path).mtimeMs > LOCK_STALE_MS) unlinkSync(path) } catch { /* gone already */ }
      if (Date.now() >= deadline) throw new Error(`timed out waiting for ${path}`)
      sleepSync(LOCK_POLL_MS)
      continue
    }
    try { writeSync(fd, `${process.pid}\n`) } finally { closeSync(fd) }
    break
  }
  try { return fn() } finally { try { unlinkSync(path) } catch { /* gone already */ } }
}

function lastEntry (chainPath) {
  if (!existsSync(chainPath)) return null
  const fd = openSync(chainPath, 'r')
  let tail
  try {
    const size = fstatSync(fd).size
    if (size === 0) return null
    const start = Math.max(0, size - TAIL_BYTES)
    tail = Buffer.alloc(size - start)
    readSync(fd, tail, 0, tail.length, start)
  } finally { closeSync(fd) }
  const lines = tail.toString('utf8').split('\n').filter(line => line.trim() !== '')
  if (lines.length === 0) return null
  const last = JSON.parse(lines.at(-1))
  if (!last || !Number.isInteger(last.seq) || typeof last.entry_hash !== 'string') throw new Error(`the last line of ${chainPath} is not a chain entry`)
  return last
}

function appendLine (path, text) {
  const isNew = !existsSync(path)
  const fd = openSync(path, 'a', FILE_MODE)
  try {
    if (isNew) { try { chmodSync(path, FILE_MODE) } catch { /* not POSIX */ } }
    writeSync(fd, text)
    fsyncSync(fd)
  } finally { closeSync(fd) }
}

/** Append one entry; the caller holds the chain lock. Throws on failure. */
export function appendEntryLocked (runDir, runId, fields, anchorPath) {
  const last = lastEntry(join(runDir, CHAIN_FILE))
  const entry = { ...fields }
  for (const key of ['seq', 'prev', 'entry_hash', 'ts']) delete entry[key]
  entry.seq = last === null ? 1 : last.seq + 1
  entry.prev = last === null ? `genesis:${runId}` : last.entry_hash
  entry.ts = nowIso()
  entry.entry_hash = entryHash(entry)
  appendLine(join(runDir, CHAIN_FILE), canonical(entry) + '\n')
  if (anchorPath) {
    privateMkdir(dirname(anchorPath))
    appendLine(anchorPath, canonical({ run: runId, seq: entry.seq, head: entry.entry_hash, ts: entry.ts }) + '\n')
  }
  return entry
}

export function appendEntry (runDir, runId, fields, anchorPath) {
  return withChainLock(runDir, () => appendEntryLocked(runDir, runId, fields, anchorPath))
}

/**
 * Claim the next free NNNN.json at or after startIndex, write payload(index) to a temp file, fsync it, rename it over
 * the claim, then seal the bytes now on disk. Throws when the span itself cannot be written (the claim is removed,
 * so the index is free again); a seal that fails only warns, and `sealed` is false.
 *   -> { path, index, sealed, entry }
 */
export function writeSealedSpan ({ spanDir, runId, startIndex, payload, anchorPath = '', seal = true, warn = () => {}, afterSeal = null }) {
  const dir = join(spanDir, runId)
  privateMkdir(dir)
  let path = null; let index = startIndex
  for (let attempt = 0; attempt < MAX_PROBE; attempt += 1) {
    const candidate = join(dir, `${String(startIndex + attempt).padStart(4, '0')}.json`)
    let fd
    try { fd = openSync(candidate, 'wx', FILE_MODE) } catch (error) { if (error?.code === 'EEXIST') continue; throw error }
    closeSync(fd)
    try { chmodSync(candidate, FILE_MODE) } catch { /* not POSIX */ }
    path = candidate; index = startIndex + attempt
    break
  }
  if (path === null) throw new Error(`no free span index near ${startIndex} after ${MAX_PROBE} attempts`)
  const tmp = join(dir, `.${basename(path)}.${process.pid}.tmp`)
  try {
    const text = JSON.stringify(payload(index), null, 1)
    const fd = openSync(tmp, 'wx', FILE_MODE)
    try { writeSync(fd, text); fsyncSync(fd) } finally { closeSync(fd) }
    renameSync(tmp, path)
    fsyncDir(dir)
  } catch (error) {
    // The claimed file is empty and the temp file half-written; left in place either reads as an archived span.
    for (const leftover of [tmp, path]) { try { unlinkSync(leftover) } catch { /* not there */ } }
    throw error
  }
  if (!seal) return { path, index, sealed: false, entry: null }
  try {
    const entry = withChainLock(dir, () => {
      const sealed = appendEntryLocked(dir, runId, { type: 'span', file: basename(path), sha256: sha256File(path) }, anchorPath)
      if (typeof afterSeal === 'function') afterSeal(dir, index)
      return sealed
    })
    return { path, index, sealed: true, entry }
  } catch (error) {
    warn(`context-guardian engine: could not seal ${basename(path)} (${error.message}); the span is written, --verify will report it unsealed`)
    return { path, index, sealed: false, entry: null }
  }
}

/**
 * Keep the newest keepSpans span files across every run. In a sealed run each deletion is recorded as a chained
 * {"type": "pruned"} line FIRST; if that cannot be written the file stays, rather than leaving a gap the verifier
 * must report as missing. Housekeeping only: never throws.
 */
export function pruneSealed ({ spanDir, keepSpans, anchorPath = '', warn = () => {} }) {
  try {
    const files = []
    for (const run of readdirSync(spanDir)) {
      const dir = join(spanDir, run)
      if (!statSync(dir).isDirectory()) continue
      for (const file of readdirSync(dir)) if (/^\d+\.json$/.test(file)) files.push(join(dir, file))
    }
    files.sort()
    for (const doomed of files.slice(0, Math.max(0, files.length - keepSpans))) {
      const dir = dirname(doomed)
      if (existsSync(join(dir, CHAIN_FILE)) && SPAN_NAME_RE.test(basename(doomed))) {
        try {
          appendEntry(dir, basename(dir), { type: 'pruned', file: basename(doomed), sha256: sha256File(doomed) }, anchorPath)
        } catch (error) {
          warn(`context-guardian engine: could not record the prune of ${doomed} (${error.message}); keeping the file`)
          continue
        }
      }
      unlinkSync(doomed)
    }
  } catch { /* housekeeping only */ }
}
