// Smoke check for the DSH preset example: install the row into a THROWAWAY DSH home and check what the engine sees.
// usage (from a clone): node examples/dsh-preset/smoke.mjs
// Touches nothing outside a temp folder. No DSH install needed; uses the `standard` preset fixture in tests/fixtures.
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = join(here, '..', '..')
const setup = await import(new URL('../../setup.mjs', import.meta.url))
const engine = await import(new URL('../../engine.js', import.meta.url))
const res = []
const check = (name, ok, why = '') => { res.push(!!ok); console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok || !why ? '' : '  -- ' + why)) }

const home = mkdtempSync(join(tmpdir(), 'cg-dsh-example-'))
try {
  mkdirSync(join(home, '.dsh', '.agent-presets'), { recursive: true })
  const standard = join(home, 'standard')
  mkdirSync(standard)
  cpSync(join(repo, 'tests', 'fixtures', 'standard.agent.cordis.yml'), join(standard, 'agent.cordis.yml'))
  const lines = []
  const code = await setup.main(['--standard', standard, '--apply'], { log: (s) => lines.push(String(s)), env: {}, home })
  check('setup_apply_exit_0', code === 0, lines.join(' | '))
  const preset = readFileSync(join(home, '.dsh', '.agent-presets', 'guardian', 'agent.cordis.yml'), 'utf8')
  const ids = setup.findCompactionGroup(preset.split(/\r?\n/)).rows.map((r) => r.id)
  check('row_is_last_in_compaction_group', ids[ids.length - 1] === 'context-guardian', ids.join(','))
  const url = (preset.match(/- id: context-guardian\s+name: '([^']+)'/) || [])[1] || ''
  check('row_points_at_this_engine', url.startsWith('file:///') && fileURLToPath(url.split('?')[0]) === join(repo, 'engine.js'), url)
  // the example row's config, read the same way the row is written
  const row = readFileSync(join(here, 'compaction-row.yml'), 'utf8')
  const config = { mode: (row.match(/^\s+mode:\s*(\S+)/m) || [])[1], tools: ((row.match(/^\s+tools:\s*\[([^\]]*)\]/m) || [])[1] || '').split(',').map((s) => s.trim()) }
  const opts = engine.resolveEngineOptions(config, {})
  // the row's own values must be what the engine resolves (an unknown mode or tool would silently fall back)
  check('engine_reads_example_row', opts.mode === config.mode && opts.tools.join(',') === config.tools.join(',') && config.tools.length === 2,
    JSON.stringify({ row: config, engine: { mode: opts.mode, tools: opts.tools } }))
  check('documented_defaults_hold', opts.staleRecall === true && opts.postAnswerCheck === false && opts.idleCompactRatio === 0 && opts.numCtxExplicit === false,
    JSON.stringify({ staleRecall: opts.staleRecall, postAnswerCheck: opts.postAnswerCheck, idleCompactRatio: opts.idleCompactRatio }))
} finally {
  rmSync(home, { recursive: true, force: true })
}
const passed = res.filter(Boolean).length
console.log(`dsh_preset_smoke: ${res.length} checks, ${passed} passed, ${res.length - passed} failed`)
process.exit(res.length && passed === res.length ? 0 : 1)
