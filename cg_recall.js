// cg_recall.js — pure recall/search/keyword-index/rewrite-cost helpers over an array of conversation NODES.
// ESM, Node >= 20, zero third-party imports. See CONTRACT_cg_recall.md for the full spec.
import { pathToFileURL } from 'node:url'

/** Revision tag for this recall implementation. */
export const RECALL_REV = 'cg-recall-1'

/** Rough token estimate: ceil(chars/4); '', null, undefined => 0. */
export function estTokens(text) {
  if (text === null || text === undefined || text === '') return 0
  return Math.ceil(String(text).length / 4)
}

const VALID_TYPES = ['seq', 'result', 'checkpoint']
const ERR_FORM = 'invalid recall id — use forms like seq "3-7", seq "7", result "3", checkpoint "1"'
const STOPWORDS = new Set([
  'this', 'that', 'with', 'from', 'have', 'will', 'your', 'were', 'been', 'they',
  'their', 'there', 'what', 'when', 'which', 'would', 'could', 'should', 'about',
  'into', 'then', 'than', 'them', 'some', 'only', 'also', 'more', 'here', 'just', 'like',
])

/** Parse a single id token or "a-b"/"a..b" range into {from,to,isRange}, or null when invalid. */
function parseIdRange(id) {
  if (id === null || id === undefined) return null
  if (typeof id === 'number') {
    if (!Number.isFinite(id) || !Number.isInteger(id) || id < 0) return null
    return { from: id, to: id, isRange: false }
  }
  const s = String(id).trim()
  if (s === '') return null
  const m = s.match(/^(-?\d+(?:\.\d+)?)\s*(?:-|\.\.)\s*(-?\d+(?:\.\d+)?)$/)
  if (m) {
    const a = Number(m[1]), b = Number(m[2])
    if (!Number.isInteger(a) || !Number.isInteger(b) || a < 0 || b < 0) return null
    return { from: Math.min(a, b), to: Math.max(a, b), isRange: true }
  }
  const n = Number(s)
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0) return null
  return { from: n, to: n, isRange: false }
}

/** Parse a recall {type,id} pair into an ok-shape request or {ok:false,error}. */
export function parseRecallRequest(type, id) {
  const rawType = (type === undefined || type === null) ? '' : String(type)
  let t = rawType.trim().toLowerCase()
  if (t === '') t = 'seq'
  if (!VALID_TYPES.includes(t)) {
    return { ok: false, error: `unknown recall type ${JSON.stringify(type)} — ${ERR_FORM}` }
  }
  const range = parseIdRange(id)
  if (!range) {
    return { ok: false, error: `bad recall id ${JSON.stringify(id)} — ${ERR_FORM}` }
  }
  if (range.isRange && t !== 'seq') {
    return { ok: false, error: `${t} accepts a single integer, not a range — ${ERR_FORM}` }
  }
  return { ok: true, type: t, from: range.from, to: range.to }
}

/** Parse the argument string of /recall ("seq 3-7", "3-7", "result 3", ...) into the same shape as parseRecallRequest. */
export function parseRecallCommand(raw) {
  const s = (raw === null || raw === undefined) ? '' : String(raw).trim()
  if (s === '') return { ok: false, error: `empty recall command — ${ERR_FORM}` }
  const m = s.match(/^(\S+)\s+(\S.*)$/)
  if (m && VALID_TYPES.includes(m[1].trim().toLowerCase())) {
    return parseRecallRequest(m[1], m[2])
  }
  return parseRecallRequest(undefined, s)
}

/** Render one nested block (inside a tool-result) to a display line. */
function renderNestedBlock(block) {
  if (!block || typeof block !== 'object') return null
  if (block.type === 'text') return String(block.text ?? '')
  if (block.type === 'image') return '[image]'
  if (block.type === 'document') return '[document]'
  return `[${block.type}]`
}

