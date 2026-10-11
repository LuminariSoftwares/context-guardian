#!/usr/bin/env python3
"""cg_recall_cli.py -- search the span archive context_guardian.py writes on every compaction.

    context-guardian-recall <term> [--run RUN_ID]

A compaction folds older messages into a summary and archives the full text as
<GUARDIAN_SPAN_DIR>/<run_id>/<NNNN>.json. The summary the model receives names
this command so the model can find a detail again without reading a span whole
(a span is, by construction, larger than the room the compaction freed).

Case-insensitive substring match over each archived message (text, content
blocks, tool calls) and the span's own summary. One line per hit:
`<span file> msg <i> [<role>]: ...snippet...`. Exit 0 with hits, 1 without,
2 when the span directory does not exist. Stdlib only, Python 3.10+. MIT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SNIPPET_CHARS = 80
DEFAULT_MAX_HITS = 50


def span_dir() -> Path:
    """The proxy's own resolution: $GUARDIAN_SPAN_DIR (also from the .env beside
    this file, which the proxy loads too), else <here>/logs/guardian_spans."""
    try:
        from dotenv import load_dotenv
        load_dotenv(PACKAGE_ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover -- optional, never fatal
        pass
    return Path(os.environ.get("GUARDIAN_SPAN_DIR",
                               str(PACKAGE_ROOT / "logs" / "guardian_spans")))


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="context-guardian-recall",
        description="Search the spans Context Guardian archived when it compacted a conversation.")
    parser.add_argument("term", help="text to find (case-insensitive)")
    parser.add_argument("--run", metavar="RUN_ID", default=None,
                        help="only this proxy run (the directory name under the span dir)")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_HITS, metavar="N",
                        help="stop after N hits (default %(default)s)")
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = span_dir()
    if not root.is_dir():
        print(f"no span archive at {root} (set GUARDIAN_SPAN_DIR)", file=sys.stderr)
        return 2
    hits = 0
    for path, idx, role, text in search(root, ns.term, ns.run):
        label = "summary" if idx == "summary" else f"msg {idx} [{role}]"
        print(f"{path} {label}: {text}")
        hits += 1
        if hits >= ns.max:
            print(f"(stopped at {ns.max} hits; narrow the term or pass --run)")
            break
    if hits == 0:
        where = f"{root / ns.run}" if ns.run else str(root)
        print(f"no match for {ns.term!r} under {where}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
