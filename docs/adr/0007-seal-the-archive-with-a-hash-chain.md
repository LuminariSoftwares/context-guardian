# 0007. Seal the archive with a hash chain; anchor it outside

**Status:** accepted. 0.12.0.

## Context

0003 made compaction "lossless on disk", and nothing could check it. A span that was edited, truncated, restored
from an old backup or deleted looked exactly like one that never changed, and a summary pointing at it would be
trusted either way. Archives are also pruned on purpose (`GUARDIAN_KEEP_SPANS`), so "a file is missing" alone is
not evidence of anything.

## Decision

Every archived file gets a line in `<run>/chain.jsonl`: the SHA-256 of its bytes as read back from disk after an
atomic write (temp file, fsync, rename), the previous line's hash, and a hash of the line itself over a canonical
JSON form. Retention appends a chained `pruned` line before it deletes. `context-guardian-recall --verify` walks
the chain and the files and reports OK or the first break by kind. The DSH engine writes the same chain
(`cg_seal.js`); one golden hash pins the canonical form in both test suites. The format is specified in
`docs/ARCHIVE_FORMAT.md` so a verifier can be written without this code.

Because whoever can write the directory can also rehash a whole chain, the chain is described everywhere as
tamper-**evident**, never tamper-proof, and `GUARDIAN_SEAL_ANCHOR_PATH` copies each new head to a file meant to live
elsewhere; `--verify` checks the chain against it. Sealing fails open (0001): a seal that cannot be written is
logged and counted, the file stays, and the verifier later reports it as unsealed.

## Alternatives

- **Hash files without chaining them**: catches an edited span, not a deleted or reordered one, and a pruned span
  is indistinguishable from a removed one.
- **Signatures with a key**: proves who sealed, but a key on the same machine has the same reach as the person who
  could edit the files, and key management is a burden a single-user tool should not impose. The anchor gets the
  part that matters (a copy out of reach) with no key at all.
- **An append-only database or a transparency log service**: a dependency, or a network call, on the request path
  of a proxy whose rule is to stay small and local.

## Consequences

- Each compaction pays a few milliseconds more (the durable write and the seal; measured in the README's
  Benchmark). Requests that do not compact pay nothing.
- Archives from before 0.12.0 verify as unsealed until `--seal-existing` seals them, labelled `backfilled`.
- A chain truncated together with its files is only caught by an anchor; the docs say so.
