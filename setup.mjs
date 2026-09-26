// Context Guardian setup: mount the engine as ONE row in a DSH agent preset.
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync
} from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const SETUP_REV = 'cg-setup-1'
export const ROW_ID = 'context-guardian'

const GUARDIAN_PKG = 'dsh-context-guardian'
const STANDARD_REL_PATHS = [
  join('node_modules', '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai', 'dsh-agent-presets', 'presets', 'standard'),
  join('node_modules', '@deepseek-ai', 'dsh-agent-presets', 'presets', 'standard'),
  join('lib', 'node_modules', '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai', 'dsh-agent-presets', 'presets', 'standard'),
  join('packages', 'preset', 'agent-presets', 'presets', 'standard')
]
const PRESET_REL = join('.agent-presets', '')
const GROUP_HEAD = /^- id: compaction\s*$/
const TOP_HEAD = /^- id: /
const ROW_LINE = /^(\s+)- id: (\S+)\s*$/
const NAME_LINE = /^\s+name: (.+)$/

// ---------------------------------------------------------------- utilities

function isDir (p) {
  try {
    return statSync(p).isDirectory()
  } catch {
    return false
  }
}

function isFile (p) {
  try {
    return statSync(p).isFile()
  } catch {
    return false
  }
}

function isPresetDir (p) {
  return isDir(p) && isFile(join(p, 'agent.cordis.yml'))
}

function readText (p) {
  return readFileSync(p, 'utf8')
}

function unquote (value) {
  const s = String(value).trim()
  if (s.length >= 2) {
    const a = s[0]
    const b = s[s.length - 1]
    if ((a === "'" && b === "'") || (a === '"' && b === '"')) return s.slice(1, -1)
  }
  return s
}

function stripVersionQuery (name) {
  return String(name).replace(/\?v=\d+\s*$/, '').trim()
}

function pad2 (n) {
  return n < 10 ? '0' + n : String(n)
}

function dateStamp (d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
}

function timeStamp (d) {
  return String(d.getFullYear()) +
    pad2(d.getMonth() + 1) +
    pad2(d.getDate()) +
    pad2(d.getHours()) +
    pad2(d.getMinutes()) +
    pad2(d.getSeconds())
}

function indentOf (line) {
  const m = /^\s*/.exec(line)
  return m ? m[0] : ''
}

function quote (s) {
  return "'" + s + "'"
}

// ------------------------------------------------------------------ exports

export function resolveDshHome (flags, env = process.env, home = homedir()) {
  const fromFlag = flags && flags['dsh-home']
  if (fromFlag) return String(fromFlag)
  const fromEnv = env && env.DSH_HOME
  if (fromEnv) return String(fromEnv)
  return join(home, '.dsh')
}

export function engineUrl (enginePath) {
  return pathToFileURL(resolve(enginePath)).href
}

export function defaultEnginePath () {
  return join(dirname(fileURLToPath(import.meta.url)), 'engine.js')
}

