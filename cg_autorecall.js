import { pathToFileURL } from 'node:url'
import { renderMessage } from './cg_recall.js'

export const RECALL_TAG = '[context-guardian recall]'
export const STOP = Object.freeze(['JSON', 'YAML', 'HTML', 'HTTP', 'HTTPS', 'URL', 'API', 'CSV', 'SQL', 'TODO', 'NOTE', 'README'])

const MAX_NAMES = 12
const HIT_CHARS = 200
const WINDOW = 80

export function extractNames(text) {
  const s = String(text ?? '')
  const regexes = [
    /\bdoc\d+\b/gi,
    /\bL\d{3}\b/g,
    /\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b/g,
    /\b[A-Z][A-Z0-9]{2,}(?:[-_][A-Z0-9]+)*\b/g,
    /"([^"\n]{3,60})"|'([^'\n]{3,60})'|`([^`\n]{3,60})`/g
  ]

  const matches = []

  for (let i = 0; i < regexes.length; i++) {
    const re = regexes[i]
    let match
    while ((match = re.exec(s)) !== null) {
      // For the quoted alternation the interesting value is the captured inner
      // text; the other four keep the whole match.
      const value = i === 4 ? (match[1] !== undefined ? match[1] : match[2] !== undefined ? match[2] : match[3]) : match[0]
      matches.push({ index: match.index, value })
    }
  }

  matches.sort((a, b) => a.index - b.index)

  const seen = new Set()
  const result = []

  for (const m of matches) {
    if (result.length >= MAX_NAMES) break
    if (m.value === undefined) continue
    if (seen.has(m.value) || STOP.includes(m.value)) continue
    seen.add(m.value)
    result.push(m.value)
  }

  return result
}

export function linePairs(text) {
  const s = String(text ?? '')
  const re = /\b(doc\d+)\b[^\n]{0,40}?\b(L\d{3})\b/gi
  const pairs = []
  const seen = new Set()
  let match

  while ((match = re.exec(s)) !== null) {
    const doc = match[1]
    const line = match[2].toUpperCase()
    const key = doc.toLowerCase() + ' ' + line.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    pairs.push({ doc, line })
  }

  return pairs
}

export function staleRecall(input = {}) {
  const inp = input && typeof input === 'object' ? input : {}
  const userText = String(inp.userText ?? '')
  const live = String(inp.liveText ?? '').toLowerCase()
  const maxHits = Number.isFinite(inp.maxHits) ? inp.maxHits : 3
  const maxChars = Number.isFinite(inp.maxChars) ? inp.maxChars : 600

  const nodes = (Array.isArray(inp.nodes) ? inp.nodes : [])
    .filter((n) => n && n.message)
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))

  const rendered = nodes.map((n) => {
    let text
    try {
      text = renderMessage(n.message)
    } catch (err) {
      text = ''
    }
    return { seq: n.seq, text: String(text ?? '') }
  })

  const hits = []
  let used = 0

  function add(label, seq, text) {
    if (hits.length >= maxHits) return
    const t = String(text).slice(0, HIT_CHARS)
    if (used + t.length > maxChars) return
    hits.push({ label, seq, text: t })
    used += t.length
  }

  // Step 1: "docNN LNNN" pairs pull the exact archived line back in.
  const pairs = linePairs(userText)
  const pairNames = new Set()

  for (const { doc, line } of pairs) {
    pairNames.add(doc.toLowerCase())
    pairNames.add(line.toLowerCase())
  }

  for (const { doc, line } of pairs) {
    const lowerDoc = doc.toLowerCase()
    // Built from the upper-cased line number: the regex is not case-insensitive
    // and archived lines start with "L017", not "l017".
    const re = new RegExp('^\\s*' + line + '\\b.*$', 'm')

    let foundEntry = null
    for (const entry of rendered) {
      if (entry.text.toLowerCase().includes(lowerDoc) && re.test(entry.text)) {
        foundEntry = entry
        break
      }
    }
    if (!foundEntry) continue

    const found = foundEntry.text.match(re)
    const lineText = (found && found[0] ? found[0] : '').trim()
    if (!lineText) continue
    if (live.includes(lineText.toLowerCase())) continue
    add(doc + ' ' + line, foundEntry.seq, lineText)
  }

  // Step 2: every other name that is neither a pair name nor still live.
  for (const name of extractNames(userText)) {
    const key = name.toLowerCase()
    if (pairNames.has(key) || live.includes(key)) continue

    let foundEntry = null
    for (const entry of rendered) {
      if (entry.text.toLowerCase().includes(key)) {
        foundEntry = entry
        break
      }
    }
    if (!foundEntry) continue

    const at = foundEntry.text.toLowerCase().indexOf(key)
    if (at === -1) continue

    const len = foundEntry.text.length
    const start = Math.max(0, at - WINDOW)
    const end = Math.min(len, at + key.length + WINDOW)
    const body = foundEntry.text.slice(start, end).replace(/[\n\t]+/g, ' ')

    add(name, foundEntry.seq, (start > 0 ? '…' : '') + body + (end < len ? '…' : ''))
  }

  if (hits.length === 0) return ''

  return RECALL_TAG + ' from earlier in this session (compacted out of view):\n' +
    hits.map((h) => '- ' + h.label + ' (seq ' + h.seq + '): ' + h.text).join('\n')
}

