/**
 * dsh-context-guardian -- Context Guardian as a native DSH bundle plugin.
 *
 * PHASE 1 SCAFFOLD. This file wires the plugin into DSH (config schema,
 * settings section, the vendored compiler, the optional Python bridge). It
 * does NOT touch compaction yet: no listener, no provider, no tools. Those
 * are Phase 3 / 3b and are marked below where they land. Until then
 * @deepseek-ai/dsh-compaction-basic behaves exactly as the profile has it.
 *
 * Written against DSH 0.1.2-alpha.2 (source read 2026-09-19):
 *   - settings:    packages/settings/settings -> ctx.settings.installSection(owner, ns, schema, entry, hooks)
 *   - compaction:  packages/compaction/compaction-basic -- `summarize()` is the
 *                  engine's sole subclass customization hook
 *   - log seam:    there is no "PreCompact" registration in this DSH; the
 *                  durable signal is a `session/event` whose type is
 *                  `compaction/start` (what dsh-openwolf listens to today)
 *
 * vendor/compiler.js and vendor/region.js are unmodified from
 * dsh-compaction-instant@0.1.4 (MIT, TsFreddie; VCC principle by lllyasviel) --
 * see vendor/LICENSE.dsh-compaction-instant. region.js imports
 * @deepseek-ai/dsh-compaction and @deepseek-ai/dsh-llm, so it is loaded only
 * where Phase 3b needs it, never at plugin load.
 *
 * MIT licensed.
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import Schema from '@deepseek-ai/schemastery'
import { COMPILER_REV, DEFAULT_ARG_TOOLS } from './vendor/compiler.js'

export const name = 'dsh-context-guardian'

/** Nothing is required at load in Phase 1; settings attaches when present. */
export const inject = []

/** Settings namespace (must match /^[a-z][a-z0-9-]*$/). */
export const SETTINGS_NAMESPACE = 'context-guardian'

/** Resolved from the module URL -- never from process.cwd(). */
const PACKAGE_ROOT = dirname(fileURLToPath(import.meta.url))
const BRIDGE_SCRIPT = join(PACKAGE_ROOT, 'modules', 'cg_bridge.py')

const ratio = () => Schema.number().min(0.05).max(0.99)

export const Config = Schema.object({
  python: Schema.string().default('')
    .description('Python for the optional bridge. Empty = $GUARDIAN_PYTHON, then a .venv beside this package, then python/python3 on PATH.'),
  numCtx: Schema.natural().default(32768)
    .description('Model context window the ratios are measured against (GUARDIAN_NUM_CTX overrides).'),
  compactThreshold: ratio().default(0.85)
    .description('Hard compaction trigger (GUARDIAN_COMPACT_THRESHOLD overrides).'),
  idleCompactRatio: ratio().default(0.45)
    .description('Proactive trigger when the agent goes idle (GUARDIAN_IDLE_COMPACT_RATIO overrides).'),
  keepRecentMessages: Schema.natural().default(8)
    .description('Most recent messages never compacted (GUARDIAN_KEEP_RECENT_MESSAGES overrides).'),
  reserveOutput: Schema.natural().default(8192)
    .description('Tokens held back for the reply (GUARDIAN_RESERVE_OUTPUT overrides).'),
  spanDir: Schema.string().default('')
    .description('Span archive. Empty = $GUARDIAN_SPAN_DIR, then the proxy\'s own default.'),
  maxRecallTokens: Schema.natural().default(16000)
    .description('Cap on one recall restore (GUARDIAN_MAX_RECALL_TOKENS overrides).'),
  maxSearchHits: Schema.natural().default(50)
    .description('Cap on one search (GUARDIAN_MAX_SEARCH_HITS overrides).'),
  toolArgTools: Schema.array(Schema.string()).default([])
    .description('Tools whose compiled line keeps its key argument. Empty = the compiler\'s DEFAULT_ARG_TOOLS.'),
  hideTools: Schema.array(Schema.string()).default([])
    .description('Tools that produce no compiled line at all.'),
})

/**
 * Fold the environment over a resolved section. GUARDIAN_* are the names
 * context_guardian.py and its .env already use.
 * Precedence: env > settings user layer > patch-row config > schema default.
 * A malformed or out-of-range env value is ignored, never half-applied.
 */
export function resolveOptions(section, env = process.env) {
  const int = (key, fallback) => {
    const raw = Number(env[key])
    return Number.isInteger(raw) && raw > 0 ? raw : fallback
  }
  const frac = (key, fallback) => {
    const raw = Number(env[key])
    return Number.isFinite(raw) && raw >= 0.05 && raw <= 0.99 ? raw : fallback
  }
  return {
    python: env.GUARDIAN_PYTHON?.trim() || section.python || defaultPython(),
    numCtx: int('GUARDIAN_NUM_CTX', section.numCtx),
    compactThreshold: frac('GUARDIAN_COMPACT_THRESHOLD', section.compactThreshold),
    idleCompactRatio: frac('GUARDIAN_IDLE_COMPACT_RATIO', section.idleCompactRatio),
    keepRecentMessages: int('GUARDIAN_KEEP_RECENT_MESSAGES', section.keepRecentMessages),
    reserveOutput: int('GUARDIAN_RESERVE_OUTPUT', section.reserveOutput),
    spanDir: env.GUARDIAN_SPAN_DIR?.trim() || section.spanDir || '',
    maxRecallTokens: int('GUARDIAN_MAX_RECALL_TOKENS', section.maxRecallTokens),
    maxSearchHits: int('GUARDIAN_MAX_SEARCH_HITS', section.maxSearchHits),
    toolArgTools: section.toolArgTools?.length > 0 ? [...section.toolArgTools] : [...DEFAULT_ARG_TOOLS],
    hideTools: [...(section.hideTools ?? [])],
  }
}

