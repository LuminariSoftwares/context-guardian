// cg_memory.js -- durable, deterministic long-term memory for context-guardian checkpoints.
// Six categories of short facts pulled out of conversation NODES with no model call, merged into
// one JSON file after every compaction and rendered as a budgeted block at the top of a checkpoint.
// ESM, Node >= 22, zero third-party imports. Exports: MEMORY_REV, MEMORY_VERSION, CATEGORIES, emptyMemory,
// normalizeText, extractMemory, mergeMemory, renderMemory, memoryStats, itemsForSession, loadMemory, saveMemory.
import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { applyProvenance, renderPinText } from './cg_provenance.js'
import { CHARS_PER_TOKEN } from './cg_recall.js'

/** Revision tag for this memory implementation. */
export const MEMORY_REV = 'cg-memory-2'  // 2026-10-01: pins category + session-scoped render

/** Schema version of the on-disk memory document. */
export const MEMORY_VERSION = 1

/** The seven memory categories, in canonical order. `pins` = facts the user explicitly asked to keep (2026-10-01). */
export const CATEGORIES = Object.freeze(['pins', 'decisions', 'constraints', 'files', 'todos', 'errors', 'preferences'])

/** Per-category item caps applied by mergeMemory. */
export const DEFAULT_MAX_PER_CATEGORY = Object.freeze({
  pins: 60,
  decisions: 40,
  constraints: 40,
  files: 200,
  todos: 40,
  errors: 20,
  preferences: 40,
})

/** Rendered block order: also the budget admission priority order. */
const ORDER = Object.freeze(['pins', 'constraints', 'preferences', 'decisions', 'todos', 'files', 'errors'])

/** Categories that belong to ONE session: rendered only for that session, so one run's facts never leak into another. */
const SESSION_SCOPED = new Set(['pins', 'files', 'errors', 'todos'])

/** Default render budget, in estimated tokens. */
const DEFAULT_MAX_TOKENS = 1200

/** Header line of the rendered memory block. */
const HEADER = '[memory -- durable notes kept across compactions and sessions by context-guardian; newest last]'

const MAX_TEXT = 300
const MAX_PIN_TEXT = 400

/**
 * A user line that asks for something to be kept (2026-10-01, bench1001: 'Important, keep this for later in the
 * conversation: ...' matched no pattern, so 12 user-declared facts were never saved and the stock summary lost them).
 * Deliberately narrow: an explicit keep/remember request, not every line that says 'important'.
 */