export function readDefaultPreset (settingsText) {
  if (typeof settingsText !== 'string') return null
  const lines = settingsText.split(/\r?\n/)
  let inside = false
  for (const line of lines) {
    if (!inside) {
      if (/^agent-presets:\s*(#.*)?$/.test(line)) inside = true
      continue
    }
    if (line.trim() === '' || /^\s*#/.test(line)) continue
    if (!/^\s/.test(line)) return null
    const m = /^\s+default:\s*(.+?)\s*$/.exec(line)
    if (m) return unquote(m[1])
  }
  return null
}

export function listUserPresets (dshHome) {
  const root = join(dshHome, PRESET_REL)
  let entries
  try {
    entries = readdirSync(root, { withFileTypes: true })
  } catch {
    return []
  }
  const ids = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    if (isPresetDir(join(root, entry.name))) ids.push(entry.name)
  }
  ids.sort()
  return ids
}

function bundlesOf (pkg) {
  if (!pkg || typeof pkg !== 'object') return []
  const dsh = pkg.dsh
  if (dsh && typeof dsh === 'object') {
    const profile = dsh.profile
    if (profile && typeof profile === 'object' && Array.isArray(profile.bundles)) return profile.bundles
    if (Array.isArray(dsh.bundles)) return dsh.bundles
  }
  return []
}

export function listProfiles (dshHome) {
  const root = join(dshHome, 'profiles')
  let entries
  try {
    entries = readdirSync(root, { withFileTypes: true })
  } catch {
    return []
  }
  const out = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const manifest = join(root, entry.name, 'package.json')
    if (!isFile(manifest)) continue
    let pkg = null
    try {
      pkg = JSON.parse(readText(manifest))
    } catch {
      pkg = null
    }
    const deps = pkg && pkg.dependencies && typeof pkg.dependencies === 'object' ? pkg.dependencies : {}
    const bundled = bundlesOf(pkg).some(b => typeof b === 'string' && b.split('@')[0] === GUARDIAN_PKG)
    const hasGuardian = Object.prototype.hasOwnProperty.call(deps, GUARDIAN_PKG) || bundled
    out.push({ name: entry.name, hasGuardian })
  }
  out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  return out
}

export function findStandardPreset (flags, env = process.env) {
  const seen = []
  const tryDir = (dir) => {
    if (isPresetDir(dir)) return dir
    return null
  }
  const fromFlag = flags && flags.standard
  if (fromFlag) {
    const hit = tryDir(resolve(String(fromFlag)))
    if (hit) return hit
  }
  const pathEnv = (env && env.PATH) || ''
  for (const part of pathEnv.split(delimiter)) {
    if (!part) continue
    const dir = resolve(part)
    const binNames = ['dsh', 'dsh.cmd', 'dsh.ps1']
    let binPath = null
    for (const name of binNames) {
      const candidate = join(dir, name)
      if (isFile(candidate)) {
        binPath = candidate
        break
      }
    }
    if (!binPath) continue
    const starts = [dir]
    try {
      starts.push(dirname(realpathSync(binPath)))
    } catch {
      // a broken symlink is simply not a hint
    }
    for (const from of starts) {
      let level = from
      for (let up = 0; up <= 8; up++) {
        for (const rel of STANDARD_REL_PATHS) {
          const dir2 = join(level, rel)
          if (seen.indexOf(dir2) !== -1) continue
          seen.push(dir2)
          const hit = tryDir(dir2)
          if (hit) return hit
        }
        const parent = dirname(level)
        if (parent === level) break
        level = parent
      }
    }
  }
  return null
}

export function findCompactionGroup (lines) {
  if (!Array.isArray(lines)) return null
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (GROUP_HEAD.test(lines[i])) {
      start = i
      break
    }
  }
  if (start === -1) return null
  let end = lines.length
  for (let i = start + 1; i < lines.length; i++) {
    if (TOP_HEAD.test(lines[i])) {
      end = i
      break
    }
  }
  const rows = []
  let rowIndent = null
  for (let i = start + 1; i < end; i++) {
    const m = ROW_LINE.exec(lines[i])
    if (!m) continue
    if (rowIndent === null) rowIndent = m[1]
    let nameLine = -1
    let name = null
    for (let j = i + 1; j < end; j++) {
      if (ROW_LINE.test(lines[j])) break
      const nm = NAME_LINE.exec(lines[j])
      if (nm) {
        nameLine = j
        name = unquote(nm[1])
        break
      }
    }
    rows.push({ id: m[2], line: i, nameLine, name })
  }
  return { start, end, rowIndent, rows }
}

function rowBlock (rowIndent, url, today) {
  const i = rowIndent
  return [
    '',
    i + '# Context Guardian (added by `npm run setup` ' + today + '): deterministic checkpoints when the LLM summary fails,',
    i + '# recall/search, /guardian status, persistent memory. Remove this row to switch it off.',
    i + '- id: ' + ROW_ID,
    i + '  name: ' + quote(url),
    i + '  config:',
    i + '    mode: llm-then-deterministic',
    i + '    idleCompactRatio: 0.45',
    i + '    tools: [recall, search]'
  ]
}

