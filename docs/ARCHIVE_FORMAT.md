# Archive format

Everything Context Guardian writes to its span archive, specified closely enough that you could write your own
verifier from this page alone, in any language. Two writers produce it, and they produce it identically: the Python
proxy (`context_guardian.py`, using `cg_archive.py`) and the DeepSeek Harness engine (`engine.js`, using
`cg_seal.js`). The reference verifier is `context-guardian-recall --verify`.

**Tamper-evident, not tamper-proof.** The chain shows whether anything was changed, removed or reordered after it
was written. It cannot stop someone who can write the directory from editing a span and rehashing every chain line
after it. Pinning the head somewhere else (see [Anchor file](#anchor-file)) is what catches that.

## Layout

```
<span_dir>/                      GUARDIAN_SPAN_DIR, default <data dir>/logs/guardian_spans
  <run_id>/                      one directory per proxy process or engine run
    0001.json, 0002.json, ...    span files (archive content)
    journal.jsonl, journal.N.jsonl   capture journal (archive content, optional)
    chain.jsonl                  the seal: one line per archived file event
    index.jsonl                  digest lines for skimming (not sealed; rebuildable)
    precompact-NNNNNN.json       DSH engine state snapshots (not archive content, not sealed)
    chain.lock                   present only while a writer holds the lock
```

`run_id` matches `^[A-Za-z0-9_-]+$`. The proxy uses `YYYYMMDD-HHMMSS` (process start, local time), the DSH engine
`dsh-<YYYYMMDDHHMMSS>-<pid>`, the Claude Code adapter `cc-<session_id>`.

**Archive content** is exactly the files whose names match `^\d{4,}\.json$` (span files) or
`^journal(\.\d+)?\.jsonl$` (journal files). Every such file in a sealed run must be covered by the chain.

Directories are created `0700` and files `0600` on POSIX; Windows ignores modes.

## Span file

UTF-8 JSON, one object, written with an indent of 1. The proxy writes:

| field | type | meaning |
|---|---|---|
| `run_id` | string | the run directory name |
| `index` | integer | the number in the file name |
| `at` | string | `YYYY-MM-DD HH:MM:SS`, local time, when written |
| `num_ctx` | integer | the configured context window |
| `message_count` | integer | `len(messages)` |
| `summary` | string | the summary that replaced these messages in the conversation |
| `messages` | array | the original messages, exactly as the client sent them |

The DSH engine adds `session_id` and `engine`; the Claude Code adapter adds `source` and `lines` (see below).
Readers ignore fields they do not know. The file's bytes are written to a temp file, fsynced, and renamed over an
exclusively created placeholder of the final name, so a span file is either absent, empty (claimed, never
finished, and removed on failure) or complete.

## chain.jsonl

One JSON object per line, `\n`-terminated, appended under the run's lock. Each line is the **canonical form** of
the entry (below), so a line's bytes are exactly what its hash covers.

| field | type | meaning |
|---|---|---|
| `seq` | integer | 1 for the first line, then +1 per line, no gaps |
| `type` | string | `span`, `journal` or `pruned` |
| `file` | string | a file name in this run directory, e.g. `0003.json` |
| `sha256` | string | 64 lowercase hex digits: SHA-256 of the file's bytes as read back from disk |
| `prev` | string | `genesis:<run_id>` on line 1, else the previous line's `entry_hash` |
| `ts` | string | ISO-8601 local time with UTC offset, seconds precision, e.g. `2026-10-23T12:00:00-05:00` |
| `backfilled` | boolean | optional; `true` when the file was sealed after the fact (`--seal-existing`) |
| `entry_hash` | string | 64 lowercase hex digits: SHA-256 of the canonical form of every other field |

Entry types:

- **`span`**: a span file was written; `sha256` is its content.
- **`journal`**: a journal file reached a sealing point; `sha256` is the whole file's content at that moment. A
  journal file is resealed as it grows, so the **last** `journal` line for a file is the one its current bytes must
  match.
- **`pruned`**: retention (`GUARDIAN_KEEP_SPANS`) deleted the file; `sha256` is its content just before deletion.
  The line is appended *before* the file is removed. If it cannot be written, the file is kept.

### Canonical form

The JSON serialisation of an object with:

- keys sorted by Unicode code point,
- no whitespace anywhere (`,` and `:` separators),
- every character outside ASCII written as a `\uXXXX` escape (lowercase hex), control characters escaped as JSON
  requires (`\n`, `\t`, `\u001f`, ...),
- integers without a fraction or exponent, `true`/`false` lowercase.

This is Python's `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`. Every value a chain
carries is ASCII in practice; the escaping rule only keeps the format well defined.

`entry_hash = sha256(canonical(entry without "entry_hash"))`, hex.

Test vector:

```
entry      {"seq": 1, "type": "span", "file": "0001.json", "sha256": "abab…ab" (64 chars),
            "prev": "genesis:20261023-120000", "ts": "2026-10-23T12:00:00-05:00"}
canonical  {"file":"0001.json","prev":"genesis:20261023-120000","seq":1,"sha256":"abab…ab","ts":"2026-10-23T12:00:00-05:00","type":"span"}
entry_hash 46fc20f69dc23fcda35e4c1688428bd5f1a3e8ac5a44f3ea19d5227de02f418d
```

### Locking

Writers take `<run_dir>/chain.lock` by creating it exclusively (`O_CREAT|O_EXCL`, Node's `wx`), write their pid into
it, append, and delete it. A lock file older than 30 seconds is treated as left by a dead writer and removed. A writer
that cannot get the lock within 2 seconds skips the seal and logs a warning; the archived file is still written and
will verify as **unsealed**.

## Verifying a run

A run with archive content and no `chain.jsonl` is **unsealed** (written before 0.12.0, or with `GUARDIAN_SEAL=0`).
Otherwise, check in this order and stop at the first failure:

1. **The chain.** For each line at position *p* (from 1): it parses as an object with the fields above
   (else `unreadable-chain`); its `seq` was not seen before and its `prev` is not already some earlier line's `prev`
   (else `chain-forked`); `entry_hash` recomputes (else `entry-edited`); `seq == p` (else `entry-reordered`);
   `prev` equals the previous line's `entry_hash`, or `genesis:<run_id>` for *p* = 1 (else `entry-reordered` when it
   names an earlier line, `entry-edited` otherwise).
2. **The files.** For each file, take its last `span`/`journal` line, unless a later `pruned` line names it. A pruned
   file is expected to be gone; if its pruned `sha256` differs from the sealed one, report `content-changed`. A sealed
   file that does not exist is `file-missing`; one whose SHA-256 differs is `content-changed`.
3. **Unsealed files.** Archive content present in the directory with no chain line is `unsealed-file`.
4. **The anchor**, when one is given: every `(seq, head)` recorded for this run must equal the chain's `entry_hash`
   at that `seq` (else `anchor-mismatch`).

A run passes when none of these fail. `context-guardian-recall --verify` exits 0 only when every checked run passes.

What each kind means, in short: `content-changed`, a file's bytes changed; `file-missing`, a file was removed
without a prune record; `entry-edited` / `entry-reordered` / `chain-forked`, the chain itself was altered;
`unsealed-file`, a file the chain does not know (a seal that failed, or something added later);
`anchor-mismatch`, the chain was rewritten after its head was copied away.

Truncating the end of a chain *and* deleting the matching files leaves a consistent shorter chain. Only an anchor
catches that.

## Anchor file

`GUARDIAN_SEAL_ANCHOR_PATH`, when set, names a file that every writer appends to after each chain line, one
canonical-form object per line:

```
{"head":"<entry_hash>","run":"<run_id>","seq":3,"ts":"2026-10-23T12:00:00-05:00"}
```

Put it outside the data directory, somewhere the people and processes that can write the archive cannot rewrite:
synced storage with version history, another disk, a machine you trust. A verifier reads every line (skipping any
that do not parse, such as a torn last line) and checks each against the chain.