/** Cross-field rule the schema cannot express; refuses the WRITE that breaks it. */
export function validateSection(section) {
  if (section.idleCompactRatio >= section.compactThreshold) {
    throw new Error(`context-guardian: idleCompactRatio (${section.idleCompactRatio}) must be below compactThreshold (${section.compactThreshold})`)
  }
}

function defaultPython() {
  const venv = process.platform === 'win32'
    ? join(PACKAGE_ROOT, '.venv', 'Scripts', 'python.exe')
    : join(PACKAGE_ROOT, '.venv', 'bin', 'python')
  if (existsSync(venv)) return venv
  return process.platform === 'win32' ? 'python' : 'python3'
}

/** The optional Python child; see modules/cg_bridge.py for the op list. */
export class PythonBridge {
  constructor(options, logger) {
    this.options = options
    this.logger = logger
    this.child = null
    this.nextId = 1
    this.pending = new Map()
    this.stopped = false
  }

  ensureStarted() {
    if (this.child !== null) return
    if (this.stopped) throw new Error('context-guardian bridge is stopped')
    const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
    if (this.options.spanDir) env.GUARDIAN_SPAN_DIR ??= this.options.spanDir
    const child = spawn(this.options.python, [BRIDGE_SCRIPT], {
      cwd: PACKAGE_ROOT,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true, // no console window, no focus steal
    })
    this.child = child
    createInterface({ input: child.stdout }).on('line', line => this.onLine(line))
    createInterface({ input: child.stderr }).on('line', line => this.logger.debug(`[bridge] ${line}`))
    child.on('error', error => this.onExit(`spawn failed (${this.options.python}): ${error.message}`))
    child.on('exit', (code, signal) => this.onExit(`exited code=${code} signal=${signal}`))
  }

  onLine(line) {
    let frame
    try {
      frame = JSON.parse(line)
    } catch {
      this.logger.warn(`context-guardian bridge: non-JSON line on stdout dropped (${line.slice(0, 120)})`)
      return
    }
    const waiter = this.pending.get(frame.id)
    if (waiter === undefined) return
    this.pending.delete(frame.id)
    clearTimeout(waiter.timer)
    if (frame.ok === true) waiter.resolve(frame.result)
    else waiter.reject(new Error(String(frame.error ?? 'bridge error')))
  }

  onExit(reason) {
    if (this.child === null) return
    this.child = null
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer)
      waiter.reject(new Error(`context-guardian bridge ${reason}`))
    }
    this.pending.clear()
    if (!this.stopped) this.logger.error(`context-guardian bridge ${reason}`)
  }

  request(op, params = {}, timeoutMs = 30000) {
    this.ensureStarted()
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`context-guardian bridge: "${op}" timed out after ${timeoutMs}ms`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.child.stdin.write(`${JSON.stringify({ id, op, ...params })}\n`)
    })
  }

  stop() {
    this.stopped = true
    const child = this.child
    if (child === null) return
    try {
      child.stdin.write(`${JSON.stringify({ id: 0, op: 'shutdown' })}\n`)
      child.stdin.end()
    } catch { /* already gone */ }
    const killer = setTimeout(() => child.kill(), 3000)
    killer.unref()
    child.once('exit', () => clearTimeout(killer))
  }
}

export function apply(ctx, config) {
  validateSection(config) // a bad patch row fails the load, loudly

  let source = () => config
  let bridge = null

  const stopBridge = () => {
    if (bridge === null) return
    bridge.stop()
    bridge = null
  }

  /** Current options; every Phase 3 consumer reads through this, never a copy. */
  const options = () => resolveOptions(source())

  /** Spawned on first use only -- Phase 1 never calls it on its own. */
  // eslint-disable-next-line no-unused-vars
  const getBridge = () => {
    bridge ??= new PythonBridge(options(), ctx.logger)
    return bridge
  }

  ctx.inject(['settings'], (settingsCtx) => {
    settingsCtx.settings.installSection(ctx, SETTINGS_NAMESPACE, Config, config, {
      validate: validateSection,
      setSource: (current) => {
        source = current
      },
      // Thresholds are read through options() at decision time, so a change
      // needs no work here; only the interpreter / span dir are spawn-time.
      onChange: () => {
        if (bridge === null) return
        const next = options()
        if (next.python !== bridge.options.python || next.spanDir !== bridge.options.spanDir) stopBridge()
      },
    })
  })

  ctx.effect(() => stopBridge)

  const now = options()
  ctx.logger.info(`context-guardian scaffold loaded (compiler ${COMPILER_REV}; window ${now.numCtx}, hard ${now.compactThreshold}, idle ${now.idleCompactRatio}) -- compaction untouched in Phase 1`)

  // ── PHASE 3 lands here ────────────────────────────────────────────────────
  // 3.1  snapshot on ctx.on('session/event', ...) where event type is
  //      'compaction/start' (absorbing dsh-openwolf's compactionSurvival, 3d)
  // 3.2  compaction: subclass @deepseek-ai/dsh-compaction-basic and override
  //      summarize() with vendor/compiler.js compileRegion() -- 3b / 3c
  // 3b   recall + search tools via ctx.tools.register(defineTool(...)), the
  //      /recall command, toolArgTools / hideTools, recall + search budgets
  // 3c   context_rewrite_cost, idle trigger, tier ladder, keyword index,
  //      context_compact
}
