// Smoke checks for setup.mjs. Plain node, no child processes: main() is called
// in-process and its output is captured through the io.log parameter.
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as setup from '../setup.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const fixtures = join(here, 'fixtures')
const standardFixture = readFileSync(join(fixtures, 'standard.agent.cordis.yml'), 'utf8')
const installedFixture = readFileSync(join(fixtures, 'installed.agent.cordis.yml'), 'utf8')
const installedUrl = (() => {
  const lines = installedFixture.split('\n')
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === '- id: context-guardian') {
      const m = /^\s+name: (.+)$/.exec(lines[i + 1])
      if (m) {
        const raw = m[1].trim()
        const bare = raw.replace(/^['"]/, '').replace(/['"]$/, '')
        return bare.replace(/\?v=\d+$/, '')
      }
    }
  }
  return null
})()

const root = mkdtempSync(join(tmpdir(), 'cg-setup-'))
const q = '\'' // the single quote the row name is wrapped in
let passed = 0
let failed = 0

async function check (name, fn) {
  try {
    await fn()
    passed++
    console.log('  ok   ' + name)
  } catch (e) {
    failed++
    console.log('  FAIL ' + name + ' (' + (e && e.message ? e.message : String(e)) + ')')
  }
}

function assert (cond, msg) {
  if (!cond) throw new Error(msg)
}

function equal (actual, expected, msg) {
  if (actual !== expected) {
    throw new Error((msg || 'not equal') + ': ' + JSON.stringify(actual) + ' !== ' + JSON.stringify(expected))
  }
}

function isFile (p) {
  try {
    return statSync(p).isFile()
  } catch {
    return false
  }
}

function backupsIn (dir) {
  return readdirSync(dir).filter(n => n.indexOf('agent.cordis.yml.bak-context-guardian-') === 0)
}

function collector () {
  const lines = []
  return { lines, log: (s) => lines.push(String(s)), text: () => lines.join('\n') }
}

// -------------------------------------------------------------- fake worlds

let seq = 0

function makeEngine () {
  const dir = join(root, 'engine-' + (++seq))
  mkdirSync(dir, { recursive: true })
  const p = join(dir, 'engine.js')
  writeFileSync(p, '// fake engine for the smoke checks\nexport const rev = 1\n')
  return p
}

function makeHome (name, opts = {}) {
  const home = join(root, name)
  const dsh = join(home, '.dsh')
  mkdirSync(join(dsh, '.agent-presets'), { recursive: true })
  const ids = opts.presets === undefined ? ['luminari'] : opts.presets
  for (const id of ids) {
    const dir = join(dsh, '.agent-presets', id)
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, 'agent.cordis.yml'), opts.text === undefined ? standardFixture : opts.text)
  }
  if (opts.settingsDefault !== null) {
    writeFileSync(join(dsh, 'settings.yaml'),
      'agent-presets:\n  default: ' + (opts.settingsDefault === undefined ? (ids[0] || '') : opts.settingsDefault) + '\n')
  }
  for (const p of opts.profiles || []) {
    const dir = join(dsh, 'profiles', p.name)
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, 'package.json'), JSON.stringify(p.pkg, null, 2))
  }
  return { home, dsh }
}

function fakeStandard () {
  const dir = join(root, 'standard-' + (++seq))
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'agent.cordis.yml'), standardFixture)
  return dir
}

async function runMain (home, argv, env = {}) {
  const io = collector()
  const code = await setup.main(argv, { log: io.log, env, home })
  return { code, lines: io.lines, text: io.text() }
}

// ------------------------------------------------------------------ checks

