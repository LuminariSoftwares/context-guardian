import { pathToFileURL } from 'node:url'

export const ATTRIBUTES = Object.freeze(['codename', 'code', 'owner', 'version', 'port', 'path', 'name'])

// "codename" is listed before "code" so the alternation never reads "codename" as "code".
const ATTR = '(' + ATTRIBUTES.join('|') + ')'
const SUBJ = '([A-Za-z][A-Za-z0-9_-]*)'
const VAL = '([A-Za-z0-9][A-Za-z0-9_./:-]*)'

// P1: "the release codename for project Heron is MAPLE-123" -> attribute, subject, value
const P1 = new RegExp('\\b(?:the\\s+)?(?:release\\s+)?' + ATTR + '\\s+for\\s+(?:project\\s+)?' + SUBJ + '\\s+is\\s+(?:now\\s+)?' + VAL, 'gi')
// P2: "Update: project Heron's release codename is now CEDAR-789" -> subject, attribute, value
const P2 = new RegExp('\\b(?:update:\\s*)?(?:project\\s+)?' + SUBJ + "'s\\s+(?:release\\s+)?" + ATTR + '\\s+is\\s+(?:now\\s+)?' + VAL, 'gi')

function makeFact(m, subjectIdx, attributeIdx) {
  const subject = m[subjectIdx]
  const attribute = m[attributeIdx]
  if (!subject || !attribute) return null
  return {
    key: subject.toLowerCase() + ' ' + attribute.toLowerCase(),
    // A trailing '.' or ':' is sentence punctuation, not part of the value.
    value: m[3].replace(/[.:]+$/, ''),
    index: m.index,
    length: m[0].length,
  }
}

export function extractFacts(text) {
  const str = String(text ?? '')
  const facts = []

  // P1 captures (attribute, subject, value); P2 captures (subject, attribute, value).
  for (const m of str.matchAll(P1)) {
    const f = makeFact(m, 2, 1)
    if (f) facts.push(f)
  }
  for (const m of str.matchAll(P2)) {
    const f = makeFact(m, 1, 2)
    if (f) facts.push(f)
  }

  // Sort by index; keep a match only if it does not start before the end of the
  // previously kept one, so overlapping reads collapse to the first one.
  facts.sort((a, b) => a.index - b.index)
  const kept = []
  let lastEnd = -1
  for (const fact of facts) {
    if (fact.index >= lastEnd) {
      kept.push(fact)
      lastEnd = fact.index + fact.length
    }
  }
  return kept
}

export function pinKey(text) {
  const facts = extractFacts(text)
  if (facts.length === 0) return { key: null, value: null }
  return { key: facts[0].key, value: facts[0].value }
}

export function applyProvenance(pins) {
  if (!Array.isArray(pins)) return []

  // Work on shallow copies: the caller's array and its objects are never touched.
  const entries = pins.map((pin) => {
    const source = pin && typeof pin === 'object' ? pin : {}
    return {
      pin: source,
      seq: Number(source.seq) || 0,
      facts: extractFacts(source.text),
    }
  })

  for (let i = 0; i < entries.length; i++) {
    const mine = entries[i]
    for (const fact of mine.facts) {
      // Any OTHER pin, wherever it sits in the array, supersedes only if its seq is
      // strictly higher and it states the same key with a different value. The
      // reported seq is the lowest such one, not the first one found.
      let by = null
      for (let j = 0; j < entries.length; j++) {
        if (j === i) continue
        const other = entries[j]
        if (other.seq <= mine.seq) continue
        for (const of_ of other.facts) {
          if (of_.key !== fact.key) continue
          if (of_.value.toLowerCase() === fact.value.toLowerCase()) continue
          if (by === null || other.seq < by) by = other.seq
        }
      }
      fact.superseded = by !== null
      fact.supersededBy = by
    }
  }

  return entries.map(({ pin, facts }) => {
    const first = facts.length > 0 ? facts[0] : null
    // A pin with no facts has nothing to keep and is therefore never retired.
    const allSuperseded = facts.length > 0 && facts.every((f) => f.superseded)
    const mapFact = (f) => ({
      key: f.key,
      value: f.value,
      index: f.index,
      length: f.length,
      superseded: f.superseded,
      supersededBy: f.supersededBy,
    })
    return Object.assign({}, pin, {
      key: first ? first.key : null,
      value: first ? first.value : null,
      facts: facts.map(mapFact),
      superseded: allSuperseded,
      supersededBy: allSuperseded
        ? Math.max(...facts.map((f) => f.supersededBy))
        : null,
    })
  })
}