/** Render one top-level block, or null when it should be skipped entirely (reasoning). */
function renderBlock(block) {
  if (!block || typeof block !== 'object') return null
  switch (block.type) {
    case 'text': return String(block.text ?? '')
    case 'reasoning': return null
    case 'tool-call': return `* ${block.name}(${block.arguments})`
    case 'tool-result': {
      const head = block.isError ? `[ERROR result of ${block.toolCallId}]` : `[result of ${block.toolCallId}]`
      const nested = Array.isArray(block.content) ? block.content.map(renderNestedBlock).filter((x) => x !== null) : []
      return [head, ...nested].join('\n')
    }
    case 'image': return '[image]'
    case 'document': return '[document]'
    default: return `[${block.type}]`
  }
}

/** Render a Message to its display text; null/undefined message => ''. */
export function renderMessage(message) {
  if (!message || !Array.isArray(message.content)) return ''
  const lines = []
  for (const block of message.content) {
    const r = renderBlock(block)
    if (r !== null) lines.push(r)
  }
  return lines.join('\n')
}

/** True when a node is a checkpoint (compact-plugin) user message. */
function isCheckpoint(node) {
  const src = node.message && node.message.source
  return !!(src && src.kind === 'plugin' && src.plugin === 'compact')
}

/** Render [seq N role]\n<renderMessage> blocks under a token budget; may cut/truncate. */
function renderNodesWithBudget(matched, maxTokens, to) {
  const rendered = matched.map((n) => `[seq ${n.seq} ${n.message.role}]\n${renderMessage(n.message)}`)
  let text = ''
  const seqs = []
  let truncated = false
  for (let i = 0; i < rendered.length; i++) {
    const candidate = i === 0 ? rendered[0] : `${text}\n\n${rendered[i]}`
    if (estTokens(candidate) <= maxTokens) {
      text = candidate
      seqs.push(matched[i].seq)
      continue
    }
    if (i === 0) {
      text = candidate.slice(0, maxTokens * 4)
      seqs.push(matched[0].seq)
      truncated = matched.length > 1 || candidate.length > text.length
    } else {
      truncated = true
    }
    break
  }
  if (truncated) {
    const lastSeq = seqs[seqs.length - 1]
    const lastIdx = matched.findIndex((n) => n.seq === lastSeq)
    const nextNode = matched[lastIdx + 1]
    const firstOmitted = nextNode ? nextNode.seq : lastSeq + 1
    text += `\n[recall truncated — next: recall(type="seq", id="${firstOmitted}-${to}")]`
  }
  return { ok: true, text, seqs, truncated, tokens: estTokens(text) }
}

/** Build the NOT FOUND result, naming the seq range actually present. */
function notFoundResult(nodesWithMsg) {
  let seqsStr = 'none'
  if (nodesWithMsg.length) {
    const all = nodesWithMsg.map((n) => n.seq)
    seqsStr = `${Math.min(...all)}-${Math.max(...all)}`
  }
  const text = `NOT FOUND: no matching nodes (seqs ${seqsStr})`
  return { ok: false, text, seqs: [], truncated: false, tokens: estTokens(text) }
}

/** Recall rendered node text for a seq range / tool-result / checkpoint request. */
export function recall(nodes, request, opts = {}) {
  const maxTokens = opts && Number.isFinite(opts.maxTokens) ? opts.maxTokens : 16000
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && typeof n.seq === 'number') : []
  const withMsg = list.filter((n) => n.message)
  if (!request || request.ok !== true) return notFoundResult(withMsg)
  const { type, from, to } = request

  let matched = []
  if (type === 'seq') {
    matched = withMsg.filter((n) => n.seq >= from && n.seq <= to).sort((a, b) => a.seq - b.seq)
  } else if (type === 'result') {
    const target = list.find((n) => n.seq === from)
    if (target && target.message) {
      const blocks = Array.isArray(target.message.content) ? target.message.content : []
      if (target.message.role === 'user' && blocks[0] && blocks[0].type === 'tool-result') {
        matched = [target]
      } else if (target.message.role === 'assistant') {
        const callIds = blocks.filter((b) => b && b.type === 'tool-call').map((b) => b.id)
        if (callIds.length) {
          matched = withMsg
            .filter((n) => {
              const c = n.message.content
              return Array.isArray(c) && c[0] && c[0].type === 'tool-result' && callIds.includes(c[0].toolCallId)
            })
            .sort((a, b) => a.seq - b.seq)
        }
      }
    }
  } else if (type === 'checkpoint') {
    const checkpoints = withMsg.filter(isCheckpoint).sort((a, b) => a.seq - b.seq)
    const n = checkpoints[from - 1]
    if (n) matched = [n]
  }

  if (matched.length === 0) return notFoundResult(withMsg)
  return renderNodesWithBudget(matched, maxTokens, to)
}

