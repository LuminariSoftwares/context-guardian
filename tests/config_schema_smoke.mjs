// The bundle's own patch row must pass its own Config schema (2026-10-03: idleCompactRatio defaulted to 0 while
// ratio() demanded >= 0.05, so DSH refused to load the plugin -- "invalid config" -- on a benchmark profile).
// usage: node tests/config_schema_smoke.mjs
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Config } from '../index.js'
const here = dirname(fileURLToPath(import.meta.url))
const res = []
const check = (n, c, why = '') => { res.push(!!c); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
const ok = (cfg) => { try { Config(cfg); return true } catch (e) { return String(e && e.message).slice(0, 160) } }
check('empty_config_valid', ok({}) === true, ok({}))
check('idle_ratio_0_means_off_valid', ok({ idleCompactRatio: 0 }) === true, ok({ idleCompactRatio: 0 }))
check('idle_ratio_045_valid', ok({ idleCompactRatio: 0.45 }) === true)
check('idle_ratio_negative_refused', ok({ idleCompactRatio: -1 }) !== true)
// every numeric key of the shipped patch row, as written in cordis.patch.yml
const yml = readFileSync(join(here, '..', 'cordis.patch.yml'), 'utf8')
const row = {}
for (const m of yml.matchAll(/^\s{8}([A-Za-z]+):\s*(-?[0-9.]+)\s*$/gm)) row[m[1]] = Number(m[2])
check('patch_row_numbers_valid', Object.keys(row).length >= 5 && ok(row) === true, JSON.stringify(row) + ' ' + ok(row))
const p = res.filter(Boolean).length
console.log(`config_schema_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
