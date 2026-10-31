#!/usr/bin/env python3
"""cg_recall_cli.py -- search the span archive context_guardian.py writes on every compaction.

    context-guardian-recall <term> [--run RUN_ID]
    context-guardian-recall --verify [--run RUN_ID | --all] [--heads] [--seal-existing]
    context-guardian-recall --receipts [--since 7d] [--share]
    context-guardian-recall --savings [--since 7d] [--json]
    context-guardian-recall --reindex [--run RUN_ID]

A compaction folds older messages into a summary and archives the full text as
<GUARDIAN_SPAN_DIR>/<run_id>/<NNNN>.json. The summary the model receives names
this command so the model can find a detail again without reading a span whole
(a span is, by construction, larger than the room the compaction freed).

Case-insensitive substring match over each archived message (text, content
blocks, tool calls) and the span's own summary. One line per hit:
`<span file> msg <i> [<role>]: ...snippet...`. Exit 0 with hits, 1 without,
2 when the span directory does not exist.

--verify walks each run's chain.jsonl (cg_archive.py) and prints OK or the
first break per run; exit 0 only when every checked run is OK. Without --run
or --all it checks the most recently written run. --heads prints each run's
chain head; --seal-existing seals spans written before sealing existed, marked
backfilled. GUARDIAN_SEAL_ANCHOR_PATH, when that file exists, is checked too.

--receipts adds up the counts-only receipts every recall path writes (see
cg_archive.py): archive size, recall calls, spans opened, the share of the
archive read back, tokens saved, seal status. --share prints the same as a
short block with no paths, no text and no run ids. --reindex rebuilds the
skim index (index.jsonl) for spans written before it existed.

--savings adds up the compaction log (GUARDIAN_LOG_PATH) and the receipts:
how many tokens the forwarded requests held before and after compaction, the
percent smaller, the oversized pastes cut to head + tail, summaries reused and
recall reads. Every figure is counted, none estimated; --json prints it as one
object. A bad --since AGE exits 2.
Stdlib only, Python 3.10+. MIT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cg_archive
import cg_paths

PACKAGE_ROOT = Path(__file__).resolve().parent
SNIPPET_CHARS = 80
DEFAULT_MAX_HITS = 50
LOCKED_NOTE = "encrypted; set GUARDIAN_ARCHIVE_KEY to search it"


def span_dir() -> Path:
    """The proxy's own resolution: $GUARDIAN_SPAN_DIR (also from the .env the
    proxy loads), else <data dir>/logs/guardian_spans. The data directory is
    the checkout for a clone and a per-user directory for an installed
    package -- see cg_paths.py, which the proxy and the MCP server share, so
    all three read the archive the proxy wrote."""
    data = cg_paths.data_dir()
    try:
        from dotenv import load_dotenv
        load_dotenv(data / cg_paths.ENV_FILENAME, override=False)
        if data != PACKAGE_ROOT:
            load_dotenv(PACKAGE_ROOT / cg_paths.ENV_FILENAME, override=False)
    except ImportError:  # pragma: no cover -- optional, never fatal
        pass
    return Path(os.environ.get("GUARDIAN_SPAN_DIR",
                               str(data / cg_paths.SPANS_SUBDIR)))


# One definition, shared with the index and the MCP server (cg_archive.py).
message_text = cg_archive.message_text


def snippet(text: str, at: int, length: int) -> str:
    start = max(0, at - SNIPPET_CHARS)
    end = min(len(text), at + length + SNIPPET_CHARS)
    out = " ".join(text[start:end].split())
    return ("..." if start > 0 else "") + out + ("..." if end < len(text) else "")


def search(root: Path, term: str, run_id: str | None = None):
    """Yield (path, message index or 'summary', role, snippet) per hit, oldest
    span first, then the run's capture journal (GUARDIAN_CAPTURE=journal; its
    index is the message's number in the journal). One hit per message.
    Unreadable span files are skipped."""
    needle = term.lower()
    pattern = f"{run_id}/*.json" if run_id else "*/*.json"
    for path in sorted(root.glob(pattern)):
        data = cg_archive.load_span(path)
        if data is None:
            continue
        if data.get("locked"):
            yield path, "locked", "locked", LOCKED_NOTE
            continue
        for i, m in enumerate(data.get("messages") or []):
            text = message_text(m)
            at = text.lower().find(needle)
            if at >= 0:
                role = cg_archive.message_role(m)
                yield path, i, role, snippet(text, at, len(term))
        summary = data.get("summary")
        if isinstance(summary, str):
            at = summary.lower().find(needle)
            if at >= 0:
                yield path, "summary", "summary", snippet(summary, at, len(term))
    runs = [root / run_id] if run_id else cg_archive.run_dirs(root)
    for run in runs:
        for path, entry in cg_archive.read_journal(run):
            m = entry.get("message")
            text = message_text(m)
            at = text.lower().find(needle)
            if at >= 0:
                role = cg_archive.message_role(m)
                yield path, entry["n"], role, snippet(text, at, len(term))


def ranked_search(root: Path, query: str, run: str | None = None):
    """Hits for a query of one or more words, best first: messages carrying the
    whole phrase, then by how many distinct words they carry, then the newer
    span first. Each word matches as a case-insensitive substring, as the
    single-term search always has. Yields (path, index or 'summary', role,
    snippet, hits)."""
    words = list(dict.fromkeys(query.lower().split()))
    phrase = " ".join(query.lower().split())
    if not words:
        return []
    pattern = f"{run}/*.json" if run else "*/*.json"
    sources, locked = [], []
    for path in root.glob(pattern):
        if not cg_archive.SPAN_NAME_RE.match(path.name):
            continue
        data = cg_archive.load_span(path)
        if data is None:
            continue
        if data.get("locked"):
            locked.append((path, "locked", "locked", LOCKED_NOTE, 0))
            continue
        texts = [(i, cg_archive.message_role(m), message_text(m))
                 for i, m in enumerate(data.get("messages") or [])]
        if isinstance(data.get("summary"), str):
            texts.append(("summary", "summary", data["summary"]))
        sources.append((path, texts))
    # The capture journal (GUARDIAN_CAPTURE=journal) is searched too; a hit's
    # index is the message's number in the journal, which read_span index 0 opens.
    for run_dir in ([root / run] if run else cg_archive.run_dirs(root)):
        by_file: dict = {}
        for path, entry in cg_archive.read_journal(run_dir):
            m = entry.get("message")
            by_file.setdefault(path, []).append(
                (entry["n"], cg_archive.message_role(m), message_text(m)))
        sources.extend(by_file.items())
    found = []
    for path, texts in sources:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        for idx, role, text in texts:
            low = text.lower()
            hits = sum(1 for w in words if w in low)
            if not hits:
                continue
            whole = phrase in low
            first = low.find(phrase) if whole else min(low.find(w) for w in words if w in low)
            length = len(phrase) if whole else next(len(w) for w in words if low.find(w) == first)
            rank = (whole, hits, mtime, str(path), -(idx if isinstance(idx, int) else 1 << 30))
            found.append((rank, path, idx, role, snippet(text, first, length), hits))
    found.sort(key=lambda t: t[0], reverse=True)
    return [(path, idx, role, snip, hits) for _, path, idx, role, snip, hits in found] + sorted(locked)


def anchor_path():
    """GUARDIAN_SEAL_ANCHOR_PATH when it names a file that exists, else None.
    Read after span_dir() so a value from .env counts."""
    value = (os.environ.get("GUARDIAN_SEAL_ANCHOR_PATH") or "").strip()
    return Path(value) if value and Path(value).is_file() else None


def cmd_verify(ns, root: Path) -> int:
    if not root.is_dir():
        print(f"no span archive at {root} (set GUARDIAN_SPAN_DIR)", file=sys.stderr)
        return 2
    anchor = anchor_path()
    if ns.seal_existing:
        for run, count in cg_archive.seal_existing(root, ns.run, anchor).items():
            if count:
                print(f"{run}  sealed {count} file(s) as backfilled")
        ns.all = ns.run is None
    if ns.heads:
        found = cg_archive.heads(root)
        for run, (seq, head) in found.items():
            if ns.run is None or ns.run == run:
                print(f"{run}  seq {seq}  head {head}")
        if not found:
            print("no sealed runs yet")
        if anchor:
            recorded = sum(len(v) for v in cg_archive.read_anchor(anchor).values())
            print(f"anchor {anchor}: {recorded} head(s) recorded")
        return 0
    reports = cg_archive.verify(root, run=ns.run, all_runs=ns.all, anchor_path=anchor)
    if not reports:
        print(f"nothing archived under {root} yet")
        return 0 if ns.run is None else 1
    for report in reports:
        print(cg_archive.format_report(report))
    counts = cg_archive.summarise(reports)
    bad = counts["breaks"] + counts["unsealed"]
    print(f"{counts['runs']} run(s) checked: {counts['runs'] - bad} OK, "
          f"{counts['breaks']} broken, {counts['unsealed']} unsealed"
          + ("" if anchor else " (no anchor file; set GUARDIAN_SEAL_ANCHOR_PATH to pin heads)"))
    return 0 if bad == 0 else 1


def cmd_receipts(ns, root: Path) -> int:
    try:
        since = cg_archive.parse_since(ns.since)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    events = cg_archive.read_receipts(cg_archive.receipts_path(), since)
    reports = cg_archive.verify(root, all_runs=True, anchor_path=anchor_path()) if root.is_dir() else []
    summary = cg_archive.receipts_summary(events, root, reports)
    window = "last %s" % ns.since if ns.since else "all time"
    print(cg_archive.format_receipts(summary, window, share=ns.share))
    if not cg_archive.receipts_enabled():
        print("(GUARDIAN_RECEIPTS=0: nothing new is being recorded)", file=sys.stderr)
    return 0


def cmd_savings(ns) -> int:
    try:
        since = cg_archive.parse_since(ns.since)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    window = "last %s" % ns.since if ns.since else "all time"
    summary = cg_archive.savings_summary(cg_archive.read_log(cg_archive.log_path(), since),
                                         cg_archive.read_receipts(cg_archive.receipts_path(), since), window)
    print(json.dumps(summary) if ns.json else cg_archive.format_savings(summary))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="context-guardian-recall",
        description="Search, and verify, the spans Context Guardian archived when it compacted a conversation.")
    parser.add_argument("term", nargs="?", help="text to find (case-insensitive)")
    parser.add_argument("--run", metavar="RUN_ID", default=None,
                        help="only this proxy run (the directory name under the span dir)")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_HITS, metavar="N",
                        help="stop after N hits (default %(default)s)")
    group = parser.add_argument_group("verify the sealed archive")
    group.add_argument("--verify", action="store_true",
                       help="check each run's chain.jsonl against the files on disk")
    group.add_argument("--all", action="store_true",
                       help="with --verify: every run (default: the most recent run)")
    group.add_argument("--heads", action="store_true",
                       help="with --verify: print each run's chain head")
    group.add_argument("--seal-existing", action="store_true",
                       help="with --verify: seal files written before sealing existed (marked backfilled)")
    group.add_argument("--reindex", action="store_true",
                       help="rebuild each run's index.jsonl (the skim digests) from its span files")
    group = parser.add_argument_group("receipts")
    group.add_argument("--receipts", action="store_true",
                       help="summarise the counts-only recall receipts")
    group.add_argument("--since", metavar="AGE", default=None,
                       help="with --receipts or --savings: only the last AGE, e.g. 24h, 7d, 2w")
    group.add_argument("--share", action="store_true",
                       help="with --receipts: a short copy-paste block with no paths, text or run ids")
    group = parser.add_argument_group("savings")
    group.add_argument("--savings", action="store_true",
                       help="before -> after token totals from the compaction log, oversized pastes cut, recall reads")
    group.add_argument("--json", action="store_true",
                       help="with --savings: one JSON object instead of text")
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = span_dir()
    if ns.savings:
        return cmd_savings(ns)
    if ns.receipts or ns.share:
        return cmd_receipts(ns, root)
    if ns.reindex:
        if not root.is_dir():
            print(f"no span archive at {root} (set GUARDIAN_SPAN_DIR)", file=sys.stderr)
            return 2
        for run, count in cg_archive.reindex(root, ns.run).items():
            print(f"{run}  indexed {count} span(s)")
        return 0
    if ns.verify or ns.heads or ns.seal_existing:
        return cmd_verify(ns, root)
    if not ns.term:
        parser.error("give a search term, or --verify")
    if not root.is_dir():
        print(f"no span archive at {root} (set GUARDIAN_SPAN_DIR)", file=sys.stderr)
        return 2
    hits, chars, matched = 0, 0, set()
    for path, idx, role, text in search(root, ns.term, ns.run):
        label = idx if idx in ("summary", "locked") else f"msg {idx} [{role}]"
        line = f"{path} {label}: {text}"
        print(line)
        hits += 1
        chars += len(line)
        matched.add(path)
        if hits >= ns.max:
            print(f"(stopped at {ns.max} hits; narrow the term or pass --run)")
            break
    cg_archive.record_recall("cli.search", query_terms=len(ns.term.split()),
                             spans_matched=len(matched), chars_returned=chars)
    if hits == 0:
        where = f"{root / ns.run}" if ns.run else str(root)
        print(f"no match for {ns.term!r} under {where}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