export function planRow (text, url, today) {
  const source = String(text)
  const eol = source.indexOf('\r\n') !== -1 ? '\r\n' : '\n'
  const lines = source.split(/\r?\n/)
  const group = findCompactionGroup(lines)
  if (!group) {
    return {
      action: 'none',
      newText: source,
      rowText: '',
      reason: 'no top-level "- id: compaction" group in this preset'
    }
  }
  if (!group.rows.some(r => r.id === 'compaction-basic')) {
    return {
      action: 'none',
      newText: source,
      rowText: '',
      reason: 'the compaction group has no compaction-basic row'
    }
  }
  const existing = group.rows.find(r => r.id === ROW_ID)
  if (existing) {
    if (existing.nameLine === -1) {
      return {
        action: 'none',
        newText: source,
        rowText: '',
        reason: 'the ' + ROW_ID + ' row has no name line'
      }
    }
    const wanted = quote(url)
    const wantedName = indentOf(lines[existing.nameLine]) + 'name: ' + wanted
    if (stripVersionQuery(existing.name) === stripVersionQuery(url)) {
      return { action: 'none', newText: source, rowText: '', reason: 'already installed' }
    }
    const next = lines.slice()
    next[existing.nameLine] = wantedName
    return {
      action: 'update',
      newText: next.join(eol),
      rowText: wantedName,
      reason: 'the ' + ROW_ID + ' row points at a different engine'
    }
  }
  let at = group.end
  while (at > group.start + 1) {
    const trimmed = lines[at - 1].trim()
    if (trimmed === '' || trimmed.startsWith('#')) at--
    else break
  }
  const block = rowBlock(group.rowIndent === null ? '    ' : group.rowIndent, url, today)
  const next = lines.slice(0, at).concat(block, lines.slice(at))
  return {
    action: 'insert',
    newText: next.join(eol),
    rowText: block.join(eol),
    reason: 'the compaction group has no ' + ROW_ID + ' row'
  }
}

// --------------------------------------------------------------------- main

function parseFlags (argv) {
  const flags = {}
  const list = Array.isArray(argv) ? argv : []
  for (let i = 0; i < list.length; i++) {
    const arg = String(list[i])
    if (arg === '--apply') {
      flags.apply = true
      continue
    }
    if (arg === '--json') {
      flags.json = true
      continue
    }
    if (!arg.startsWith('--')) continue
    const eq = arg.indexOf('=')
    if (eq === -1) {
      const name = arg.slice(2)
      const value = i + 1 < list.length && !String(list[i + 1]).startsWith('--') ? String(list[++i]) : ''
      flags[name] = value
    } else {
      flags[arg.slice(2, eq)] = arg.slice(eq + 1)
    }
  }
  return flags
}

function readSettingsDefault (home) {
  const settings = join(home, 'settings.yaml')
  if (!isFile(settings)) return null
  try {
    return readDefaultPreset(readText(settings))
  } catch {
    return null
  }
}

function verifyWritten (file, plan, url) {
  const reread = readText(file)
  const eol = reread.indexOf('\r\n') !== -1 ? '\r\n' : '\n'
  const lines = reread.split(/\r?\n/)
  const expected = plan.newText.split(/\r?\n/).length
  if (lines.length !== expected) {
    return 'line count is ' + lines.length + ', expected ' + expected
  }
  const group = findCompactionGroup(lines)
  if (!group) return 'the compaction group is gone after the write'
  const row = group.rows.find(r => r.id === ROW_ID)
  if (!row) return 'the ' + ROW_ID + ' row is missing after the write'
  if (stripVersionQuery(row.name) !== url) {
    return 'the ' + ROW_ID + ' row reads ' + JSON.stringify(row.name) + ' after the write'
  }
  if (eol === '\r\n' && /(^|[^\r])\n/.test(reread)) {
    return 'the rewritten file mixes CRLF and bare LF'
  }
  return null
}