/** Escape a literal string for use inside a RegExp. */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Build a one-line snippet around a match, collapsing whitespace and marking cuts with …. */
function makeSnippet(text, index, len, contextChars) {
  const start = Math.max(0, index - contextChars)
  const end = Math.min(text.length, index + len + contextChars)
  let snippet = text.slice(start, end).replace(/[\n\t]+/g, ' ')
  if (start > 0) snippet = `…${snippet}`
  if (end < text.length) snippet = `${snippet}…`
  return snippet
}

/** Search rendered node text for a literal substring or regex; one hit per matching node. */
export function search(nodes, query, opts = {}) {
  const maxHits = Number.isFinite(opts.maxHits) ? opts.maxHits : 50
  const contextChars = Number.isFinite(opts.contextChars) ? opts.contextChars : 80
  const useRegex = !!opts.regex
  if (query === null || query === undefined || String(query).trim() === '') {
    return { ok: false, error: 'search query must be a non-empty string' }
  }
  let re
  try {
    re = new RegExp(useRegex ? String(query) : escapeRegExp(String(query)), 'i')
  } catch (e) {
    return { ok: false, error: `invalid regex: ${e.message}` }
  }
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.message) : []
  const matches = []
  for (const n of list) {
    const text = renderMessage(n.message)
    const m = re.exec(text)
    if (m) matches.push({ seq: n.seq, role: n.message.role, snippet: makeSnippet(text, m.index, m[0].length, contextChars) })
  }
  matches.sort((a, b) => a.seq - b.seq)
  const total = matches.length
  return { ok: true, hits: matches.slice(0, maxHits), total, capped: total > maxHits }
}

/** Render a search() result to a compact string for the model. */
export function renderSearch(result, query) {
  if (!result || result.ok !== true) {
    return `search error: ${result && result.error ? result.error : 'unknown error'}`
  }
  if (result.total === 0) return `0 hits for "${query}"`
  const suffix = result.capped ? ` (showing first ${result.hits.length})` : ''
  const lines = [`${result.total} hits for "${query}"${suffix}`]
  for (const h of result.hits) lines.push(`seq ${h.seq} [${h.role}] ${h.snippet}`)
  lines.push(`NEXT STEP: recall(type="seq", id="${result.hits[0].seq}")`)
  return lines.join('\n')
}

