// cg_anchors.js -- anchor recovery for the context-guardian.
// After a conversation region is compacted into a checkpoint, find the named
// things (files, identifiers, URLs, code spans) that the checkpoint dropped but
// that later turns still reference, plus the constraints/decisions the summary
// forgot. Pure library: no imports, no I/O, no clock, no mutated globals.

export const ANCHORS_REV = 'cg-anchors-1'

const UNIQUE_CAP = 2000
const RETURN_CAP = 200
const LOST_CAP = 40
const ITEM_CAP = 20

const CARRIED_HEADER = '[context-guardian: carried facts -- named before this checkpoint and still referenced after it, but missing from the summary above]'

const RE_URL = /https?:\/\/[^\s)\]>"'`]+/g
const RE_PATH = /(?:[A-Za-z]:[\\/])?(?:[\w.\-]+[\\/])+[\w.\-]+\.[A-Za-z0-9]{1,8}\b/g
const RE_FILE = /\b[\w\-]+\.(py|js|mjs|cjs|ts|tsx|jsx|json|md|yml|yaml|toml|txt|sh|bat|ps1|csv|html|css|sql|go|rs|java|c|cpp|h)\b/g
const RE_CALL = /\b([A-Za-z_][A-Za-z0-9_]{2,})\(/g

const RE_TRAILING = /[.,;:!?]+$/
const RE_WHITESPACE = /\s+/g

const K_CODE = 1
const K_URL = 2
const K_PATH = 3
const K_FILE = 4
const K_CALL = 5

// Defensive coercion: never throw, never leak a non-string into a regex.
function asText(value) {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  try {
    return String(value)
  } catch {
    return ''
  }
}

function asList(value) {
  if (Array.isArray(value)) return value
  if (value === null || value === undefined) return []
  return [value]
}

function asItems(value) {
  return asList(value).map((entry) => {
    if (entry !== null && typeof entry === 'object') return entry
    return { cat: '', text: asText(entry) }
  })
}

function norm(s) {
  return asText(s).toLowerCase().replace(RE_WHITESPACE, ' ').trim().replace(RE_TRAILING, '')
}

function pushCandidate(bag, text, kind, start) {
  if (text === '') return
  bag.push({ text, lc: text.toLowerCase(), kind, start, seq: bag.length })
}

// Kind 1: code spans, one per odd split segment that has a closing backtick.
function collectCodeSpans(text, bag) {
  const segments = text.split('`')
  let offset = 0
  for (let i = 0; i < segments.length; i += 1) {
    const segment = segments[i]
    const start = offset
    offset += segment.length + 1
    if (i % 2 !== 1 || i >= segments.length - 1) continue
    if (segment.includes('\n')) continue
    const trimmed = segment.trim()
    if (trimmed.length < 3 || trimmed.length > 120) continue
    pushCandidate(bag, trimmed, K_CODE, start)
  }
}

function collectMatches(text, re, kind, bag, group) {
  for (const m of text.matchAll(re)) {
    let value = group === 0 ? m[0] : m[group]
    if (value === undefined) continue
    if (kind === K_URL) value = value.replace(RE_TRAILING, '')
    if (value === '') continue
    pushCandidate(bag, value, kind, m.index)
  }
}

// An occurrence is redundant only when it is a strict sub-string of a longer
// anchor AND it is not a distinct mention in its own right: either it was found
// by a different extractor (a path containing a bare file name), or the shorter
// match physically lies inside the longer one in this text.
function insideShorter(cand, other) {
  return cand.start >= other.start && cand.start + cand.text.length <= other.start + other.text.length
}

function isRedundant(cand, unique) {
  for (const other of unique) {
    if (other === cand) continue
    if (other.lc.length <= cand.lc.length) continue
    if (!other.lc.includes(cand.lc)) continue
    if (cand.kind !== other.kind || insideShorter(cand, other)) return true
  }
  return false
}

export function extractAnchors(text) {
  if (typeof text !== 'string' || text === '') return []

  const bag = []
  collectCodeSpans(text, bag)
  collectMatches(text, RE_URL, K_URL, bag, 0)
  collectMatches(text, RE_PATH, K_PATH, bag, 0)
  collectMatches(text, RE_FILE, K_FILE, bag, 0)
  collectMatches(text, RE_CALL, K_CALL, bag, 1)
  if (bag.length === 0) return []

  bag.sort((a, b) => (a.start - b.start) || (a.seq - b.seq))

  const seen = new Set()
  const unique = []
  for (const cand of bag) {
    if (seen.has(cand.lc)) continue
    seen.add(cand.lc)
    unique.push(cand)
    if (unique.length >= UNIQUE_CAP) break
  }

  const out = []
  for (const cand of unique) {
    if (isRedundant(cand, unique)) continue
    out.push(cand.text)
    if (out.length >= RETURN_CAP) break
  }
  return out
}

export function findLostAnchors({ regionTexts = [], laterTexts = [], checkpointText = '', memoryTexts = [] } = {}) {
  const region = asList(regionTexts).map(asText).join('\n')
  const later = asList(laterTexts).map(asText).join('\n').toLowerCase()
  const haystack = (asText(checkpointText) + '\n' + asList(memoryTexts).map(asText).join('\n')).toLowerCase()

  const recurring = []
  for (const anchor of extractAnchors(region)) {
    if (later.includes(anchor.toLowerCase())) recurring.push(anchor)
  }

  const kept = []
  const lost = []
  for (const anchor of recurring) {
    if (haystack.includes(anchor.toLowerCase())) {
      kept.push(anchor)
    } else if (lost.length < LOST_CAP) {
      lost.push(anchor)
    }
  }
  return { recurring, kept, lost }
}

export function lostConstraints({ regionItems = [], checkpointText = '' } = {}) {
  const target = norm(checkpointText)
  const out = []
  for (const item of asItems(regionItems)) {
    const cat = asText(item.cat)
    if (cat !== 'constraints' && cat !== 'decisions') continue
    const text = norm(item.text)
    if (text === '') continue
    if (target.includes(text)) continue
    out.push(item)
    if (out.length >= ITEM_CAP) break
  }
  return out
}

export function renderCarried(lost = [], items = [], maxChars = 1600) {
  const anchors = asList(lost).map(asText).filter((s) => s !== '')
  const carried = []
  for (const item of asItems(items)) {
    const cat = asText(item.cat)
    if (cat !== 'constraints' && cat !== 'decisions') continue
    const text = asText(item.text)
    if (text === '') continue
    carried.push(cat === 'constraints' ? '- constraint: ' + text : '- decision: ' + text)
  }

  const candidates = []
  for (const anchor of anchors) candidates.push('- `' + anchor + '`')
  for (const line of carried) candidates.push(line)
  if (candidates.length === 0) return ''

  const limit = Number.isFinite(maxChars) ? maxChars : 0
  const lines = [CARRIED_HEADER]
  let width = CARRIED_HEADER.length
  for (const line of candidates) {
    // join('\n') costs one separator per extra line: width + line + count.
    if (width + 1 + line.length > limit) break
    lines.push(line)
    width += 1 + line.length
  }
  const leftOut = candidates.length - (lines.length - 1)
  if (leftOut > 0) lines.push('- (+' + leftOut + ' more; recall by seq)')
  return lines.join('\n')
}