await check('exports_exact', () => {
  for (const name of [
    'SETUP_REV', 'ROW_ID', 'resolveDshHome', 'engineUrl', 'defaultEnginePath',
    'readDefaultPreset', 'listUserPresets', 'listProfiles', 'findStandardPreset',
    'findCompactionGroup', 'planRow', 'main'
  ]) {
    assert(name in setup, 'missing export ' + name)
  }
  equal(setup.SETUP_REV, 'cg-setup-1', 'SETUP_REV')
  equal(setup.ROW_ID, 'context-guardian', 'ROW_ID')
  equal(typeof setup.main, 'function', 'main is a function')
  equal(typeof setup.planRow, 'function', 'planRow is a function')
})

await check('default_dsh_home_is_home_dot_dsh', () => {
  const H = join(root, 'homes', 'H')
  equal(setup.resolveDshHome({}, {}, H), join(H, '.dsh'), 'bare')
  equal(setup.resolveDshHome({}, { DSH_HOME: '/env/dsh' }, H), '/env/dsh', 'DSH_HOME wins')
  equal(setup.resolveDshHome({ 'dsh-home': '/flag/dsh' }, { DSH_HOME: '/env/dsh' }, H), '/flag/dsh', 'flag wins')
  equal(setup.resolveDshHome({}, {}, H).endsWith(join('.dsh')), true, 'no hardcoded separator')
})

await check('default_engine_is_beside_setup', () => {
  const p = setup.defaultEnginePath()
  assert(p.endsWith('engine.js'), 'not engine.js: ' + p)
  equal(dirname(p), resolve(join(here, '..')), 'dirname')
})

await check('engine_url_is_file_url', () => {
  const p = join(root, 'engine-1', 'engine.js')
  const url = setup.engineUrl(p)
  assert(url.startsWith('file:///'), 'not a file url: ' + url)
  assert(!url.includes('\\'), 'backslash in ' + url)
  assert(url.endsWith('/engine.js'), 'missing tail: ' + url)
})

await check('settings_default_parsed', () => {
  equal(setup.readDefaultPreset('agent-presets:\n  default: luminari\n'), 'luminari', 'nested')
  equal(setup.readDefaultPreset('other:\n  default: x\n'), null, 'other block')
  equal(setup.readDefaultPreset('agent-presets:\r\n  default: \'quo\'\r\n'), 'quo', 'crlf + quotes')
  equal(setup.readDefaultPreset('agent-presets:\n'), null, 'empty block')
  equal(setup.readDefaultPreset(''), null, 'empty text')
  equal(setup.readDefaultPreset('top:\n  a: 1\nagent-presets:\n  default: late\n'), 'late', 'second block')
})

await check('insert_into_standard_is_pure', () => {
  const url = 'file:///tmp/x/engine.js'
  const plan = setup.planRow(standardFixture, url, '2026-09-26')
  equal(plan.action, 'insert', 'action')
  assert(plan.newText.includes('# Context Guardian (added by `npm run setup` 2026-09-26)'), 'comment missing')
  const before = standardFixture.split('\n')
  const after = plan.newText.split('\n')
  assert(after.length > before.length, 'no growth')
  // pure insertion: every original line survives, in order, unchanged
  let at = -1
  for (let i = 0; i < before.length; i++) {
    let found = -1
    for (let j = at + 1; j < after.length; j++) {
      if (after[j] === before[i]) {
        found = j
        break
      }
    }
    assert(found !== -1, 'original line ' + i + ' gone: ' + JSON.stringify(before[i]))
    at = found
  }
  const group = setup.findCompactionGroup(after)
  assert(group, 'no compaction group after insert')
  const ids = group.rows.map(r => r.id)
  equal(ids[ids.length - 1], 'context-guardian', 'guardian is not the last row: ' + ids.join(','))
  assert(ids.indexOf('compaction-basic') !== -1, 'compaction-basic row lost')
  equal(after[after.indexOf('- id: compaction')], '- id: compaction', 'compaction head moved')
  // the delegation header comment block still runs straight into its top-level row
  const del = after.findIndex(l => l === '- id: delegation')
  assert(del !== -1, 'no delegation row')
  // the fixture header is '# <box rule> delegation ...'; keep the source ASCII
  const head = after.findIndex(l => l.startsWith('# \u2500\u2500 delegation'))
  assert(head !== -1 && head < del, 'delegation header missing')
  for (let i = head; i < del; i++) {
    const t = after[i].trim()
    assert(t === '' || t.startsWith('#'), 'the guardian row landed inside the delegation header at ' + i)
  }
})

