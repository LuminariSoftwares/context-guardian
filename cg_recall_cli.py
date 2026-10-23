#!/usr/bin/env python3
"""cg_recall_cli.py -- search the span archive context_guardian.py writes on every compaction.

    context-guardian-recall <term> [--run RUN_ID]
    context-guardian-recall --verify [--run RUN_ID | --all] [--heads] [--seal-existing]
    context-guardian-recall --receipts [--since 7d] [--share]

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
short block with no paths, no text and no run ids.
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


def message_text(m) -> str:
    """Everything searchable in one archived message, as one string."""
    if not isinstance(m, dict):
        return str(m)
    parts = []
    content = m.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    for key in ("tool_calls", "function_call"):
        if m.get(key):
            parts.append(json.dumps(m[key], ensure_ascii=False))
    return "\n".join(parts)


def snippet(text: str, at: int, length: int) -> str:
    start = max(0, at - SNIPPET_CHARS)
    end = min(len(text), at + length + SNIPPET_CHARS)
    out = " ".join(text[start:end].split())
    return ("..." if start > 0 else "") + out + ("..." if end < len(text) else "")


def search(root: Path, term: str, run_id: str | None = None):
    """Yield (path, message index or 'summary', role, snippet) per hit, oldest
    span first. One hit per message. Unreadable span files are skipped."""
    needle = term.lower()
    pattern = f"{run_id}/*.json" if run_id else "*/*.json"
    for path in sorted(root.glob(pattern)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for i, m in enumerate(data.get("messages") or []):
            text = message_text(m)
            at = text.lower().find(needle)
            if at >= 0:
                role = m.get("role", "?") if isinstance(m, dict) else "?"
                yield path, i, role, snippet(text, at, len(term))
        summary = data.get("summary")
        if isinstance(summary, str):
            at = summary.lower().find(needle)
            if at >= 0:
                yield path, "summary", "summary", snippet(summary, at, len(term))


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
    group = parser.add_argument_group("receipts")
    group.add_argument("--receipts", action="store_true",
                       help="summarise the counts-only recall receipts")
    group.add_argument("--since", metavar="AGE", default=None,
                       help="with --receipts: only the last AGE, e.g. 24h, 7d, 2w")
    group.add_argument("--share", action="store_true",
                       help="with --receipts: a short copy-paste block with no paths, text or run ids")
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = span_dir()
    if ns.receipts or ns.share:
        return cmd_receipts(ns, root)
    if ns.verify or ns.heads or ns.seal_existing:
        return cmd_verify(ns, root)
    if not ns.term:
        parser.error("give a search term, or --verify")
    if not root.is_dir():
        print(f"no span archive at {root} (set GUARDIAN_SPAN_DIR)", file=sys.stderr)
        return 2
    hits, chars, matched = 0, 0, set()
    for path, idx, role, text in search(root, ns.term, ns.run):
        label = "summary" if idx == "summary" else f"msg {idx} [{role}]"
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
