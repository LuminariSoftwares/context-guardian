#!/usr/bin/env python3
"""cg_recall_index.py -- a derived full-text index over the span archive.

    python cg_recall_index.py --reindex          rebuild <span dir>/recall.sqlite
    python cg_recall_index.py --search "<query>" print `<run> <span> <score>` per hit
    python cg_recall_index.py --selftest         run the built-in checks

The spans stay the source of truth: recall.sqlite is rebuilt from them and can
be deleted at any time. Two SQLite FTS5 tables hold the same rows (run, name,
heading, summary, text): spans_words (porter + unicode61, finds word forms)
and spans_tri (trigram, finds substrings). A search runs the query on both,
fuses the two top-50 lists by reciprocal rank (k=60), and, when both come back
empty, corrects each unknown word to its closest word in the index vocabulary
(difflib) and runs the same plain MATCH again.

Encrypted spans (format 2) are never indexed: the index is clear text on disk.

Rewritten from the idea (FTS5 + trigram with rank fusion and a typo fallback)
in context-mode (mksglu/context-mode, Elastic License 2.0); ideas only, no
code was copied. Stdlib only, Python 3.10+. MIT.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import cg_archive

INDEX_NAME = "recall.sqlite"
RRF_K = 60
TOP_N = 50
HEADING_CHARS = 120
WEIGHTS = "0, 0, 3, 3, 1"  # run, name, heading, summary, text: bm25() weights, lower score = better
TYPO_CUTOFF = 0.7


def index_path(root) -> Path:
    return Path(root) / INDEX_NAME


def _span_rows(root: Path):
    for run in cg_archive.run_dirs(root):
        for path in sorted(run.glob("*.json")):
            if not cg_archive.SPAN_NAME_RE.match(path.name):
                continue
            data = cg_archive.load_span(path)
            if data is None or data.get("locked") or data.get("format") == cg_archive.ENCRYPT_FORMAT:
                continue
            texts = [cg_archive.message_text(m) for m in data.get("messages") or []]
            body = "\n".join(t for t in texts if t)
            heading = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")[:HEADING_CHARS]
            summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
            yield run.name, path.name, heading, summary, body


def reindex(root) -> int:
    """Rebuild recall.sqlite from the spans under root; returns spans indexed.
    Written to recall.sqlite.tmp and swapped in with os.replace, so a half-built
    index never exists; if the swap fails the old file is untouched."""
    root = Path(root)
    final = index_path(root)
    tmp = final.with_name(INDEX_NAME + ".tmp")
    root.mkdir(parents=True, exist_ok=True)
    for stale in (tmp, tmp.with_name(tmp.name + "-journal")):
        stale.unlink(missing_ok=True)
    count = 0
    try:
        con = sqlite3.connect(str(tmp))
        try:
            cols = "run UNINDEXED, name UNINDEXED, heading, summary, text"
            con.execute(f"CREATE VIRTUAL TABLE spans_words USING fts5({cols}, tokenize='porter unicode61')")
            con.execute(f"CREATE VIRTUAL TABLE spans_tri USING fts5({cols}, tokenize='trigram')")
            con.execute("CREATE VIRTUAL TABLE spans_vocab USING fts5vocab(spans_words, 'row')")
            for row in _span_rows(root):
                con.execute("INSERT INTO spans_words VALUES (?,?,?,?,?)", row)
                con.execute("INSERT INTO spans_tri VALUES (?,?,?,?,?)", row)
                count += 1
            con.commit()
        finally:
            con.close()
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        tmp.with_name(tmp.name + "-journal").unlink(missing_ok=True)
        raise
    return count


def _quote(words: List[str]) -> str:
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


_TABLES = ("spans_words", "spans_tri")


def _top(con, table: str, match: str) -> List[Tuple[str, str]]:
    if table not in _TABLES:   # identifiers come only from this fixed list; the user's text is the bound `?`
        raise ValueError("unknown index table %r" % table)
    try:
        rows = con.execute(
            f"SELECT run, name FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table}, {WEIGHTS}) LIMIT {TOP_N}",  # nosec B608
            (match,)).fetchall()
    except sqlite3.Error:
        return []
    return [(r[0], r[1]) for r in rows]


def _fuse(con, words: List[str]) -> List[Tuple[str, str, float]]:
    match = _quote(words)
    scores: dict = {}
    for table in ("spans_words", "spans_tri"):
        for rank, key in enumerate(_top(con, table, match), 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(run, name, score) for (run, name), score in ordered]


def _vocab(con) -> List[str]:
    try:
        return [r[0] for r in con.execute("SELECT term FROM spans_vocab")]
    except sqlite3.Error:
        return []


def search(root, query: str, k: int = 20) -> List[Tuple[str, str, float]]:
    """[(run, span file name, score)] best first, at most k. Empty or missing
    index, empty query, or any SQLite error returns []."""
    words = [w for w in str(query).lower().split() if w]
    path = index_path(root)
    if not words or not path.is_file():
        return []
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        hits = _fuse(con, words)
        if not hits:
            vocab = _vocab(con)
            known = set(vocab)
            fixed = []
            for w in words:
                if w in known or not vocab:
                    fixed.append(w)
                    continue
                close = difflib.get_close_matches(w, vocab, n=1, cutoff=TYPO_CUTOFF)
                fixed.append(close[0] if close else w)
            if fixed != words:
                hits = _fuse(con, fixed)
        return hits[:max(0, int(k))]
    except sqlite3.Error:
        return []
    finally:
        con.close()


# --- selftest ----------------------------------------------------------------------

def _fixture(root: Path) -> None:
    import json
    texts = {"r1": "EXAMPLE-ONLY the compaction happened after the build",
             "r2": "EXAMPLE-ONLY the csv exporter writes a header"}
    for run, text in texts.items():
        d = root / run
        d.mkdir(parents=True)
        (d / "0001.json").write_text(json.dumps({"summary": "", "messages": [{"role": "user", "content": text}]}),
                                     encoding="utf-8")


def _t_index_path():
    return index_path("x") == Path("x") / INDEX_NAME


def _t_missing_index_empty():
    with tempfile.TemporaryDirectory() as d:
        return search(Path(d), "anything") == []


def _t_reindex_and_search():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fixture(root)
        return reindex(root) == 2 and search(root, "csv exporter")[0][:2] == ("r2", "0001.json")


def _t_trigram_substring():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fixture(root)
        reindex(root)
        return search(root, "xporte")[0][0] == "r2"


def _t_typo_fallback():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fixture(root)
        reindex(root)
        return search(root, "compation")[0][0] == "r1"


def _t_punctuation_safe():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fixture(root)
        reindex(root)
        return isinstance(search(root, 'a "b (c* AND -d'), list)


def _t_failed_swap_keeps_old():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fixture(root)
        reindex(root)
        before = index_path(root).read_bytes()
        real = os.replace

        def boom(*a, **k):
            raise OSError("simulated")
        os.replace = boom
        try:
            reindex(root)
            return False
        except OSError:
            pass
        finally:
            os.replace = real
        tmp = index_path(root).with_name(INDEX_NAME + ".tmp")
        return index_path(root).read_bytes() == before and not tmp.exists()


CHECKS = [("index_path", _t_index_path), ("missing_index_empty", _t_missing_index_empty),
          ("reindex_and_search", _t_reindex_and_search), ("trigram_substring", _t_trigram_substring),
          ("typo_fallback", _t_typo_fallback), ("punctuation_safe", _t_punctuation_safe),
          ("failed_swap_keeps_old", _t_failed_swap_keeps_old)]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  error", name, type(exc).__name__, str(exc)[:120])
        passed += ok
        print(("PASS " if ok else "FAIL ") + name)
    print("cg_recall_index selftest: %d checks, %d passed, %d failed" % (len(CHECKS), passed, len(CHECKS) - passed))
    return 0 if passed == len(CHECKS) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cg_recall_index", description="Derived FTS5 recall index over the span archive.")
    ap.add_argument("--reindex", action="store_true", help="rebuild recall.sqlite from the spans")
    ap.add_argument("--search", metavar="QUERY", help="print run, span and score per hit")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks")
    ns = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if ns.selftest:
        return selftest()
    import cg_recall_cli
    root = cg_recall_cli.span_dir()
    if ns.reindex:
        print("indexed %d span(s) into %s" % (reindex(root), index_path(root)))
        return 0
    if ns.search:
        hits = search(root, ns.search)
        for run, name, score in hits:
            print("%s %s %.4f" % (run, name, score))
        return 0 if hits else 1
    ap.error("give --reindex, --search or --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