/** Fold rendered node text into a keyword -> seqs index, path/identifier-like terms only. */
export function foldKeywordIndex(nodes, opts = {}) {
  const maxTerms = Number.isFinite(opts.maxTerms) ? opts.maxTerms : 40
  const minLen = Number.isFinite(opts.minLen) ? opts.minLen : 4
  const maxSeqsPerTerm = Number.isFinite(opts.maxSeqsPerTerm) ? opts.maxSeqsPerTerm : 6
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.message) : []
  const termSeqs = new Map()
  const tokenRe = /[A-Za-z_][\w.\-/\\]{3,}/g
  for (const n of list) {
    const text = renderMessage(n.message)
    const seenInNode = new Set()
    let m
    tokenRe.lastIndex = 0
    while ((m = tokenRe.exec(text))) {
      const term = m[0].toLowerCase().replace(/[.,]+$/, '')
      if (term.length < minLen || STOPWORDS.has(term) || seenInNode.has(term)) continue
      seenInNode.add(term)
      if (!termSeqs.has(term)) termSeqs.set(term, [])
      termSeqs.get(term).push(n.seq)
    }
  }
  const entries = []
  for (const [term, seqs] of termSeqs) {
    if (seqs.length >= 2) entries.push({ term, seqs: seqs.slice().sort((a, b) => a - b) })
  }
  entries.sort((a, b) => b.seqs.length - a.seqs.length || (a.term < b.term ? -1 : a.term > b.term ? 1 : 0))
  return entries.slice(0, maxTerms).map((e) => ({ term: e.term, seqs: e.seqs.slice(0, maxSeqsPerTerm) }))
}

/** Render a keyword index to a compact string for the model; '' when empty. */
export function renderKeywordIndex(index) {
  if (!Array.isArray(index) || index.length === 0) return ''
  const lines = ['KEYWORD INDEX (term → seqs; use recall):']
  for (const e of index) lines.push(`${e.term}: ${e.seqs.join(', ')}`)
  return lines.join('\n')
}

/** Round to 4 decimal places. */
function round4(x) {
  return Math.round(x * 10000) / 10000
}

/** Estimate whether rewriting (shadowing) a span of context is worth its reprefill cost. */
export function rewriteCost(input) {
  const src = input && typeof input === 'object' ? input : {}
  const surfaceTokens = src.surfaceTokens
  const shadowedTokens = src.shadowedTokens
  const replacementTokens = src.replacementTokens
  const window = src.window
  const cachedPrefixTokens = src.cachedPrefixTokens === undefined ? 0 : src.cachedPrefixTokens
  const nums = [surfaceTokens, shadowedTokens, replacementTokens, window, cachedPrefixTokens]
  if (!nums.every((v) => Number.isFinite(v)) || window <= 0) {
    return { error: 'rewriteCost requires finite numeric surfaceTokens/shadowedTokens/replacementTokens/window (window > 0) and cachedPrefixTokens' }
  }
  const saved = shadowedTokens - replacementTokens
  const pressureBefore = round4(surfaceTokens / window)
  const pressureAfter = round4((surfaceTokens - saved) / window)
  const reprefillTokens = Math.max(0, surfaceTokens - saved - Math.max(0, cachedPrefixTokens - shadowedTokens))
  const tier = pressureBefore < 0.30 ? 'none'
    : pressureBefore < 0.50 ? 'watch'
    : pressureBefore < 0.70 ? 'idle'
    : pressureBefore < 0.90 ? 'compact'
    : 'emergency'
  const worthIt = saved > 0 && (tier === 'emergency' || tier === 'compact' || saved >= 0.10 * window)
  return { pressureBefore, pressureAfter, saved, reprefillTokens, worthIt, tier }
}

// ---------------------------------------------------------------------------
// CLI selftest — only runs when this file is the process entry point.
// ---------------------------------------------------------------------------