await check('insert_keeps_crlf', () => {
  const crlf = standardFixture.replace(/\n/g, '\r\n')
  const plan = setup.planRow(crlf, 'file:///tmp/x/engine.js', '2026-09-26')
  equal(plan.action, 'insert', 'action')
  assert(plan.newText.includes('\r\n'), 'no CRLF')
  assert(!(/(^|[^\r])\n/.test(plan.newText)), 'bare LF survived')
  equal(plan.newText.split('\r\n').length - crlf.split('\r\n').length, 9, 'line growth')
  const group = setup.findCompactionGroup(plan.newText.split('\r\n'))
  equal(group.rows[group.rows.length - 1].id, 'context-guardian', 'last row')
})

await check('already_installed_is_none', () => {
  assert(installedUrl, 'could not read the installed fixture url')
  const plan = setup.planRow(installedFixture, installedUrl, '2026-09-26')
  equal(plan.action, 'none', 'action')
  equal(plan.reason, 'already installed', 'reason')
  equal(plan.newText, installedFixture, 'text changed')
})

await check('update_changes_only_name_line', () => {
  const url = 'file:///somewhere/else/engine.js'
  const plan = setup.planRow(installedFixture, url, '2026-09-26')
  equal(plan.action, 'update', 'action')
  const before = installedFixture.split('\n')
  const after = plan.newText.split('\n')
  equal(after.length, before.length, 'line count changed')
  const diff = []
  for (let i = 0; i < before.length; i++) if (before[i] !== after[i]) diff.push(i)
  equal(diff.length, 1, 'changed lines: ' + diff.join(','))
  equal(after[diff[0]], '      name: ' + q + url + q, 'new name line')
})

await check('no_compaction_group_fails', () => {
  const noGroup = standardFixture.split('\n').filter(l => l !== '- id: compaction').join('\n')
  const plan = setup.planRow(noGroup, 'file:///x/engine.js', '2026-09-26')
  equal(plan.action, 'none', 'action')
  equal(plan.reason, 'no top-level "- id: compaction" group in this preset', 'reason')
  equal(setup.findCompactionGroup(noGroup.split('\n')), null, 'group still found')
  const noBasic = standardFixture.replace(/^(\s+)- id: compaction-basic\s*$/m, '$1- id: compaction-renamed')
  const plan2 = setup.planRow(noBasic, 'file:///x/engine.js', '2026-09-26')
  equal(plan2.action, 'none', 'action 2')
  equal(plan2.reason, 'the compaction group has no compaction-basic row', 'reason 2')
  const { home } = makeHome('nogroup', { text: noGroup })
  const engine = makeEngine()
  return runMain(home, ['--engine', engine]).then(r => {
    equal(r.code, 1, 'main exit\n' + r.text)
    assert(r.text.includes('FAIL'), 'no FAIL line:\n' + r.text)
  })
})

await check('dry_run_writes_nothing', async () => {
  const { home, dsh } = makeHome('dry')
  const engine = makeEngine()
  const presetFile = join(dsh, '.agent-presets', 'luminari', 'agent.cordis.yml')
  const before = readFileSync(presetFile)
  const r = await runMain(home, ['--engine', engine])
  equal(r.code, 0, 'exit code\n' + r.text)
  assert(r.text.includes('dry run'), 'no dry run line:\n' + r.text)
  assert(r.text.includes('PLAN insert row in ' + presetFile), 'no PLAN line:\n' + r.text)
  const after = readFileSync(presetFile)
  assert(before.equals(after), 'the preset file changed')
  const strays = readdirSync(dirname(presetFile)).filter(n => n !== 'agent.cordis.yml')
  equal(strays.join(','), '', 'stray files')
  assert(r.text.includes('NEXT: '), 'no NEXT line:\n' + r.text)
})

