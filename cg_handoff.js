// cg_handoff.js -- context-guardian hand-off writer (C-Handoff)
//
// Turns the context-guardian per-session memory (pinned facts, decisions, files)
// into a hand-off file another session -- or Claude, via the escalate_to_claude
// tool -- can read to continue the work without the conversation.
//
// Standalone ES module, Node 20+, built-ins only: node:fs, node:path, node:url.

import { mkdirSync, writeFileSync, renameSync, mkdtempSync, rmSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const SCHEMA = 1;

// A session id becomes a file name, so it is restricted to a conservative set.
const SESSION_ID_RE = /^[A-Za-z0-9._-]{1,128}$/;

function isSessionId(s) {
    return typeof s === 'string' && SESSION_ID_RE.test(s);
}

function itemsOf(memory) {
    return memory && Array.isArray(memory.items) ? memory.items : [];
}

function bySeq(a, b) {
    return (a && a.seq ? a.seq : 0) - (b && b.seq ? b.seq : 0);
}

/**
 * Build the plain hand-off object from a context-guardian memory.
 * Pins and files are scoped to `session`; decisions carry across sessions.
 * Superseded pins are kept here (with their flag) and only hidden in the
 * markdown rendering.
 */
export function buildHandoff(memory, session, opts = {}) {
    opts = opts || {};

    const items = itemsOf(memory);

    const pins = items
        .filter(item => item && item.cat === 'pins' && item.session === session)
        .sort(bySeq)
        .map(item => ({
            text: item.text,
            seq: item.seq,
            saidBy: item.saidBy || 'user',
            superseded: item.superseded || false
        }));

    const decisions = items
        .filter(item => item && item.cat === 'decisions')
        .sort(bySeq)
        .map(item => item.text);

    const files = items
        .filter(item => item && item.cat === 'files' && item.session === session)
        .sort(bySeq)
        .map(item => ({
            path: item.text,
            last_action: item.action || 'read'
        }));

    return {
        schema: SCHEMA,
        session,
        written_at: opts.now || new Date().toISOString(),
        goal: opts.goal || '',
        pins,
        decisions,
        rejected: [],
        files,
        open_question: opts.openQuestion ?? null,
        last_step: opts.lastStep ?? null
    };
}

/**
 * Render the hand-off as markdown. Lines joined with "\n", ending with "\n".
 * Superseded pins are not shown.
 */
export function renderHandoffMd(h) {
    h = h || {};
    const pins = Array.isArray(h.pins) ? h.pins : [];
    const decisions = Array.isArray(h.decisions) ? h.decisions : [];
    const files = Array.isArray(h.files) ? h.files : [];

    const lines = [];
    lines.push(`# Hand-off ${h.session}`);
    lines.push('');
    lines.push(`GOAL: ${h.goal}`);
    lines.push('');

    lines.push('## Pins');
    for (const pin of pins) {
        if (!pin || pin.superseded) continue;
        lines.push(`- ${pin.text}`);
    }
    lines.push('');

    lines.push('## Decisions');
    for (const decision of decisions) {
        lines.push(`- ${decision}`);
    }
    lines.push('');

    lines.push('## Files');
    for (const file of files) {
        lines.push(`- ${file.path} (${file.last_action})`);
    }
    lines.push('');

    if (h.open_question != null) {
        lines.push(`OPEN QUESTION: ${h.open_question}`);
    }
    if (h.last_step != null) {
        lines.push(`LAST STEP: ${h.last_step}`);
    }

    // Exactly one trailing newline, whether or not an OPEN QUESTION / LAST STEP
    // line was appended above.
    if (lines[lines.length - 1] !== '') lines.push('');

    return lines.join('\n');
}

/**
 * Write the hand-off files into `dir` (created if needed) and return the
 * absolute paths written: {json, md, latest}.
 * Synchronous on purpose -- callers may read the files back immediately.
 */
export function writeHandoff(dir, h) {
    const session = h && h.session;

    // Validate before touching the filesystem so a bad id writes nothing.
    if (!isSessionId(session)) {
        throw new Error('bad session id');
    }

    const base = resolve(dir);
    mkdirSync(base, { recursive: true });

    const jsonFile = join(base, `handoff_${session}.json`);
    const latestJsonFile = join(base, 'handoff_latest.json');
    const mdFile = join(base, `handoff_${session}.md`);

    const json = JSON.stringify(h, null, 2);
    const md = renderHandoffMd(h);

    const writeAtomic = (target, content) => {
        const tmp = `${target}.tmp`;
        writeFileSync(tmp, content, 'utf8');
        renameSync(tmp, target);
    };

    writeAtomic(jsonFile, json);
    writeAtomic(latestJsonFile, json);
    writeAtomic(mdFile, md);

    return { json: jsonFile, md: mdFile, latest: latestJsonFile };
}

// A writable scratch directory, using only node:fs/node:path.
function scratchBase() {
    const env = process.env.TMPDIR || process.env.TEMP || process.env.TMP;
    if (env) return env;
    return process.platform === 'win32' ? process.cwd() : '/tmp';
}

export function selftest() {
    const MEM = {
        version: 1,
        updated: null,
        items: [
            { cat: 'pins', text: 'B pin', seq: 5, session: 's1' },
            { cat: 'pins', text: 'A pin', seq: 2, session: 's1' },
            { cat: 'pins', text: 'old', seq: 3, session: 's1', superseded: true },
            { cat: 'pins', text: 'other', seq: 1, session: 's2' },
            { cat: 'decisions', text: 'use py3.11', seq: 4, session: 's2' },
            { cat: 'files', text: 'a.py', seq: 6, session: 's1', action: 'edit' }
        ]
    };

    const checks = [
        ['pins_sorted_and_scoped', () => {
            return buildHandoff(MEM, 's1').pins.map(p => p.text).join(',') === 'A pin,old,B pin';
        }],
        ['saidBy_default_user', () => {
            return buildHandoff(MEM, 's1').pins[0].saidBy === 'user';
        }],
        ['decisions_any_session', () => {
            return buildHandoff(MEM, 's1').decisions[0] === 'use py3.11';
        }],
        ['files_action', () => {
            return buildHandoff(MEM, 's1').files[0].last_action === 'edit';
        }],
        ['empty_memory', () => {
            return buildHandoff(null, 'x').pins.length === 0;
        }],
        ['md_hides_superseded', () => {
            return !renderHandoffMd(buildHandoff(MEM, 's1')).includes('- old');
        }],
        ['md_goal_line', () => {
            return renderHandoffMd(buildHandoff(MEM, 's1', { goal: 'G' })).split('\n')[2] === 'GOAL: G';
        }],
        ['write_and_bad_id', () => {
            const tmpDir = mkdtempSync(join(scratchBase(), 'cgh-selftest-'));
            try {
                const h = buildHandoff(MEM, 's1');
                const out = writeHandoff(tmpDir, h);
                if (!existsSync(out.latest) || !existsSync(out.json) || !existsSync(out.md)) {
                    return false;
                }
                let threw = false;
                try {
                    writeHandoff(tmpDir, { ...h, session: '../x' });
                } catch (e) {
                    threw = e instanceof Error && e.message === 'bad session id';
                }
                return threw;
            } finally {
                rmSync(tmpDir, { recursive: true, force: true });
            }
        }]
    ];

    let passed = 0;
    let failed = 0;

    for (const [name, fn] of checks) {
        let ok = false;
        try {
            ok = !!fn();
        } catch (e) {
            ok = false;
        }
        if (ok) passed++;
        else failed++;
    }

    console.log(`cg_handoff selftest: ${checks.length} checks, ${passed} passed, ${failed} failed`);

    return { checks: checks.length, passed, failed };
}

// Run directly: `node cg_handoff.mjs --selftest`. The argv guard matters --
// importing this module where process.argv[1] is undefined must not throw.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    if (process.argv.includes('--selftest')) {
        const result = selftest();
        process.exit(result.failed === 0 && result.checks > 0 ? 0 : 1);
    }
}