function userNode(seq, text) {
  return { seq, message: { role: 'user', content: [{ type: 'text', text }] } }
}

export function selftest() {
  const FENCE = '`'.repeat(3)
  const doc = (k) => Array.from({ length: 100 }, (_, i) => 'L' + String(i + 1).padStart(3, '0') + ' Word' + k + ' line ' + (i + 1) + '; code C' + k + String(i + 1).padStart(3, '0') + '.').join('\n')
  const nodes = [1, 2, 4].map((k, i) => userNode(10 * (i + 1), 'Reading task ' + k + '. Below is lines 1-100 of the read-only file doc0' + k + '.txt.\n' + FENCE + '\n' + doc(k) + '\n' + FENCE))
  const ask = 'Final check, part 2. Reply with ONLY a JSON object: {"doc01 L017": ?, "doc02 L058": ?, "doc04 L081": ?}.'
  const checks = [
    ['names_in_order_unique_stoplisted', () => JSON.stringify(extractNames('see HeronNest and MAPLE-123, then "the blue file" and doc03 L044 and HeronNest again in JSON')) === JSON.stringify(['HeronNest', 'MAPLE-123', 'the blue file', 'doc03', 'L044'])],
    ['line_pairs', () => JSON.stringify(linePairs(ask)) === JSON.stringify([{ doc: 'doc01', line: 'L017' }, { doc: 'doc02', line: 'L058' }, { doc: 'doc04', line: 'L081' }])],
    ['bench_three_early_lines_recalled', () => {
      const r = staleRecall({ userText: ask, liveText: 'summary: three reading tasks were done', nodes })
      return r.startsWith(RECALL_TAG) && r.includes('code C1017') && r.includes('code C2058') && r.includes('code C4081')
    }],
    ['live_line_not_recalled', () => {
      const live = 'L017 Word1 line 17; code C1017.'
      const r = staleRecall({ userText: ask, liveText: live, nodes })
      return !r.includes('C1017') && r.includes('C2058')
    }],
    ['max_three_hits_and_600_chars', () => {
      const r = staleRecall({ userText: ask + ' also MAPLE-123', liveText: '', nodes: nodes.concat([userNode(50, 'the codename is MAPLE-123')]) })
      return r.split('\n').length === 4 && r.length <= 600 + 200
    }],
    ['name_absent_from_live_recalled', () => {
      const r = staleRecall({ userText: 'what was MAPLE-123 again?', liveText: 'nothing here', nodes: [userNode(5, 'For reference: the release codename for project Heron is MAPLE-123.')] })
      return r.includes('MAPLE-123 (seq 5)')
    }],
    ['name_still_live_skipped', () => staleRecall({ userText: 'what was MAPLE-123 again?', liveText: 'Heron is MAPLE-123', nodes: [userNode(5, 'Heron is MAPLE-123')] }) === ''],
    ['nothing_found_empty_string', () => staleRecall({ userText: 'hello there', liveText: '', nodes }) === '' && staleRecall() === ''],
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
  console.log('cg_autorecall selftest: ' + checks.length + ' checks, ' + passed + ' passed, ' + failed + ' failed')
  return { checks: checks.length, passed, failed }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--selftest')) {
  const r = selftest()
  process.exit(r.failed === 0 && r.checks > 0 ? 0 : 1)
}