await check('apply_inserts_and_backs_up', async () => {
  const { home, dsh } = makeHome('apply')
  const engine = makeEngine()
  const dir = join(dsh, '.agent-presets', 'luminari')
  const presetFile = join(dir, 'agent.cordis.yml')
  const before = readFileSync(presetFile, 'utf8')
  const url = setup.engineUrl(engine)
  const r1 = await runMain(home, ['--apply', '--engine', engine])
  equal(r1.code, 0, 'first exit\n' + r1.text)
  assert(r1.text.includes('DONE insert -- backup '), 'no DONE line:\n' + r1.text)
  const text = readFileSync(presetFile, 'utf8')
  assert(text.includes('- id: context-guardian'), 'row missing')
  assert(text.includes('name: ' + q + url + q), 'url missing: ' + url)
  const backups = readdirSync(dir).filter(n => n.indexOf('agent.cordis.yml.bak-context-guardian-') === 0)
  equal(backups.length, 1, 'backup count: ' + backups.join(','))
  assert(/^agent\.cordis\.yml\.bak-context-guardian-\d{14}$/.test(backups[0]), 'backup name: ' + backups[0])
  const backup = readFileSync(join(dir, backups[0]), 'utf8')
  equal(backup, before, 'backup content differs')
  const r2 = await runMain(home, ['--apply', '--engine', engine])
  equal(r2.code, 0, 'second exit\n' + r2.text)
  assert(r2.text.includes('already installed'), 'no already installed line:\n' + r2.text)
  const backups2 = readdirSync(dir).filter(n => n.indexOf('agent.cordis.yml.bak-context-guardian-') === 0)
  equal(backups2.length, 1, 'second backup written')
  const strays = readdirSync(dir).filter(n => n !== 'agent.cordis.yml' && n !== backups[0])
  equal(strays.join(','), '', 'stray files')
  // the applied file still parses and the guardian row is the last one
  const group = setup.findCompactionGroup(text.split('\n'))
  const ids = group.rows.map(r => r.id)
  equal(ids[ids.length - 1], 'context-guardian', 'last row: ' + ids.join(','))
})

await check('creates_guardian_preset_from_standard', async () => {
  const { home, dsh } = makeHome('create', { presets: [], settingsDefault: null })
  const engine = makeEngine()
  const std = fakeStandard()
  const dry = await runMain(home, ['--engine', engine, '--standard', std])
  equal(dry.code, 0, 'dry exit\n' + dry.text)
  assert(dry.text.includes('PLAN create preset guardian from ' + std), 'no create plan:\n' + dry.text)
  assert(!existsSync(join(dsh, '.agent-presets', 'guardian')), 'created during a dry run')
  const applied = await runMain(home, ['--apply', '--engine', engine, '--standard', std])
  equal(applied.code, 0, 'apply exit\n' + applied.text)
  const file = join(dsh, '.agent-presets', 'guardian', 'agent.cordis.yml')
  assert(isFile(file), 'no guardian preset file')
  const text = readFileSync(file, 'utf8')
  assert(text.includes('- id: context-guardian'), 'row missing in the new preset')
  const group = setup.findCompactionGroup(text.split('\n'))
  const ids = group.rows.map(r => r.id)
  equal(ids[ids.length - 1], 'context-guardian', 'last row: ' + ids.join(','))
  const meta = readFileSync(join(dsh, '.agent-presets', 'guardian', 'preset.yml'), 'utf8')
  assert(meta.includes('name: Standard + Context Guardian'), 'preset.yml name')
  assert(meta.includes('description: The shipped standard preset with the context-guardian compaction row.'), 'preset.yml description')
  assert(meta.includes('order: 50'), 'preset.yml order')
  equal(setup.listUserPresets(dsh).join(','), 'guardian', 'listed presets')
  // findStandardPreset also finds it from the flag
  equal(setup.findStandardPreset({ standard: std }, {}), std, 'findStandardPreset by flag')
  equal(setup.findStandardPreset({}, { PATH: '' }), null, 'no PATH hit')
})

