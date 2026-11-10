#!/usr/bin/env node
// launch.mjs -- `npm run launch -- <client> [args]` (or `node launch.mjs <client> [args]`).
// Runs cg_launch.py (the same command as `context-guardian launch`) with the Python the DSH bridge would
// use: $GUARDIAN_PYTHON, a .venv beside this package, then python3 / python on PATH. It starts the proxy
// if needed, points one client at it for that one process, and stops what it started (ADR 0010).
// npm runs scripts in the package folder, so the client starts in $INIT_CWD, where the user typed the command.
// On Windows `npm run` goes through npm.cmd, which can show "Terminate batch job (Y/N)?" on Ctrl-C:
// start it with `node launch.mjs <client>` there.
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const win = process.platform === 'win32'

function candidates(env = process.env) {
  const found = []
  const given = (env.GUARDIAN_PYTHON || '').trim()
  if (given) found.push(given)
  const venv = join(here, '.venv', win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python')
  if (existsSync(venv)) found.push(venv)
  found.push(win ? 'python' : 'python3', 'python')
  return found
}

function pythonWithProxyDeps() {
  const tried = []
  for (const py of candidates()) {
    const probe = spawnSync(py, ['-c', 'import sys, fastapi, uvicorn, httpx; assert sys.version_info >= (3, 10)'], { stdio: 'ignore' })
    if (probe.status === 0) return { py, tried }
    tried.push(py)
  }
  return { py: null, tried }
}

const { py, tried } = pythonWithProxyDeps()
if (!py) {
  process.stderr.write(
    `context-guardian launch: no Python 3.10+ with the proxy's packages (fastapi, uvicorn, httpx) was found.\n` +
    `Tried: ${tried.join(', ')}\n` +
    `Install them into one of those interpreters, e.g.: ${tried[0] || 'python'} -m pip install context-guardian\n` +
    `or point GUARDIAN_PYTHON at an interpreter that has them.\n`)
  process.exit(2)
}

// A listener (not "ignore"): an ignored SIGINT would be inherited by the client and cost it Ctrl-C.
process.on('SIGINT', () => {})
const child = spawn(py, [join(here, 'cg_launch.py'), ...process.argv.slice(2)], {
  stdio: 'inherit',
  cwd: process.env.INIT_CWD || process.cwd(),
})
for (const sig of ['SIGTERM', 'SIGHUP']) process.on(sig, () => { try { child.kill(sig) } catch { /* already gone */ } })
child.on('error', (err) => { process.stderr.write(`context-guardian launch: ${err.message}\n`); process.exit(2) })
child.on('exit', (code, signal) => process.exit(code ?? (signal ? 128 + ({ SIGINT: 2, SIGTERM: 15, SIGHUP: 1, SIGKILL: 9 }[signal] || 1) : 2)))
