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

## Journal files

Written only with `GUARDIAN_CAPTURE=journal`. `journal.jsonl`, then `journal.1.jsonl`, `journal.2.jsonl`, ... in
write order: a file is closed (sealed one last time) and the next one started before a line would take it past
`min(32 MiB, GUARDIAN_CAPTURE_MAX_MB / 4)`. Capture stops for the run before the total would pass
`GUARDIAN_CAPTURE_MAX_MB`. One JSON object per line, UTF-8:

| field | type | meaning |
|---|---|---|
| `n` | integer | the message's number in the run's journal, from 0, continuous across files |
| `request` | integer | which request of the run (from 1) first carried this message |
| `ts` | string | ISO-8601 local time with offset, when written |
| `hash` | string | the message's identity hash (below) |
| `message` | object | the message exactly as the client sent it |

**Identity hash:** SHA-256 over four parts, for `role`, `content`, `tool_calls` and `tool_call_id` in that order.
Each part is `s` + the UTF-8 bytes when the value is a string, otherwise `j` + its JSON (keys sorted, no whitespace,
non-ASCII unescaped; `null` when absent). Each part is preceded by its length as 8 bytes big-endian. A request's
k-th copy of a hash is journaled when the journal holds fewer than k copies, so a message that genuinely repeats is
journaled each time and a resent history adds nothing. The proxy's own summary messages are never journaled.

**Sealing:** `journal` chain lines carry an extra integer field `size`, the number of bytes of the file their
`sha256` covers. A journal is sealed in batches (every 64 lines, every 5 seconds of activity, at rotation, when
capture stops, and at shutdown), so a verifier hashes the first `size` bytes of the file against the **last**
`journal` line for it. Bytes past `size` were written after the last seal: report them as an uncovered tail, not as a
break.

## index.jsonl

One JSON object per line, one line per span, appended when the span is written; not sealed and not fsynced, because
`context-guardian-recall --reindex` rebuilds it from the span files. Each line is a pure function of the span file's
parsed content, so a rebuild produces exactly what live indexing wrote, and the DSH engine computes the same lines.

| field | type | meaning |
|---|---|---|
| `span` | string | the file name without `.json`, e.g. `0003` |
| `ts` | string | the span's `at` field verbatim (`""` if absent) |
| `messages` | integer | number of messages in the span |
| `chars` | integer | Unicode code points of the span's *plain text*: each message's string `content`, or the `text` of its text blocks, joined with `\n` (tool-call JSON is not counted) |
| `est_tokens` | integer | `floor(chars / 3.5)` |
| `digest` | string | the first sentence of `summary`; when the span has no summary, of the first user message with text; failing that, of the first message with text. At most 160 code points; longer ones are cut to 157 plus `...` |
| `digest_from` | string | `summary`, `first-user-message`, or `first-message` (no user message has text: the first message of any role that does) |
| `terms` | array of strings | up to 8 keywords |

**First sentence:** collapse every run of whitespace (Python's `str.split()` set) to one space, then take the
shortest prefix that ends in `.`, `!` or `?` and is followed by a space or the end; if there is none, the whole
collapsed text.

A message's *role* and *text* are read from the message itself, or, for a Claude Code transcript line that has no
`content` of its own but a `message` object, from that inner message (`role` falls back to the line's `type`).

**Keywords:** lowercase the first 50,000 code points of the plain text; take every non-overlapping match of
`[a-z0-9_]{3,40}`, left to right; drop all-digit words and the stopwords listed in `cg_archive.STOPWORDS` (the same
list is in `cg_seal.js`); count; sort by count descending, then word ascending; keep the first 8.

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
   file that does not exist is `file-missing`; one whose SHA-256 differs is `content-changed`. For a `journal` line
   with `size`, hash only the first `size` bytes.
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