await check('finds_standard_on_path', () => {
  // A PATH entry holding a `dsh` binary, with the shipped preset two levels up.
  const base = join(root, 'pathwalk')
  const bin = join(base, 'bin')
  mkdirSync(bin, { recursive: true })
  writeFileSync(join(bin, 'dsh'), '#!/bin/sh\n')
  const shipped = join(base, 'node_modules', '@deepseek-ai', 'dsh-agent-presets', 'presets', 'standard')
  mkdirSync(shipped, { recursive: true })
  writeFileSync(join(shipped, 'agent.cordis.yml'), standardFixture)
  const bogus = join(root, 'bogus-path')
  mkdirSync(bogus, { recursive: true })
  const delim = process.platform === 'win32' ? ';' : ':'
  equal(setup.findStandardPreset({}, { PATH: [bogus, bin].join(delim) }), shipped, 'walk up from the bin dir')
  // too deep to reach: the walk stops after 8 levels
  const deepBin = join(base, 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'bin')
  mkdirSync(deepBin, { recursive: true })
  writeFileSync(join(deepBin, 'dsh'), '#!/bin/sh\n')
  equal(setup.findStandardPreset({}, { PATH: deepBin }), null, 'walked too far')
  // a dir that merely holds a node_modules tree is not enough without a dsh file
  equal(setup.findStandardPreset({}, { PATH: base }), null, 'no dsh file in the PATH dir')
  // an explicit --standard that does not exist falls through to the PATH
  equal(setup.findStandardPreset({ standard: join(root, 'nope') }, { PATH: [bogus, bin].join(delim) }),
    shipped, 'bad flag falls through')
  // the bin file is realpath-ed before the walk starts
  const link = join(root, 'linked-bin')
  try {
    symlinkSync(bin, link, 'dir')
  } catch {
    return // symlinks unavailable on this host
  }
  equal(setup.findStandardPreset({}, { PATH: link }), shipped, 'symlinked bin dir')
})

await check('no_standard_exits_2', async () => {
  const { home } = makeHome('nostd', { presets: [], settingsDefault: null })
  const engine = makeEngine()
  const emptyPath = join(root, 'empty-path')
  mkdirSync(emptyPath, { recursive: true })
  const r = await runMain(home, ['--engine', engine], { PATH: emptyPath })
  equal(r.code, 2, 'exit code\n' + r.text)
  assert(r.text.includes('FAIL'), 'no FAIL line:\n' + r.text)
  assert(r.text.includes('.agent-presets'), 'no manual hint:\n' + r.text)
  // a single user preset needs no standard preset at all
  const single = makeHome('single', { presets: ['only'] })
  const r2 = await runMain(single.home, ['--engine', engine])
  equal(r2.code, 0, 'single preset exit\n' + r2.text)
  assert(r2.text.includes('PLAN insert row in'), 'no plan for the single preset:\n' + r2.text)
})

await check('json_output', async () => {
  const { home } = makeHome('json')
  const engine = makeEngine()
  const r = await runMain(home, ['--engine', engine, '--json'])
  equal(r.code, 0, 'exit code\n' + r.text)
  equal(r.lines.length, 1, 'expected exactly one line, got ' + r.lines.length)
  const obj = JSON.parse(r.lines[0])
  equal(obj.setup, setup.SETUP_REV, 'setup rev')
  equal(obj.action, 'insert', 'action')
  equal(obj.applied, false, 'applied')
  equal(obj.preset, 'luminari', 'preset')
  equal(obj.presetFile.endsWith(join('.agent-presets', 'luminari', 'agent.cordis.yml')), true, 'presetFile')
  equal(obj.dshHome.endsWith(join('.dsh')), true, 'dshHome')
  equal(obj.engine, engine, 'engine')
  equal(obj.engineUrl, setup.engineUrl(engine), 'engineUrl')
  equal(obj.backup, null, 'backup')
  equal(obj.created, null, 'created')
  assert(Array.isArray(obj.next) && obj.next.length >= 2, 'next steps')
  assert(Array.isArray(obj.profiles), 'profiles')
  equal(obj.errors.length, 0, 'errors')
})

