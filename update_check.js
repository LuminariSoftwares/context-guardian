/**
 * Once-a-day "a newer version exists" notice for the guardian plugins.
 *
 * Rules this file exists to keep:
 *   - never throws and never blocks plugin load (callers fire-and-forget it);
 *   - never reaches the network more than once a day per plugin, per machine;
 *   - never nags a stable user with a prerelease;
 *   - an opt-out env var kills it outright, and any failure is silent.
 *
 * ESM, Node 22+, no dependencies: global fetch and AbortSignal.timeout.
 * MIT licensed.
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

const DAY_MS = 24 * 60 * 60 * 1000

/** Any of these, non-empty and not 0/false, turns the check off. */
const OPT_OUT_VARS = ['GUARDIAN_NO_UPDATE_CHECK', 'NO_UPDATE_NOTIFIER', 'CI']
const OPT_OUT_OFF = new Set(['0', 'false'])

/** The official SemVer 2.0.0 grammar; leading zeroes and loose forms are invalid. */
const SEMVER_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$/

const isNumericId = (id) => /^\d+$/.test(id)

function parseVersion(value) {
  if (typeof value !== 'string') return null
  const match = SEMVER_RE.exec(value)
  if (match === null) return null
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    prerelease: match[4] === undefined ? null : match[4].split('.'),
  }
}

/** Prerelease identifiers: numeric below alphanumeric, numerics by value, text by ASCII. */
function comparePrerelease(a, b) {
  for (let i = 0; ; i += 1) {
    const left = a[i]
    const right = b[i]
    if (left === undefined && right === undefined) return 0
    if (left === undefined) return -1 // a shorter run of identifiers has lower precedence
    if (right === undefined) return 1
    const leftNumeric = isNumericId(left)
    const rightNumeric = isNumericId(right)
    if (leftNumeric && rightNumeric) {
      if (Number(left) !== Number(right)) return Number(left) < Number(right) ? -1 : 1
    } else if (leftNumeric !== rightNumeric) {
      return leftNumeric ? -1 : 1 // numeric identifiers always have lower precedence
    } else if (left !== right) {
      return left < right ? -1 : 1
    }
  }
}

/**
 * Full SemVer 2.0 precedence, build metadata ignored.
 * Anything that is not a version compares as 0, so a typo never invents an update.
 */
export function compareVersions(a, b) {
  const left = parseVersion(a)
  const right = parseVersion(b)
  if (left === null || right === null) return 0
  for (const field of ['major', 'minor', 'patch']) {
    if (left[field] !== right[field]) return left[field] < right[field] ? -1 : 1
  }
  if (left.prerelease === null && right.prerelease === null) return 0
  if (left.prerelease === null) return 1 // a release outranks any of its prereleases
  if (right.prerelease === null) return -1
  return comparePrerelease(left.prerelease, right.prerelease)
}

/**
 * The version to offer, from npm's dist-tags, or null.
 * A prerelease user may be moved to anything newer; a stable user only ever sees
 * another stable -- an alpha is not news to someone on a release.
 */
export function pickUpdate(current, distTags) {
  if (distTags === null || typeof distTags !== 'object' || Array.isArray(distTags)) return null
  const currentParsed = parseVersion(current)
  if (currentParsed === null) return null
  const currentIsPrerelease = currentParsed.prerelease !== null
  let best = null
  for (const value of Object.values(distTags)) {
    const parsed = parseVersion(value)
    if (parsed === null) continue
    if (!currentIsPrerelease && parsed.prerelease !== null) continue
    if (compareVersions(value, current) <= 0) continue
    if (best === null || compareVersions(value, best) > 0) best = value
  }
  return best
}

export function noticeLine(pkg, current, next) {
  return `${pkg}: ${next} is available (you have ${current}). Update: dsh plugin update ${pkg}  ·  silence: GUARDIAN_NO_UPDATE_CHECK=1`
}

export function defaultCacheFile(pkg) {
  return join(homedir(), '.cache', 'dsh-guardians', `${pkg}.update.json`)
}

function optedOut(env) {
  for (const key of OPT_OUT_VARS) {
    const raw = env?.[key]
    if (raw === undefined || raw === null) continue
    if (typeof raw === 'string') {
      const value = raw.trim()
      if (value !== '' && !OPT_OUT_OFF.has(value.toLowerCase())) return true
    } else {
      return true
    }
  }
  return false
}

/** Best effort: a cache we cannot write is a cache we simply check again. */
async function writeCache(file, data) {
  try {
    await mkdir(dirname(file), { recursive: true })
    await writeFile(file, JSON.stringify(data))
  } catch { /* ignored on purpose */ }
}

function isOk(response) {
  if (!response) return false
  if (typeof response.status === 'number') return response.status >= 200 && response.status < 300
  return response.ok === true
}

/**
 * Returns the notice line it logged, or null. Never throws, never blocks:
 * the caller does not await it, so plugin load time does not change.
 */
export async function checkForUpdate({
  pkg,
  current,
  cacheFile,
  env = process.env,
  fetchImpl = globalThis.fetch,
  now = Date.now,
  log = console.error,
  timeoutMs = 3000,
} = {}) {
  try {
    if (optedOut(env)) return null

    // 2. Today's answer, if we have one, is the answer -- no network.
    let cache = null
    try {
      cache = JSON.parse(await readFile(cacheFile, 'utf8'))
    } catch { cache = null }
    if (cache !== null && typeof cache === 'object' && Number.isFinite(cache.checkedAt) && now() - cache.checkedAt < DAY_MS) {
      if (typeof cache.next === 'string' && compareVersions(cache.next, current) > 0) {
        const line = noticeLine(pkg, current, cache.next)
        log(line)
        return line
      }
      return null
    }

    // 3. Ask the registry. Any failure at all is silent and writes nothing.
    const response = await fetchImpl(
      `https://registry.npmjs.org/-/package/${encodeURIComponent(pkg)}/dist-tags`,
      { signal: AbortSignal.timeout(timeoutMs), headers: { accept: 'application/json' } },
    )
    if (!isOk(response)) return null
    const tags = await response.json()
    if (tags === null || typeof tags !== 'object' || Array.isArray(tags)) return null

    // 4. Record the answer either way, so a quiet day stays a quiet day.
    const next = pickUpdate(current, tags)
    await writeCache(cacheFile, { checkedAt: now(), next })
    if (next === null) return null
    const line = noticeLine(pkg, current, next)
    log(line)
    return line
  } catch {
    return null
  }
}