const PIN_RE = /\b(?:keep\s+(?:this|that|these|it)(?:\s+\w+){0,3}\s+for\s+later|for\s+later\s+(?:use|reference)|remember\s+(?:this|that|these)|(?:do\s+not|don't)\s+forget|make\s+a\s+note|note\s+(?:this|that)\s+for\s+later|pin\s+(?:this|that))\b/i
const MAX_ERROR_TEXT = 200

/** Explicit `prefix: fact` markers, matched at line start after bullet removal. */
const PREFIXES = [
  [/^decision:\s*/i, 'decisions'],
  [/^decided:\s*/i, 'decisions'],
  [/^remember:\s*/i, 'decisions'],
  [/^constraint:\s*/i, 'constraints'],
  [/^rule:\s*/i, 'constraints'],
  [/^next\s+step:\s*/i, 'todos'],
  [/^todo:\s*/i, 'todos'],
  [/^next:\s*/i, 'todos'],
  [/^error:\s*/i, 'errors'],
  [/^preference:\s*/i, 'preferences'],
  [/^prefer:\s*/i, 'preferences'],
]

/** User-only strong patterns; the whole candidate line is the fact. */
const STRONG = [
  [/^(?:please\s+)?(?:never|always|do not|don't|must not|must)\b/i, 'constraints'],
  [/^i\s*(?:prefer|like|want|'d rather|would rather)\b/i, 'preferences'],
  [/^(?:let'?s|we(?:'ll| will)|we are going to|we're going to)\s+(?:use|go with|keep|switch to|stick with)\b/i, 'decisions'],
]

const BULLET_RE = /^(?:[-*+]\s+|\d+[.)]\s+)/
const CHECKBOX_RE = /^[-*+]\s*\[([ xX])\]\s*(.*)$/
const ERROR_RE = /\b(?:Error|Exception|Traceback|FAILED|ENOENT|EACCES|EPERM)\b/
const FILES_WRITTEN_RE = /^files\s+written\s*\(latest last\)\s*:\s*(.*)$/i
const FILES_LINE_RE = /^files:\s*(.*)$/i
const SEQ_TAG_RE = /^(.+?) \(seq (\d+)\)$/
const WRITE_CALL_RE = /write|edit|patch|create|replace/i

/** Trim, then hard-cap a fact at `max` chars by cutting and appending an ellipsis. */
function clamp(text, max = MAX_TEXT) {
  const s = String(text ?? '').trim()
  return s.length > max ? s.slice(0, max - 3) + '...' : s
}

/** A fresh, empty memory document. */
export function emptyMemory() {
  return { version: MEMORY_VERSION, updated: null, items: [] }
}

/** Rough token estimate: ceil(chars / CHARS_PER_TOKEN), the package-wide constant from cg_recall.js. */
export function estTokens(text) {
  return Math.ceil(String(text).length / CHARS_PER_TOKEN)
}

/** Lowercase, collapse whitespace runs, trim, strip trailing sentence punctuation. */
export function normalizeText(text) {
  if (text === null || text === undefined) return ''
  return String(text).toLowerCase().replace(/\s+/g, ' ').trim().replace(/[.,;:!]+$/, '')
}

/** Merge key for an item: category plus normalized fact (paths stay case-sensitive). */
function mergeKey(cat, text, session) {
  const scope = cat === 'pins' ? String(session ?? '') : ''
  return cat + '\u0000' + scope + '\u0000' + (cat === 'files' ? String(text) : normalizeText(text))
}

/** Oldest first: smaller `last`, then smaller seq. */
function oldestFirst(a, b) {
  const la = a.last || ''
  const lb = b.last || ''
  if (la !== lb) return la < lb ? -1 : 1
  return (a.seq || 0) - (b.seq || 0)
}

/** Newest first: larger `last`, then larger seq. */
function newestFirst(a, b) {
  return -oldestFirst(a, b)
}

/** Validate and copy one item, or null when it cannot be stored. */
function normalizeItem(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  if (CATEGORIES.indexOf(raw.cat) < 0) return null
  if (typeof raw.text !== 'string' || raw.text.trim() === '') return null
  return {
    cat: raw.cat,
    text: raw.text,
    seq: Number.isFinite(raw.seq) ? raw.seq : 0,
    session: typeof raw.session === 'string' ? raw.session : '',
    first: typeof raw.first === 'string' ? raw.first : null,
    last: typeof raw.last === 'string' ? raw.last : null,
    count: Number.isFinite(raw.count) && raw.count >= 1 ? Math.floor(raw.count) : 1,
    done: raw.done === true,
  }
}

/** Deep-copy a memory document; anything invalid becomes emptyMemory(). */
function sanitizeMemory(memory) {
  if (!memory || typeof memory !== 'object' || Array.isArray(memory) || !Array.isArray(memory.items)) {
    return emptyMemory()
  }
  const items = []
  for (const raw of memory.items) {
    const it = normalizeItem(raw)
    if (it) items.push(it)
  }
  return {
    version: MEMORY_VERSION,
    updated: typeof memory.updated === 'string' ? memory.updated : null,
    items,
  }
}

/** Build a fresh extract item. */
function makeItem(cat, text, seq, session, done = false) {
  return { cat, text, seq, session, first: null, last: null, count: 1, done }
}

/** Rule 1-3: candidate lines out of one text block. */
function fromText(text, seq, session, isUser, out) {
  for (const rawLine of String(text ?? '').split('\n')) {
    const line = rawLine.trim()
    if (line.length < 8) continue
    const box = line.match(CHECKBOX_RE)
    if (box) {
      const body = box[2].trim()
      if (body !== '') out.push(makeItem('todos', clamp(body), seq, session, box[1] !== ' '))
      continue
    }
    const bare = line.replace(BULLET_RE, '')
    if (isUser) {
      const pin = bare.match(PIN_RE)
      if (pin) {
        // Keep from the start of the sentence that holds the request: the facts follow it.
        const cut = Math.max(bare.lastIndexOf('. ', pin.index) + 2, 0)
        out.push(makeItem('pins', clamp(bare.slice(cut), MAX_PIN_TEXT), seq, session))
        continue
      }
    }
    let hit = false
    for (const pair of PREFIXES) {
      const m = bare.match(pair[0])
      if (!m) continue
      hit = true
      const body = bare.slice(m[0].length).trim()
      if (body !== '') out.push(makeItem(pair[1], clamp(body), seq, session))
      break
    }
    if (hit) continue
    if (!isUser) continue
    for (const pair of STRONG) {
      if (pair[0].test(bare)) {
        out.push(makeItem(pair[1], clamp(bare), seq, session))
        break
      }
    }
  }
}

/** Rule 4: one errors item out of a tool-result that failed. */
function fromToolResult(block, seq, session, out) {
  const inner = Array.isArray(block.content) ? block.content : []
  const parts = []
  for (const b of inner) {
    if (b && typeof b === 'object' && b.type === 'text') parts.push(String(b.text ?? ''))
  }
  const lines = parts.join('\n').split('\n').map((l) => l.trim())
  const hit = lines.find((l) => ERROR_RE.test(l))
  if (hit === undefined && block.isError !== true) return
  const chosen = hit !== undefined ? hit : (lines.find((l) => l !== '') || '')
  if (chosen === '') return
  out.push(makeItem('errors', clamp(chosen, MAX_ERROR_TEXT), seq, session))
}

/** Rule 5: a files item out of a mutating tool call. */
function fromToolCall(block, seq, session, out) {
  const name = typeof block.name === 'string' ? block.name : ''
  if (!WRITE_CALL_RE.test(name)) return
  let args = block.arguments
  if (typeof args === 'string') {
    try { args = JSON.parse(args) } catch { return }
  }
  if (!args || typeof args !== 'object' || Array.isArray(args)) return
  const p = args.path ?? args.file_path ?? args.filePath ?? args.target
  if (typeof p !== 'string' || p.trim() === '') return
  out.push(makeItem('files', clamp(p.trim()), seq, session))
}

/** Rule 6: files items recorded by an earlier checkpoint's rendered block. */
function fromCheckpoint(text, seq, session, out) {
  for (const rawLine of String(text ?? '').split('\n')) {
    const line = rawLine.trim()
    const written = line.match(FILES_WRITTEN_RE)
    if (written) {
      for (const piece of written[1].split(', ')) {
        const tag = piece.trim().match(SEQ_TAG_RE)
        if (tag) out.push(makeItem('files', clamp(tag[1]), Number(tag[2]), session))
      }
      continue
    }
    const rendered = line.match(FILES_LINE_RE)
    if (rendered) {
      for (const piece of rendered[1].split(', ')) {
        const p = piece.trim()
        if (p !== '') out.push(makeItem('files', clamp(p), seq, session))
      }
    }
  }
}

/** Pull durable facts out of conversation nodes, in node order. */
export function extractMemory(nodes, opts = {}) {
  const session = opts && typeof opts.session === 'string' ? opts.session : ''
  const out = []
  if (!Array.isArray(nodes)) return out
  for (const node of nodes) {
    if (!node || typeof node !== 'object') continue
    const message = node.message
    if (!message || typeof message !== 'object') continue
    const seq = Number.isFinite(node.seq) ? node.seq : 0
    const source = message.source
    const injected = source !== undefined && source !== null
    const isCheckpoint = injected && typeof source === 'object' &&
      source.kind === 'plugin' && source.plugin === 'compact'
    const content = Array.isArray(message.content) ? message.content : []
    const first = content[0]
    const firstIsBlock = first !== null && typeof first === 'object'
    const isToolResultNode = firstIsBlock && first.type === 'tool-result'
    const isUserText = message.role === 'user' && !injected && !isCheckpoint && firstIsBlock && first.type === 'text'
    const isAssistant = message.role === 'assistant' && !isCheckpoint
    for (const block of content) {
      if (!block || typeof block !== 'object') continue
      if (block.type === 'tool-call') {
        fromToolCall(block, seq, session, out)
        continue
      }
      if (block.type !== 'text') continue
      if (isCheckpoint) fromCheckpoint(String(block.text ?? ''), seq, session, out)
      else if (isUserText || isAssistant) fromText(block.text, seq, session, isUserText, out)
    }
    if (isToolResultNode) fromToolResult(first, seq, session, out)
  }
  return out
}

/** Merge items into a NEW memory document; neither argument is mutated. */
export function mergeMemory(memory, items, opts = {}) {
  const now = opts && typeof opts.now === 'string' ? opts.now : new Date().toISOString()
  const caps = Object.assign({}, DEFAULT_MAX_PER_CATEGORY, (opts && opts.maxPerCategory) || {})
  const next = sanitizeMemory(memory)
  const index = new Map()
  for (const it of next.items) index.set(mergeKey(it.cat, it.text, it.session), it)
  if (Array.isArray(items)) {
    for (const raw of items) {
      const it = normalizeItem(raw)
      if (!it) continue
      const key = mergeKey(it.cat, it.text, it.session)
      const existing = index.get(key)
      if (existing) {
        existing.count += 1
        existing.last = now
        existing.seq = it.seq
        existing.session = it.session
        existing.done = existing.done || it.done
        continue
      }
      const added = {
        cat: it.cat,
        text: it.text,
        seq: it.seq,
        session: it.session,
        first: now,
        last: now,
        count: 1,
        done: it.done,
      }
      next.items.push(added)
      index.set(key, added)
    }
  }
  for (const cat of CATEGORIES) {
    const cap = caps[cat]
    if (typeof cap !== 'number' || !Number.isFinite(cap) || cap < 0) continue
    const ofCat = next.items.filter((it) => it.cat === cat)
    if (ofCat.length <= cap) continue
    const ranked = ofCat.slice().sort(oldestFirst)
    for (let i = 0; i < ofCat.length - cap; i++) {
      const victim = ranked[i]
      const at = next.items.indexOf(victim)
      if (at >= 0) next.items.splice(at, 1)
    }
  }
  return { version: MEMORY_VERSION, updated: now, items: next.items }
}

/** One rendered line for a single item. */
function renderLine(cat, item) {
  if (cat === 'todos') return '- [ ] ' + item.text
  if (cat === 'errors') return '- ' + item.text + ' (seq ' + item.seq + ')'
  return '- ' + item.text
}

/** Category header line, or '' when this category is not rendered. */
function renderHeader(cat) {
  if (cat === 'errors') return 'errors (latest):'
  if (cat === 'pins') return 'pinned by the user (verbatim -- keep; answer from these, never from memory of the summary):'
  return cat + ':'
}

/** Render the memory as a budgeted block for the top of a checkpoint. */
export function renderMemory(memory, opts = {}) {
  const max = opts && Number.isFinite(opts.maxTokens) ? opts.maxTokens : DEFAULT_MAX_TOKENS
  // A session id scopes pins/files/errors/todos to that session; no id keeps the old, unscoped behaviour.
  const session = opts && typeof opts.session === 'string' && opts.session !== '' ? opts.session : null
  const items = []
  const raw = memory && typeof memory === 'object' && Array.isArray(memory.items) ? memory.items : []
  for (const r of raw) {
    const it = normalizeItem(r)
    if (!it) continue
    if (session !== null && SESSION_SCOPED.has(it.cat) && it.session !== session && it.session !== '') continue
    items.push(it)
  }
  // C-Pin provenance (2026-10-03): a pinned value that a LATER pin changed never renders again,
  // and a pin whose every keyed fact changed is retired. memory.json keeps them all (audit).
  const provenance = applyProvenance(items.filter((it) => it.cat === 'pins'))
  for (let i = items.length - 1, k = provenance.length - 1; i >= 0; i--) {
    if (items[i].cat !== 'pins') continue
    const p = provenance[k--]
    if (p.superseded) items.splice(i, 1)
    else items[i] = Object.assign({}, items[i], { text: renderPinText(p) })
  }
  if (items.length === 0) return ''
  const acc = [HEADER]
  const admitted = { pins: [], constraints: [], preferences: [], decisions: [], todos: [], errors: [], files: [] }
  for (const cat of ORDER) {
    if (cat === 'files') {
      const cands = items.filter((it) => it.cat === 'files').sort(newestFirst)
      const picked = []
      for (const c of cands) {
        const line = 'files: ' + picked.concat([c.text]).join(', ')
        const base = picked.length === 0 ? acc : acc.slice(0, -1)
        if (estTokens(base.concat([line]).join('\n')) <= max) {
          picked.push(c.text)
          if (picked.length === 1) acc.push(line)
          else acc[acc.length - 1] = line
        } else break
      }
      if (picked.length > 0) admitted.files = picked
      continue
    }
    const cands = items.filter((it) => it.cat === cat && !(cat === 'todos' && it.done)).sort(newestFirst)
    let opened = false
    for (const c of cands) {
      const line = renderLine(cat, c)
      const base = opened ? acc : acc.concat([renderHeader(cat)])
      if (estTokens(base.concat([line]).join('\n')) <= max) {
        if (!opened) {
          acc.push(renderHeader(cat))
          opened = true
        }
        admitted[cat].push(c)
        acc.push(line)
      } else break
    }
  }
  const lines = [HEADER]
  for (const cat of ORDER) {
    const picked = admitted[cat]
    if (picked.length === 0) continue
    if (cat === 'files') {
      lines.push('files: ' + picked.slice().reverse().join(', '))
      continue
    }
    lines.push(renderHeader(cat))
    for (const it of picked.slice().sort(oldestFirst)) lines.push(renderLine(cat, it))
  }
  if (lines.length === 1) return ''
  return lines.join('\n')
}

/** Item counts per category plus the number of open todos. */
export function memoryStats(memory) {
  const byCategory = {}
  for (const cat of CATEGORIES) byCategory[cat] = 0
  const items = memory && typeof memory === 'object' && Array.isArray(memory.items) ? memory.items : []
  let total = 0
  let openTodos = 0
  for (const it of items) {
    const item = normalizeItem(it)
    if (!item) continue
    total += 1
    byCategory[item.cat] += 1
    if (item.cat === 'todos' && !item.done) openTodos += 1
  }
  return { total, byCategory, openTodos }
}

/** The stored items belonging to one session. */
export function itemsForSession(memory, session) {
  const items = memory && typeof memory === 'object' && Array.isArray(memory.items) ? memory.items : []
  const out = []
  for (const raw of items) {
    const it = normalizeItem(raw)
    if (it && it.session === session) out.push(it)
  }
  return out
}

/** Rename an unreadable memory file aside, best effort. */
function quarantine(path) {
  try {
    renameSync(path, path + '.corrupt-' + Date.now())
  } catch {
    // best effort only
  }
}

/** Read a memory file; never throws. */
export function loadMemory(path) {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (err) {
    if (err && err.code === 'ENOENT') return { memory: emptyMemory(), status: 'missing' }
    return { memory: emptyMemory(), status: 'corrupt', error: String((err && err.message) || err) }
  }
  let data
  try {
    data = JSON.parse(raw)
  } catch (err) {
    quarantine(path)
    return { memory: emptyMemory(), status: 'corrupt', error: String((err && err.message) || err) }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(data.items)) {
    quarantine(path)
    return { memory: emptyMemory(), status: 'corrupt', error: 'memory file has no items array' }
  }
  const items = []
  for (const it of data.items) {
    const item = normalizeItem(it)
    if (item) items.push(item)
  }
  return {
    memory: { version: MEMORY_VERSION, updated: typeof data.updated === 'string' ? data.updated : null, items },
    status: 'loaded',
  }
}

/** Write a memory file atomically; never throws. */
export function saveMemory(path, memory) {
  let tmp = null
  try {
    mkdirSync(dirname(path), { recursive: true })
    tmp = path + '.tmp-' + process.pid
    writeFileSync(tmp, JSON.stringify(memory, null, 2) + '\n', 'utf8')
    renameSync(tmp, path)
    return true
  } catch {
    if (tmp !== null) {
      try { unlinkSync(tmp) } catch { /* best effort only */ }
    }
    return false
  }
}
