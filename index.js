/**
 * dsh-context-guardian -- Context Guardian as a native DSH bundle plugin.
 *
 * This is the SETTINGS HALF of the bundle, loaded in the DSH profile plane:
 * config schema, the `context-guardian` settings section, the update notice
 * and the optional Python bridge. It does not compact anything and registers
 * no listener, provider or tool.
 *
 * Compaction, recall, search, memory and hand-off live in engine.js, which
 * DSH mounts as ONE row in the agent preset's `compaction` group -- a plane
 * this file cannot see. `npm run setup` (setup.mjs) writes that row; see
 * docs/dsh-integration.md. Without it @deepseek-ai/dsh-compaction-basic
 * behaves exactly as the profile has it.
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
 * @deepseek-ai/dsh-compaction and @deepseek-ai/dsh-llm, so this file never
 * loads it.
 *
 * MIT licensed.
 */
import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import Schema from '@deepseek-ai/schemastery'
import { checkForUpdate, defaultCacheFile } from './update_check.js'
import { COMPILER_REV, DEFAULT_ARG_TOOLS } from './vendor/compiler.js'

export const name = 'dsh-context-guardian'

/** Nothing is required at load; settings attaches when present. */
export const inject = []

/** Settings namespace (must match /^[a-z][a-z0-9-]*$/). */
export const SETTINGS_NAMESPACE = 'context-guardian'

/** Resolved from the module URL -- never from process.cwd(). */
const PACKAGE_ROOT = dirname(fileURLToPath(import.meta.url))
const BRIDGE_SCRIPT = join(PACKAGE_ROOT, 'modules', 'cg_bridge.py')

/** This plugin's own version, from the package.json beside this file. */
const PKG_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version

const ratio = () => Schema.number().min(0.05).max(0.99)

export const Config = Schema.object({
  python: Schema.string().default('')
    .description('Python for the optional bridge. Empty = $GUARDIAN_PYTHON, then a .venv beside this package, then python/python3 on PATH.'),
  numCtx: Schema.natural().default(32768)
    .description('Model context window the ratios are measured against (GUARDIAN_NUM_CTX overrides).'),
  compactThreshold: ratio().default(0.85)
    .description('Hard compaction trigger (GUARDIAN_COMPACT_THRESHOLD overrides).'),
  // 0 = off (the default since alpha.8); ratio() demands >= 0.05, so 0 needs its own bound (2026-10-03: a 0 default made the plugin unloadable)
  idleCompactRatio: Schema.number().min(0).max(0.99).default(0)
    .description('Proactive trigger when the agent goes idle; 0 = off, the default since 0.1.0-alpha.8 (GUARDIAN_IDLE_COMPACT_RATIO overrides).'),
  reserveOutput: Schema.natural().default(8192)
    .description('Tokens held back for the reply (GUARDIAN_RESERVE_OUTPUT overrides).'),
  spanDir: Schema.string().default('')
    .description('Span archive. Empty = $GUARDIAN_SPAN_DIR, then the proxy\'s own default.'),
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
    reserveOutput: int('GUARDIAN_RESERVE_OUTPUT', section.reserveOutput),
    spanDir: env.GUARDIAN_SPAN_DIR?.trim() || section.spanDir || '',
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

  /** Current options; read through this, never a copy. */
  const options = () => resolveOptions(source())

  /** Spawned on first use only -- nothing in this file calls it on its own. */
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
  ctx.logger.info(`context-guardian settings loaded (compiler ${COMPILER_REV}; window ${now.numCtx}, hard ${now.compactThreshold}, idle ${now.idleCompactRatio}) -- compaction is the engine row in the agent preset (npm run setup), not this plugin`)

  // Fire-and-forget: the update notice is a courtesy line, never a reason to wait.
  void checkForUpdate({
    pkg: name,
    current: PKG_VERSION,
    cacheFile: defaultCacheFile(name),
    log: (line) => ctx.logger.info(line),
  }).catch(() => {})
}