function runSelftest() {
  const checks = []
  const check = (name, fn) => checks.push([name, fn])

  const T = (text) => ({ type: 'text', text })
  const okReq = (t, i) => {
    const r = parseRecallRequest(t, i)
    if (!r.ok) throw new Error(`fixture parse failed: ${JSON.stringify(r)}`)
    return r
  }

  // My own fixture set (distinct from the overseer probe's fixtures).
  const myNodes = [
    { seq: 10, message: null },
    { seq: 11, message: { role: 'user', content: [T('open config/app_settings.json and check retry_limit')] } },
    {
      seq: 12,
      message: {
        role: 'assistant',
        content: [
          { type: 'reasoning', text: 'HIDDEN-THOUGHT' },
          T('Looking it up.'),
          { type: 'tool-call', id: 'call-a', name: 'fetch', arguments: '{"file":"config/app_settings.json"}' },
        ],
      },
    },
    {
      seq: 13,
      message: {
        role: 'user',
        content: [{ type: 'tool-result', toolCallId: 'call-a', content: [T('retry_limit = 5  # x.y+z note'), { type: 'document' }] }],
      },
    },
    { seq: 14, message: { role: 'assistant', content: [T('retry_limit is 5 in config/app_settings.json')] } },
    { seq: 15, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T('CHECKPOINT-ALPHA body')] } },
    { seq: 16, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'call-z', isError: true, content: [T('kaboom')] }] } },
    { seq: 17, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T('CHECKPOINT-BETA body')] } },
  ]

  check('rev_and_estTokens', () =>
    RECALL_REV === 'cg-recall-1' && estTokens('abcd') === 1 && estTokens('') === 0 && estTokens(null) === 0 && estTokens(undefined) === 0)

  check('parse_default_type_and_range_forms', () => {
    const a = parseRecallRequest(undefined, '2-9')
    const b = parseRecallRequest(' SEQ ', ' 9 - 2 ')
    const c = parseRecallRequest('seq', '2..9')
    const d = parseRecallRequest('seq', 5)
    return a.ok && a.type === 'seq' && a.from === 2 && a.to === 9 &&
      b.ok && b.from === 2 && b.to === 9 &&
      c.ok && c.from === 2 && c.to === 9 &&
      d.ok && d.from === 5 && d.to === 5
  })

  check('parse_rejects_bad_ids_and_types', () => {
    const bad = [
      parseRecallRequest('seq', ''),
      parseRecallRequest('seq', '-4'),
      parseRecallRequest('seq', '2.5'),
      parseRecallRequest('bogus', '3'),
      parseRecallRequest('result', '3-4'),
      parseRecallRequest('checkpoint', '1-2'),
      parseRecallRequest('seq', 'nope'),
    ]
    return bad.every((r) => r.ok === false && typeof r.error === 'string') && bad[0].error.includes('seq "3-7"')
  })

  check('parse_command_variants', () => {
    const a = parseRecallCommand('  seq   2-9 ')
    const b = parseRecallCommand('2-9')
    const c = parseRecallCommand('result 13')
    const d = parseRecallCommand('checkpoint 1')
    const e = parseRecallCommand('   ')
    const f = parseRecallCommand('13')
    return a.ok && a.from === 2 && a.to === 9 && b.ok && b.type === 'seq' &&
      c.ok && c.type === 'result' && c.from === 13 && d.ok && d.type === 'checkpoint' &&
      e.ok === false && f.ok && f.from === 13
  })

  check('render_message_skips_reasoning_formats_call', () => {
    const s = renderMessage(myNodes[2].message)
    return !s.includes('HIDDEN-THOUGHT') && s.includes('Looking it up.') &&
      s.includes('* fetch({"file":"config/app_settings.json"})') && renderMessage(null) === '' && renderMessage(undefined) === ''
  })

  check('render_message_tool_result_and_error', () => {
    const s = renderMessage(myNodes[3].message)
    const e = renderMessage(myNodes[6].message)
    return s.startsWith('[result of call-a]\n') && s.includes('retry_limit = 5') && s.includes('[document]') &&
      e.startsWith('[ERROR result of call-z]') && e.includes('kaboom')
  })

  check('render_message_never_throws_on_garbage', () => {
    renderMessage({})
    renderMessage({ role: 'user' })
    renderMessage({ role: 'user', content: [null, 42, { type: 'mystery' }] })
    return true
  })

  check('recall_seq_range_basic', () => {
    const r = recall(myNodes, okReq('seq', '10-14'))
    return r.ok && JSON.stringify(r.seqs) === '[11,12,13,14]' &&
      r.text.includes('[seq 11 user]') && r.text.includes('[seq 14 assistant]') && r.truncated === false && r.tokens === estTokens(r.text)
  })

  check('recall_seq_budget_truncates_with_next_pointer', () => {
    const r = recall(myNodes, okReq('seq', '11-14'), { maxTokens: 40 })
    return r.ok && r.truncated === true && r.seqs.length >= 1 && r.seqs.length < 4 && r.seqs[0] === 11 &&
      r.text.trimEnd().endsWith(`[recall truncated — next: recall(type="seq", id="${r.seqs[r.seqs.length - 1] + 1}-14")]`)
  })

  check('recall_first_node_too_big_is_cut_not_dropped', () => {
    const big = [{ seq: 1, message: { role: 'user', content: [T('z'.repeat(6000))] } }, { seq: 2, message: { role: 'user', content: [T('w')] } }]
    const r = recall(big, okReq('seq', '1-2'), { maxTokens: 80 })
    return r.ok && r.truncated === true && r.seqs[0] === 1 && r.text.includes('zzzz') && r.text.length < 800
  })

  check('recall_result_by_result_seq_and_by_call_seq', () => {
    const a = recall(myNodes, okReq('result', 13))
    const b = recall(myNodes, okReq('result', 12))
    const c = recall(myNodes, okReq('result', 14))
    return a.ok && a.text.includes('retry_limit = 5') && b.ok && JSON.stringify(b.seqs) === '[13]' && c.ok === false && c.text.startsWith('NOT FOUND')
  })

  check('recall_checkpoint_ordinal', () => {
    const a = recall(myNodes, okReq('checkpoint', 1))
    const b = recall(myNodes, okReq('checkpoint', 2))
    const c = recall(myNodes, okReq('checkpoint', 3))
    return a.ok && a.text.includes('CHECKPOINT-ALPHA') && !a.text.includes('CHECKPOINT-BETA') &&
      b.ok && b.text.includes('CHECKPOINT-BETA') && c.ok === false
  })

  check('recall_not_found_names_present_range', () => {
    const r = recall(myNodes, okReq('seq', '500-600'))
    return r.ok === false && r.text.startsWith('NOT FOUND') && r.text.includes('seqs 11-17') && r.seqs.length === 0 && r.truncated === false
  })

  check('search_literal_not_regex_and_case_insensitive', () => {
    const a = search(myNodes, 'x.y+z')
    const b = search(myNodes, 'RETRY_LIMIT')
    return a.ok && a.total === 1 && a.hits[0].seq === 13 && b.ok && b.total === 3 && JSON.stringify(b.hits.map((h) => h.seq)) === '[11,13,14]'
  })

  check('search_regex_and_invalid_regex_and_empty', () => {
    const a = search(myNodes, 'retry_limit\\s*=\\s*\\d+', { regex: true })
    const b = search(myNodes, '(unclosed', { regex: true })
    const c = search(myNodes, '   ')
    return a.ok && a.total === 1 && b.ok === false && typeof b.error === 'string' && c.ok === false
  })

  check('search_skips_reasoning', () => search(myNodes, 'HIDDEN-THOUGHT').total === 0)

  check('search_cap_and_snippet_shape', () => {
    const many = Array.from({ length: 60 }, (_, i) => ({ seq: i + 1, message: { role: 'assistant', content: [T(`${'a'.repeat(300)}\n\tNEEDLE\n${'b'.repeat(300)}`)] } }))
    const r = search(many, 'needle', { maxHits: 5, contextChars: 10 })
    const s = r.hits[0].snippet
    return r.ok && r.total === 60 && r.hits.length === 5 && r.capped === true && !/[\n\t]/.test(s) && s.includes('NEEDLE') && s.startsWith('…') && s.endsWith('…')
  })

  check('render_search_variants', () => {
    const r = search(myNodes, 'retry_limit')
    const s = renderSearch(r, 'retry_limit')
    const lines = s.trimEnd().split('\n')
    const zero = renderSearch(search(myNodes, 'nothingmatcheshere'), 'nothingmatcheshere')
    const err = renderSearch(search(myNodes, '(unclosed', { regex: true }), '(unclosed')
    return lines[lines.length - 1] === 'NEXT STEP: recall(type="seq", id="11")' && s.includes('3 hits for "retry_limit"') &&
      s.includes('seq 13 [user]') && zero.includes('0 hits') && zero.includes('nothingmatcheshere') && err.startsWith('search error: ')
  })

  check('keyword_index_basic', () => {
    const idx = foldKeywordIndex(myNodes)
    const terms = idx.map((x) => x.term)
    const cfg = idx.find((x) => x.term === 'config/app_settings.json')
    const rl = idx.find((x) => x.term === 'retry_limit')
    return !!cfg && JSON.stringify(cfg.seqs) === '[11,12,14]' && !!rl && JSON.stringify(rl.seqs) === '[11,13,14]' &&
      !terms.includes('this') && terms.indexOf('config/app_settings.json') < terms.indexOf('retry_limit')
  })

  check('keyword_index_limits_and_render', () => {
    const many = Array.from({ length: 30 }, (_, i) => ({ seq: i + 1, message: { role: 'user', content: [T(`shared thing${i % 3}zz`)] } }))
    const idx = foldKeywordIndex(many, { maxTerms: 2, maxSeqsPerTerm: 3 })
    const txt = renderKeywordIndex(idx)
    return idx.length === 2 && idx[0].term === 'shared' && JSON.stringify(idx[0].seqs) === '[1,2,3]' &&
      txt.split('\n')[0] === 'KEYWORD INDEX (term → seqs; use recall):' && txt.includes('shared: 1, 2, 3') && renderKeywordIndex([]) === ''
  })

  check('rewrite_cost_fields_and_tiers', () => {
    const a = rewriteCost({ surfaceTokens: 30000, shadowedTokens: 20000, replacementTokens: 2000, window: 32768, cachedPrefixTokens: 25000 })
    const b = rewriteCost({ surfaceTokens: 8000, shadowedTokens: 3000, replacementTokens: 1000, window: 32768 })
    const t = (s) => rewriteCost({ surfaceTokens: s, shadowedTokens: 10, replacementTokens: 5, window: 1000 }).tier
    return a.saved === 18000 && a.tier === 'emergency' && a.worthIt === true && a.pressureBefore === 0.9155 &&
      a.pressureAfter === 0.3662 && a.reprefillTokens === 7000 &&
      b.tier === 'none' && b.worthIt === false && b.reprefillTokens === 6000 &&
      t(299) === 'none' && t(300) === 'watch' && t(500) === 'idle' && t(700) === 'compact' && t(900) === 'emergency'
  })

  check('rewrite_cost_error_on_bad_window', () => {
    const c = rewriteCost({ surfaceTokens: 1, shadowedTokens: 1, replacementTokens: 0, window: 0 })
    const d = rewriteCost({ surfaceTokens: NaN, shadowedTokens: 1, replacementTokens: 0, window: 10 })
    const e = rewriteCost({})
    return typeof c.error === 'string' && typeof d.error === 'string' && typeof e.error === 'string'
  })

  check('never_throws_on_garbage_inputs', () => {
    renderMessage({})
    recall([], okReq('seq', 1))
    recall(null, okReq('seq', 1))
    recall(myNodes, { ok: false })
    search(null, 'x')
    search(myNodes, null)
    foldKeywordIndex(null)
    foldKeywordIndex(myNodes, {})
    rewriteCost(null)
    rewriteCost(undefined)
    return true
  })

  let passed = 0
  for (const [name, fn] of checks) {
    let ok = false
    try { ok = fn() === true } catch { ok = false }
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`)
    if (ok) passed += 1
  }
  const failed = checks.length - passed
  console.log(`cg_recall selftest: ${checks.length} checks, ${passed} passed, ${failed} failed`)
  process.exit(failed === 0 ? 0 : 1)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--selftest')) {
  runSelftest()
}
