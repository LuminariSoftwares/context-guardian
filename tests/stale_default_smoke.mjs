// 0.1.0-alpha.9 (2026-10-04): stale recall is ON by default -- it passed its bar on the variant benchmark
// (S1/S2/S3 12/12 current, 0 stale, early 3/3; pins-only R1/R2 9-12/12 with 3 stale).
// Written red first against 0.1.0-alpha.8's `staleRecall: false`.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const res = [];
const check = (n, c) => { res.push(!!c); console.log((c ? "ok   " : "FAIL ") + n); };
const eng = read("engine.js");
check("engine_default_stale_recall_on", /staleRecall:\s*true,/.test(eng));
check("engine_option_off_only_when_false", /pick\('staleRecall'\)\s*!==\s*false/.test(eng));
check("engine_env_zero_still_turns_it_off", /envStale === '0' \|\| envStale === 'false' \? false/.test(eng));
check("post_answer_check_stays_opt_in", /postAnswerCheck:\s*false,/.test(eng) && /pick\('postAnswerCheck'\)\s*===\s*true/.test(eng));
check("docs_say_on", /\| staleRecall \| true \|/.test(read("docs/dsh-integration.md")));
const p = res.filter(Boolean).length;
console.log(`stale_default_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`);
process.exit(res.length && p === res.length ? 0 : 1);
