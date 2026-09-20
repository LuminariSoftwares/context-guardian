// DSH-side smoke for the bundle entry. Run from the repo root after `pnpm install`:
//   node tests/dsh_smoke.mjs
// Uses a fake ctx (no DSH boot) and the real Python bridge (hello + spans only).
import * as plugin from 'dsh-context-guardian'
let pass = 0, total = 0
const check = (name, cond) => { total++; if (cond) pass++; console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}`) }
const throws = (fn) => { try { fn(); return false } catch { return true } }
const cfg = plugin.Config({})
check('name_and_namespace', plugin.name === 'dsh-context-guardian' && plugin.SETTINGS_NAMESPACE === 'context-guardian')
check('schema_defaults', cfg.numCtx === 32768 && cfg.compactThreshold === 0.85 && cfg.idleCompactRatio === 0.45 && cfg.maxRecallTokens === 16000 && cfg.maxSearchHits === 50)
check('schema_refuses_out_of_range_ratio', throws(() => plugin.Config({ compactThreshold: 1.5 })))
check('env_beats_section', plugin.resolveOptions(cfg, { GUARDIAN_NUM_CTX: '8192', GUARDIAN_COMPACT_THRESHOLD: '0.7' }).numCtx === 8192
  && plugin.resolveOptions(cfg, { GUARDIAN_COMPACT_THRESHOLD: '0.7' }).compactThreshold === 0.7)
check('bad_env_is_ignored_not_half_applied', plugin.resolveOptions(cfg, { GUARDIAN_NUM_CTX: 'lots', GUARDIAN_COMPACT_THRESHOLD: '7' }).numCtx === 32768
  && plugin.resolveOptions(cfg, { GUARDIAN_COMPACT_THRESHOLD: '7' }).compactThreshold === 0.85)
check('empty_toolArgTools_means_compiler_default', plugin.resolveOptions(cfg, {}).toolArgTools.length > 0
  && plugin.resolveOptions({ ...cfg, toolArgTools: ['read'] }, {}).toolArgTools.join() === 'read')
check('idle_must_be_below_hard', throws(() => plugin.validateSection({ ...cfg, idleCompactRatio: 0.9 })) && !throws(() => plugin.validateSection(cfg)))
const disposers = []; const injected = []; const logs = []; const listeners = []
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(l => [l, m => logs.push(`${l}: ${m}`)]))
const refuse = what => () => { throw new Error(`phase 1 must not ${what}`) }
const ctx = { logger, inject: (deps, cb) => injected.push({ deps, cb }), effect: fn => disposers.push(fn()), on: (...a) => listeners.push(a), tools: { register: refuse('register tools') } }
process.chdir('/')
plugin.apply(ctx, cfg)
check('apply_refuses_bad_row', throws(() => plugin.apply(ctx, { ...cfg, idleCompactRatio: 0.95 })))
check('no_listeners_in_phase_1', listeners.length === 0)
let section = null
injected[0].cb({ settings: { installSection: (owner, ns, schema, entry, hooks) => { section = { owner, ns, hooks } } } })
check('settings_section_with_validate', injected[0].deps.join() === 'settings' && section.ns === 'context-guardian' && typeof section.hooks.validate === 'function')
check('load_line_names_compiler_rev', logs.some(l => l.startsWith('info: context-guardian scaffold loaded (compiler vcc-')))
const bridge = new plugin.PythonBridge(plugin.resolveOptions(cfg, {}), logger)
const hello = await bridge.request('hello')
const spans = await bridge.request('spans', { limit: 3 })
check('bridge_hello_and_spans', hello.bridge === '0.1.0' && typeof spans.exists === 'boolean' && Array.isArray(spans.spans))
bridge.stop()
disposers[0]()
await new Promise(r => setTimeout(r, 800))
check('no_error_logs', !logs.some(l => l.startsWith('error:')))
console.log(`dsh-context-guardian smoke: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total ? 0 : 1)