await check('profiles_detected', async () => {
  const { home, dsh } = makeHome('profiles', {
    presets: ['a', 'b', 'c'],
    settingsDefault: 'a',
    profiles: [
      { name: 'web', pkg: { name: 'web', dependencies: { 'dsh-context-guardian': '0.1.0' } } },
      { name: 'api', pkg: { name: 'api', dependencies: { lodash: '1.0.0' } } },
      { name: 'cli', pkg: { name: 'cli', dsh: { profile: { bundles: ['dsh-context-guardian'] } } } }
    ]
  })
  mkdirSync(join(dsh, 'profiles', 'nomanifest'), { recursive: true })
  const profiles = setup.listProfiles(dsh)
  equal(profiles.length, 3, 'profile count: ' + JSON.stringify(profiles))
  const byName = {}
  for (const p of profiles) byName[p.name] = p.hasGuardian
  equal(byName.web, true, 'web (dependencies)')
  equal(byName.api, false, 'api (no dependency)')
  equal(byName.cli, true, 'cli (dsh.profile.bundles)')
  equal(setup.listUserPresets(dsh).join(','), 'a,b,c', 'sorted presets')
  const engine = makeEngine()
  const r = await runMain(home, ['--engine', engine, '--preset', 'b'])
  equal(r.code, 0, 'exit\n' + r.text)
  assert(/profile web has dsh-context-guardian/.test(r.text), 'no profile line:\n' + r.text)
  assert(r.text.includes('profile api no dsh-context-guardian'), 'no negative profile line:\n' + r.text)
  // the first profile with the guardian, in sorted order, is cli
  assert(r.text.includes('dsh web --profile cli'), 'next step does not name the guardian profile:\n' + r.text)
  const r2 = await runMain(home, ['--engine', engine, '--profile', 'api', '--preset', 'b'])
  assert(r2.text.includes('dsh web --profile api'), '--profile ignored:\n' + r2.text)
  const r3 = await runMain(home, ['--engine', engine, '--preset', 'nope'])
  equal(r3.code, 1, 'unknown preset exit\n' + r3.text)
  assert(r3.text.includes('FAIL'), 'no FAIL line for an unknown preset')
})

await check('no_profile_warns', async () => {
  const { home } = makeHome('warn')
  const engine = makeEngine()
  const r = await runMain(home, ['--engine', engine])
  equal(r.code, 0, 'exit\n' + r.text)
  assert(r.text.includes('WARN no profile lists dsh-context-guardian'), 'no WARN line:\n' + r.text)
  assert(r.text.includes('dsh web --profile web'), 'default profile name:\n' + r.text)
  const home2 = makeHome('warn2', { presets: ['a', 'b'], settingsDefault: null })
  const r2 = await runMain(home2.home, ['--engine', engine, '--standard', fakeStandard()])
  equal(r2.code, 0, 'exit\n' + r2.text)
  assert(r2.text.includes('(optional) make it the default: set "agent-presets: default: guardian"'),
    'no optional default line:\n' + r2.text)
  const home3 = makeHome('warn3', { presets: ['a'], settingsDefault: 'a' })
  const r3 = await runMain(home3.home, ['--engine', engine, '--preset', 'a'])
  assert(!r3.text.includes('(optional) make it the default'), 'optional line for the settings default:\n' + r3.text)
})

