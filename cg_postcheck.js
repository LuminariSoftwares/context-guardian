// cg_postcheck.js -- context-guardian postcheck (C-Recall stage b: steer a final answer that states a stale pinned value).
//
// Pure module: finds statements in the model's final answer that state a STALE value
// for a subject the user has pinned, and renders a one-shot steering correction.
//
// Only imports allowed by the contract.
import { pathToFileURL } from 'node:url'
import { applyProvenance } from './cg_provenance.js'

export const STEER_TAG = '[context-guardian check]'

const MAX_REPORTS = 3
const STEER_MAX = 400
// A value must look like an identifier/codename: has a digit, two capitals, or a dash.
const VALUE_SHAPE = /[0-9]|[A-Z]{2}|-/

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function stripTrailing(value) {
  return String(value).replace(/[.:]+$/, '')
}

// subject -> [{key, value, seq}] for every fact that no later pin superseded.
export function currentFacts(pins) {
  const out = new Map()
  let copies
  try {
    copies = applyProvenance(Array.isArray(pins) ? pins : [])
  } catch {
    return out
  }
  if (!Array.isArray(copies)) return out
  for (const p of copies) {
    if (!p || typeof p !== 'object') continue
    const facts = Array.isArray(p.facts) ? p.facts : []
    for (const f of facts) {
      if (!f || typeof f !== 'object') continue
      if (f.superseded) continue
      const key = typeof f.key === 'string' ? f.key : String(f.key ?? '')
      if (!key) continue
      const subject = key.split(' ')[0].toLowerCase()
      if (!subject) continue
      const value = f.value === undefined || f.value === null ? '' : String(f.value)
      let list = out.get(subject)
      if (!list) { list = []; out.set(subject, list) }
      const dup = list.some(e => e.key === key && String(e.value).toLowerCase() === value.toLowerCase())
      if (dup) continue
      list.push({ key, value, seq: p.seq })
    }
  }
  return out
}

// Every "<subject> ...: value" style assertion in the text, in reading order.
export function assertions(finalText, subjects) {
  const text = String(finalText ?? '')
  const found = []
  for (const raw of (Array.isArray(subjects) ? subjects : [])) {
    if (raw === undefined || raw === null) continue
    const subject = String(raw)
    if (!subject) continue
    let re
    try {
      re = new RegExp(
        '["\']?\\b' + escapeRe(subject) +
        "\\b(?:'s(?:\\s+release)?\\s+[a-z]+)?[\"']?\\s*(?::|=|is now|is)\\s*[\"']?([A-Za-z0-9][A-Za-z0-9_./:-]*)",
        'gi',
      )
    } catch {
      continue
    }
    let m
    while ((m = re.exec(text)) !== null) {
      if (m[0] === '') { re.lastIndex++; continue }
      found.push({ subject: subject.toLowerCase(), value: stripTrailing(m[1]), index: m.index })
    }
  }
  found.sort((a, b) => a.index - b.index)
  return found.map(e => ({ subject: e.subject, value: e.value }))
}

// Assertions that contradict a subject's single current pinned fact.
export function staleAssertions(finalText, pins) {
  const facts = currentFacts(pins)
  const subjects = []
  const current = new Map()
  for (const [subject, list] of facts) {
    if (list.length !== 1) continue
    subjects.push(subject)
    current.set(subject, list[0])
  }
  if (subjects.length === 0) return []

  let seen
  try {
    seen = assertions(finalText, subjects)
  } catch {
    return []
  }
  if (!Array.isArray(seen)) return []

  const reported = new Set()
  const out = []
  for (const a of seen) {
    if (out.length >= MAX_REPORTS) break
    if (!a || reported.has(a.subject)) continue
    const value = String(a.value ?? '')
    if (!VALUE_SHAPE.test(value)) continue
    const cur = current.get(a.subject)
    if (!cur) continue
    if (value.toLowerCase() === String(cur.value).toLowerCase()) continue
    reported.add(a.subject)
    out.push({ subject: a.subject, said: value, current: cur.value, key: cur.key, seq: cur.seq })
  }
  return out
}

// Render the one-shot steering message (capped at STEER_MAX characters).
export function steerText(stale) {
  if (!Array.isArray(stale) || stale.length === 0) return ''
  let s = STEER_TAG + ' Before you finish, correct these against the pinned facts -- ' +
    stale.map(s2 => s2.key + ': you wrote ' + s2.said + ', the current pinned value is ' + s2.current + ' (seq ' + s2.seq + ')').join('; ') + '.'
  if (s.length > STEER_MAX) s = s.slice(0, STEER_MAX - 1) + '…'
  return s
}

export function selftest() {
  const plant = { text: 'Important, keep this for later in the conversation: the release codename for project Heron is MAPLE-123; and the release codename for project Kite is RIVER-456.', seq: 3 }
  const update = { text: 'Important, keep this for later in the conversation: the release codename for project Heron is now CEDAR-789.', seq: 30 }
  const pins = [plant, update]
  const checks = [
    ['current_facts_skip_superseded', () => {
      const f = currentFacts(pins)
      return f.get('heron').length === 1 && f.get('heron')[0].value === 'CEDAR-789' && f.get('kite')[0].value === 'RIVER-456'
    }],
    ['json_answer_with_stale_value', () => {
      const s = staleAssertions('{"Heron": "MAPLE-123", "Kite": "RIVER-456"}', pins)
      return s.length === 1 && s[0].subject === 'heron' && s[0].said === 'MAPLE-123' && s[0].current === 'CEDAR-789' && s[0].seq === 30
    }],
    ['prose_possessive_assertion', () => staleAssertions("Heron's codename is MAPLE-123.", pins).length === 1],
    ['current_value_no_finding', () => staleAssertions('{"Heron": "CEDAR-789", "Kite": "river-456"}', pins).length === 0],
    ['non_value_words_ignored', () => staleAssertions('Heron: see above', pins).length === 0],
    ['unknown_subject_ignored', () => staleAssertions('{"Owl": "X-1"}', pins).length === 0],
    ['steer_text_tagged_and_capped', () => {
      const t = steerText(staleAssertions('{"Heron": "MAPLE-123"}', pins))
      const long = steerText(Array.from({ length: 20 }, (_, i) => ({ key: 'k' + i + ' codename', said: 'A-' + i, current: 'B-' + i, seq: i })))
      return t.startsWith(STEER_TAG) && t.includes('CEDAR-789') && long.length <= 400 && steerText([]) === ''
    }],
    ['assertions_in_text_order', () => JSON.stringify(assertions('Kite = K-1 then Heron: H-2', ['heron', 'kite'])) === JSON.stringify([{ subject: 'kite', value: 'K-1' }, { subject: 'heron', value: 'H-2' }])],
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
  console.log('cg_postcheck selftest: ' + checks.length + ' checks, ' + passed + ' passed, ' + failed + ' failed')
  return { checks: checks.length, passed, failed }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--selftest')) {
  const r = selftest()
  process.exit(r.failed === 0 && r.checks > 0 ? 0 : 1)
}