export function renderPinText(pin) {
  const text = String((pin && pin.text) ?? '')
  const facts = pin && Array.isArray(pin.facts) ? pin.facts : []
  if (facts.length === 0) return text

  // Replace the last span first so the remaining indexes stay valid.
  const spans = facts
    .filter((f) => f && f.superseded)
    .slice()
    .sort((a, b) => b.index - a.index)

  let out = text
  for (const f of spans) {
    const start = f.index
    const end = f.index + f.length
    if (!(Number.isInteger(start) && Number.isInteger(end)) || start < 0 || end > out.length || end < start) continue
    out = out.slice(0, start) + '[' + f.key + ': superseded by seq ' + f.supersededBy + ']' + out.slice(end)
  }
  return out
}

export function selftest() {
  const plant = 'Important, keep this for later in the conversation: the release codename for project Heron is MAPLE-123; and the release codename for project Kite is RIVER-456.'
  const update = 'Important, keep this for later in the conversation: the release codename for project Heron is now CEDAR-789.'
  const checks = [
    ['p1_two_facts', () => {
      const f = extractFacts(plant)
      return f.length === 2 && f[0].key === 'heron codename' && f[0].value === 'MAPLE-123' && f[1].key === 'kite codename' && f[1].value === 'RIVER-456'
    }],
    ['p2_possessive_update', () => {
      const f = extractFacts("Update: project Heron's release codename is now CEDAR-789.")
      return f.length === 1 && f[0].key === 'heron codename' && f[0].value === 'CEDAR-789'
    }],
    ['code_is_not_codename', () => extractFacts('the code for Kite is ABC123')[0].key === 'kite code'],
    ['no_match_null_key', () => {
      const k = pinKey('please remember that I like tea')
      return k.key === null && k.value === null
    }],
    ['partial_supersede_keeps_pin', () => {
      const r = applyProvenance([{ text: plant, seq: 3 }, { text: update, seq: 30 }])
      return r[0].superseded === false && r[0].facts[0].superseded === true && r[0].facts[0].supersededBy === 30 && r[0].facts[1].superseded === false && r[1].superseded === false
    }],
    ['full_supersede', () => {
      const r = applyProvenance([{ text: 'For reference: the release codename for project Owl is ONE-1.', seq: 2 }, { text: 'For reference: the release codename for project Owl is TWO-2.', seq: 9 }])
      return r[0].superseded === true && r[0].supersededBy === 9 && r[0].key === 'owl codename'
    }],
    ['same_value_restated_not_superseded', () => {
      const r = applyProvenance([{ text: 'the release codename for project Owl is ONE-1', seq: 2 }, { text: 'the release codename for project Owl is one-1', seq: 9 }])
      return r[0].superseded === false && r[0].facts[0].superseded === false
    }],
    ['older_pin_never_supersedes_newer', () => {
      const r = applyProvenance([{ text: 'the release codename for project Owl is TWO-2', seq: 9 }, { text: 'the release codename for project Owl is ONE-1', seq: 2 }])
      return r[0].superseded === false && r[1].superseded === true
    }],
    ['render_strikes_only_stale_fact', () => {
      const r = applyProvenance([{ text: plant, seq: 3 }, { text: update, seq: 30 }])
      const t = renderPinText(r[0])
      return !t.includes('MAPLE-123') && t.includes('RIVER-456') && t.includes('[heron codename: superseded by seq 30]')
    }],
    ['input_not_mutated', () => {
      const pins = [{ text: plant, seq: 3 }, { text: update, seq: 30 }]
      applyProvenance(pins)
      return !('facts' in pins[0]) && !('superseded' in pins[0])
    }],
  ]
  let passed = 0
  for (const [name, fn] of checks) {
    let ok = false
    let label = name
    try {
      ok = Boolean(fn())
    } catch (err) {
      label = name + ' (raised ' + (err && err.name) + ': ' + (err && err.message) + ')'
    }
    if (ok) passed += 1
    console.log((ok ? 'ok   ' : 'FAIL ') + label)
  }
  const failed = checks.length - passed
  console.log('cg_provenance selftest: ' + checks.length + ' checks, ' + passed + ' passed, ' + failed + ' failed')
  return { checks: checks.length, passed, failed }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--selftest')) {
  const r = selftest()
  process.exit(r.failed === 0 && r.checks > 0 ? 0 : 1)
}
