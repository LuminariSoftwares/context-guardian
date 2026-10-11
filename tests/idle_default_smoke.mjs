// The idle trigger is OFF by default on EVERY install path (2026-10-03).
// engine.js and docs/dsh-integration.md said off since 0.1.0-alpha.8, but the npm entry (index.js schema) and the
// bundle patch (cordis.patch.yml) still shipped 0.45 -- so a public install compacted on idle and the append-only
// chain never ran (measured run: 17 idle compactions, 0 chain events).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const res = [];
const check = (n, c) => { res.push(!!c); console.log((c ? "ok   " : "FAIL ") + n); };
check("engine_default_idle_off", /idleCompactRatio:\s*0,/.test(read("engine.js")));
// 2026-10-03: ratio() demands >= 0.05, so the schema needs its own min(0) bound -- tests/config_schema_smoke.mjs proves it validates
check("index_schema_default_idle_off", /idleCompactRatio:\s*Schema\.number\(\)\.min\(0\)\.max\(0\.99\)\.default\(0\)/.test(read("index.js")));
check("bundle_patch_idle_off", /^\s*idleCompactRatio:\s*0\s*$/m.test(read("cordis.patch.yml")));
check("docs_say_off", /idleCompactRatio \| 0 \(off\)/.test(read("docs/dsh-integration.md")));
const p = res.filter(Boolean).length;
console.log(`idle_default_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`);
process.exit(res.length && p === res.length ? 0 : 1);