await check('missing_engine_fails', async () => {
  const { home } = makeHome('noengine')
  const r = await runMain(home, ['--engine', join(root, 'nowhere', 'engine.js')])
  equal(r.code, 1, 'exit\n' + r.text)
  assert(r.text.includes('FAIL no engine file at '), 'no FAIL line:\n' + r.text)
})

await check('missing_dsh_home_fails', async () => {
  const home = join(root, 'nohome')
  mkdirSync(home, { recursive: true })
  const engine = makeEngine()
  const missing = join(home, 'missing')
  const r = await runMain(home, ['--engine', engine, '--dsh-home', missing])
  equal(r.code, 1, 'exit\n' + r.text)
  assert(r.text.includes('FAIL no DSH home at ' + missing), 'FAIL line:\n' + r.text)
  assert(r.text.includes('npx @deepseek-ai/dsh web'), 'fix line:\n' + r.text)
  const r2 = await runMain(home, ['--engine', engine], { DSH_HOME: missing })
  equal(r2.code, 1, 'env exit\n' + r2.text)
})

await check('already_installed_fixture_through_main', async () => {
  const engine = makeEngine()
  const url = setup.engineUrl(engine)
  // the installed fixture, with its row repointed at the fake engine and the
  // ?v=5 cache-buster left in place
  const text = installedFixture.replace(/(name: ')(file:\/\/\/[^']*)(')/, (m, a, b, c) => a + url + '?v=5' + c)
  const { home, dsh } = makeHome('installed', { text })
  const file = join(dsh, '.agent-presets', 'luminari', 'agent.cordis.yml')
  const r = await runMain(home, ['--engine', engine])
  equal(r.code, 0, 'exit\n' + r.text)
  assert(r.text.includes('already installed'), 'not detected:\n' + r.text)
  equal(readFileSync(file, 'utf8'), text, 'the file changed')
  equal(readdirSync(dirname(file)).join(','), 'agent.cordis.yml', 'no writes')
  // a different url is an update: one name line, backed up
  const r2 = await runMain(home, ['--apply', '--engine', makeEngine()])
  equal(r2.code, 0, 'update exit\n' + r2.text)
  assert(r2.text.includes('DONE update -- backup '), 'no DONE update line:\n' + r2.text)
  const after = readFileSync(file, 'utf8').split('\n')
  const before = text.split('\n')
  equal(after.length, before.length, 'line count changed')
  const diff = before.map((l, i) => (l === after[i] ? -1 : i)).filter(i => i !== -1)
  equal(diff.length, 1, 'changed lines: ' + diff.join(','))
  equal(backupsIn(dirname(file)).length, 1, 'backup count')
})

await check('apply_keeps_crlf_end_to_end', async () => {
  const crlf = standardFixture.replace(/\n/g, '\r\n')
  const { home, dsh } = makeHome('crlf', { text: crlf })
  const engine = makeEngine()
  const file = join(dsh, '.agent-presets', 'luminari', 'agent.cordis.yml')
  const r = await runMain(home, ['--apply', '--engine', engine])
  equal(r.code, 0, 'exit\n' + r.text)
  assert(r.text.includes('DONE insert'), 'no DONE line:\n' + r.text)
  const after = readFileSync(file, 'utf8')
  assert(!(/(^|[^\r])\n/.test(after)), 'bare LF in the written file')
  const lines = after.split('\r\n')
  const group = setup.findCompactionGroup(lines)
  const ids = group.rows.map(r2 => r2.id)
  equal(ids[ids.length - 1], 'context-guardian', 'last row: ' + ids.join(','))
  equal(lines.length - crlf.split('\r\n').length, 9, 'line growth')
  const backup = backupsIn(dirname(file))[0]
  assert(readFileSync(join(dirname(file), backup), 'utf8') === crlf, 'backup is not the original CRLF text')
})

console.log('setup_smoke: ' + (passed + failed) + ' checks, ' + passed + ' passed, ' + failed + ' failed')
rmSync(root, { recursive: true, force: true })
process.exit(failed === 0 && passed + failed >= 16 ? 0 : 1)