export async function main (argv, io = { log: console.log, env: process.env, home: homedir() }) {
  const flags = parseFlags(argv)
  const env = io.env || {}
  const home = io.home || homedir()
  const lines = []
  const result = {
    setup: SETUP_REV,
    dshHome: null,
    profiles: [],
    engine: null,
    engineUrl: null,
    preset: null,
    presetFile: null,
    action: null,
    applied: false,
    backup: null,
    created: null,
    next: [],
    errors: []
  }
  const now = new Date()
  const today = dateStamp(now)
  const stamp = timeStamp(now)

  const say = (s) => lines.push(s)
  const fail = (s) => {
    result.errors.push(s)
    say('FAIL ' + s)
  }
  const done = (code) => {
    if (flags.json) io.log(JSON.stringify(result))
    else for (const line of lines) io.log(line)
    return code
  }

  // 1. DSH home
  const dshHome = resolveDshHome(flags, env, home)
  result.dshHome = dshHome
  if (!isDir(dshHome)) {
    fail('no DSH home at ' + dshHome)
    say('  fix: install DSH (npx @deepseek-ai/dsh web) or pass --dsh-home / set DSH_HOME')
    return done(1)
  }
  say('OK dsh home ' + dshHome)

  // 2. Profiles
  const profiles = listProfiles(dshHome)
  result.profiles = profiles
  for (const profile of profiles) {
    say('  profile ' + profile.name + ' ' +
      (profile.hasGuardian ? 'has ' + GUARDIAN_PKG : 'no ' + GUARDIAN_PKG))
  }
  const withGuardian = profiles.find(p => p.hasGuardian)
  if (!withGuardian) {
    say('WARN no profile lists ' + GUARDIAN_PKG)
    say('  fix: dsh plugin --profile <name> add ' + GUARDIAN_PKG +
      ' (the engine row below works without it; the profile half adds settings)')
  }
  const profileName = flags.profile || (withGuardian ? withGuardian.name : 'web')

  // 3. Engine
  const engine = flags.engine ? resolve(String(flags.engine)) : defaultEnginePath()
  const url = engineUrl(engine)
  result.engine = engine
  result.engineUrl = url
  if (!isFile(engine)) {
    fail('no engine file at ' + engine)
    say('  fix: pass --engine <path/to/engine.js>')
    return done(1)
  }
  say('OK engine ' + engine + ' -> ' + url)

  // 4. Preset choice
  const userPresets = listUserPresets(dshHome)
  const settingsDefault = readSettingsDefault(dshHome)
  let preset = null
  let created = false
  let standardDir = null
  if (flags.preset) {
    preset = String(flags.preset)
    if (userPresets.indexOf(preset) === -1) {
      fail('no user preset "' + preset + '" in ' + join(dshHome, PRESET_REL))
      say('  fix: create ' + join(dshHome, PRESET_REL, preset, 'agent.cordis.yml') +
        ' or drop --preset to pick one automatically')
      return done(1)
    }
    say('OK preset ' + preset + ' (--preset)')
  } else if (settingsDefault && userPresets.indexOf(settingsDefault) !== -1) {
    preset = settingsDefault
    say('OK preset ' + preset + ' (settings default)')
  } else if (userPresets.length === 1) {
    preset = userPresets[0]
    say('OK preset ' + preset + ' (the only one)')
  } else {
    const standard = findStandardPreset(flags, env)
    standardDir = standard
    if (!standard) {
      fail('no user preset to edit and no shipped standard preset found')
      say('  fix: pass --standard <dir> holding agent.cordis.yml, or copy the shipped standard preset folder to ' +
        join(home, '.agent-presets', 'guardian') + '/ and re-run')
      say('  manual: ' + join(home, '.agent-presets', 'guardian', 'agent.cordis.yml') +
        ' (the preset folder, not the file)')
      return done(2)
    }
    preset = 'guardian'
    created = true
    result.created = preset
    if (!flags.apply) {
      say('PLAN create preset guardian from ' + standard)
    } else {
      const dir = join(dshHome, PRESET_REL, preset)
      try {
        mkdirSync(dir, { recursive: true })
        copyFileSync(join(standard, 'agent.cordis.yml'), join(dir, 'agent.cordis.yml'))
        writeFileSync(join(dir, 'preset.yml'), [
          'name: Standard + Context Guardian',
          'description: The shipped standard preset with the context-guardian compaction row.',
          'order: 50',
          ''
        ].join('\n'))
        say('OK created preset ' + preset + ' in ' + dir)
      } catch (e) {
        fail('could not create preset ' + preset + ': ' + e.message)
        return done(1)
      }
    }
  }
  result.preset = preset
  const presetFile = join(dshHome, PRESET_REL, preset, 'agent.cordis.yml')
  result.presetFile = presetFile
  // during a dry run of the create path the destination does not exist yet, so
  // the plan is made against the standard preset it would be copied from
  let sourceFile = presetFile
  if (!isFile(presetFile)) {
    if (created && !flags.apply && standardDir) {
      sourceFile = join(standardDir, 'agent.cordis.yml')
      say('PLAN preset file ' + presetFile + ' (copied from ' + standardDir + ')')
    } else {
      fail('no agent.cordis.yml at ' + presetFile)
      return done(1)
    }
  } else {
    say('OK preset file ' + presetFile)
  }

  // 5. Plan
  const text = readText(sourceFile)
  const plan = planRow(text, url, today)
  result.action = plan.action
  if (plan.action === 'none') {
    if (plan.reason === 'already installed') {
      say('OK ' + plan.reason + ' (' + ROW_ID + ' -> ' + url + ')')
    } else {
      fail(plan.reason + ' (' + presetFile + ')')
      say('  fix: edit the preset by hand, or point --preset at a preset that has the compaction group')
      return done(1)
    }
  } else {
    say('PLAN ' + plan.action + ' row in ' + presetFile)
    for (const line of plan.rowText.split(/\r?\n/)) say(line)
    if (flags.apply) {
      const backup = presetFile + '.bak-context-guardian-' + stamp
      const tmp = presetFile + '.tmp-' + process.pid
      try {
        copyFileSync(presetFile, backup)
        writeFileSync(tmp, plan.newText)
        renameSync(tmp, presetFile)
      } catch (e) {
        try {
          if (existsSync(tmp)) unlinkSync(tmp)
        } catch {
          // best effort
        }
        fail('could not write ' + presetFile + ': ' + e.message)
        return done(1)
      }
      let problem = null
      try {
        problem = verifyWritten(presetFile, plan, url)
      } catch (e) {
        problem = e.message
      }
      if (problem) {
        try {
          copyFileSync(backup, presetFile)
        } catch {
          // nothing else to try
        }
        fail('the write did not verify (' + problem + '); ' + presetFile + ' was restored from the backup')
        return done(1)
      }
      result.applied = true
      result.backup = backup
      say('DONE ' + plan.action + ' -- backup ' + backup)
    } else {
      say('dry run -- nothing written; re-run with --apply')
    }
  }

  // 6. Next step
  const next = [
    'start a NEW session in DSH with the agent preset "' + preset + '" selected (no DSH restart needed), e.g. dsh web --profile ' + profileName,
    'in that session type /guardian -- it should report the engine revision and the window'
  ]
  if (preset !== settingsDefault) {
    next.push('(optional) make it the default: set "agent-presets: default: ' + preset + '" in ' + join(dshHome, 'settings.yaml'))
  }
  result.next = next
  for (const line of next) say('NEXT: ' + line)
  return done(0)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main(process.argv.slice(2))
}
