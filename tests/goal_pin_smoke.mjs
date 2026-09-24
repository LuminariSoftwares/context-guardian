// Contract smoke for the pinned-goal feature of engine.js.
// usage: node tests/goal_pin_smoke.mjs      (exit 0 iff all checks pass)
import { GOAL_CLOSE, GOAL_OPEN, GOAL_UPDATE_RE, buildCheckpoint, extractGoal, renderGoal, resolveEngineOptions } from '../engine.js'
import { RECALL_GUIDE } from '../vendor/compiler.js'

const T = (text) => ({ type: 'text', text })
const user = (seq, text) => ({ seq, message: { role: 'user', content: [T(text)] } })
const assistant = (seq, text) => ({ seq, message: { role: 'assistant', content: [T(text)] } })
const checkpoint = (seq, text) => ({ seq, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T(text)] } })
const options = () => resolveEngineOptions({ userTextTokens: 50 }, {})

const GOAL_BLOCK = new RegExp(`${GOAL_OPEN}\n([\\s\\S]*?)\n${GOAL_CLOSE}`)
const firstGoalOf = (text) => { const m = text.match(GOAL_BLOCK); return m === null ? null : m[1] }
const countOf = (text, needle) => text.split(needle).length - 1

// A first request that is far longer than userTextTokens allows, with non-ASCII
// characters and blank lines, so byte-for-byte survival is non-trivial.
let REQUEST = ''
while (REQUEST.length < 6000) REQUEST += `Línea ${REQUEST.length} — fix café/naïve ñ «test»\n\n`
REQUEST = REQUEST.slice(0, 6000)

const checks = []
const check = (name, fn) => checks.push([name, fn])

check('first_request_survives_first_checkpoint_byte_for_byte', () => {
  const text = buildCheckpoint([user(1, REQUEST)], options(), 0.8).text
  return firstGoalOf(text) === REQUEST
})

check('first_request_is_longer_than_user_text_cap', () => {
  const opts = options()
  return REQUEST.length === 6000 && REQUEST.length > opts.userTextTokens
})

check('second_compaction_keeps_request_and_does_not_duplicate', () => {
  const first = buildCheckpoint([user(1, REQUEST)], options(), 0.8).text
  // region holds the first checkpoint as a checkpoint node plus new turns
  const region = [checkpoint(1, first), assistant(2, 'working on it'), user(3, 'next please')]
  const second = buildCheckpoint(region, options(), 0.8).text
  return second.includes(REQUEST) && firstGoalOf(second) === REQUEST && countOf(second, GOAL_OPEN) === 1
})

check('goal_update_becomes_update_and_survives_second_compaction', () => {
  const first = buildCheckpoint([user(1, REQUEST)], options(), 0.8).text
  const region = [checkpoint(1, first), user(2, 'Goal: also do X'), assistant(3, 'ok')]
  const second = buildCheckpoint(region, options(), 0.8).text
  const updateBlock = `${GOAL_OPEN} update seq 2\nGoal: also do X\n${GOAL_CLOSE}`
  return second.includes(updateBlock) && countOf(second, GOAL_OPEN) === 2
})

check('tool_result_and_injected_user_messages_are_never_the_goal', () => {
  const nodes = [
    { seq: 1, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'c1', content: [T('TOOL OUTPUT')] }] } },
    { seq: 2, message: { role: 'user', source: { kind: 'agent-instructions' }, content: [T('INJECTED INSTRUCTIONS')] } },
    { seq: 3, message: { role: 'user', source: { kind: 'plugin', plugin: 'skill-catalog' }, content: [T('INJECTED SKILLS')] } },
  ]
  const extracted = extractGoal(nodes)
  const text = buildCheckpoint(nodes, options(), 0.8).text
  return extracted.goal === null && extracted.updates.length === 0 && !text.includes(GOAL_OPEN) && !text.includes('INJECTED')
})

check('no_user_text_means_no_goal_block_and_unchanged_checkpoint', () => {
  const nodes = [assistant(1, 'assistant only'), { seq: 2, message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'c1', content: [T('RESULT')] }] } }]
  const text = buildCheckpoint(nodes, options(), 0.8).text
  // before the goal-pin change the text was: header, RECALL_GUIDE, body -- no goal markers at all
  return !text.includes('[pinned goal') && !text.includes(GOAL_OPEN) && text.includes(RECALL_GUIDE)
})

check('goal_block_sits_before_recall_guide', () => {
  const text = buildCheckpoint([user(1, 'do the thing')], options(), 0.8).text
  const pinned = text.indexOf("[pinned goal -- the session's first request")
  return pinned !== -1 && text.indexOf(RECALL_GUIDE) !== -1 && pinned < text.indexOf(RECALL_GUIDE)
})

check('checkpoint_goal_wins_over_a_later_user_message', () => {
  const goalText = renderGoal({ goal: 'CHECKPOINT GOAL', updates: [] })
  const region = [checkpoint(1, `header\n${goalText}\n${RECALL_GUIDE}`)]
  const allNodes = [user(9, 'a later user request')]
  const text = buildCheckpoint(region, options(), 0.8, allNodes).text
  return firstGoalOf(text) === 'CHECKPOINT GOAL'
})

check('updates_are_deduped_by_seq_first_wins_and_sorted', () => {
  const mk = (nodeSeq, updateSeq, body) => ({ seq: nodeSeq, message: { role: 'user', source: { kind: 'plugin', plugin: 'compact' }, content: [T(`${GOAL_OPEN} update seq ${updateSeq}\n${body}\n${GOAL_CLOSE}`)] } })
  const nodes = [mk(1, 9, 'NINE-A'), mk(2, 2, 'TWO'), mk(3, 9, 'NINE-B')]
  const { goal, updates } = extractGoal(nodes)
  return goal === null && updates.map(u => u.seq).join(',') === '2,9' && updates.find(u => u.seq === 9).text === 'NINE-A'
})

check('first_user_text_node_is_the_fallback_goal', () => {
  const extracted = extractGoal([user(1, 'the real first request'), user(2, 'Goal: tweak it')])
  return extracted.goal === 'the real first request' && extracted.updates.length === 1 && extracted.updates[0].seq === 2
})

check('exported_constants_match_the_contract', () => {
  return GOAL_OPEN === '<<<GOAL' && GOAL_CLOSE === 'GOAL>>>' && GOAL_UPDATE_RE.test('  Goal: x') && !GOAL_UPDATE_RE.test('not a goal')
})

let passed = 0
for (const [name, fn] of checks) {
  let ok = false
  let err = ''
  try { ok = fn() === true } catch (error) { err = ` (error: ${error.message})` }
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${err}`)
  if (ok) passed += 1
}
console.log(`goal_pin_smoke: ${checks.length} checks, ${passed} passed`)
process.exit(passed === checks.length ? 0 : 1)
