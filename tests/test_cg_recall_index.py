"""cg_recall_index: a derived FTS5 index over the spans; the spans stay the source of truth."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cg_recall_cli  # noqa: E402
import cg_recall_index  # noqa: E402

TEXTS = {"s-alpha": "EXAMPLE-ONLY the compaction happened after the long build finished",
         "s-beta": "EXAMPLE-ONLY the csv exporter writes rows with a header line",
         "s-gamma": "EXAMPLE-ONLY unrelated talk about the weather and lunch plans"}


def _capture(tmp_path, spans, sid, text):
    t = tmp_path / (sid + ".jsonl")
    with open(t, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    hook = {"session_id": sid, "transcript_path": str(t), "cwd": str(tmp_path), "hook_event_name": "PreCompact"}
    env = dict(os.environ, PYTHONPATH=str(ROOT), GUARDIAN_SPAN_DIR=str(spans))
    return subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), "capture"], input=json.dumps(hook),
                          capture_output=True, encoding="utf-8", timeout=120, env=env, cwd=str(tmp_path))


@pytest.fixture
def spans(tmp_path):
    root = tmp_path / "spans"
    for sid, text in TEXTS.items():
        _capture(tmp_path, root, sid, text)
    return root


def _cli(tmp_path, spans, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT), GUARDIAN_SPAN_DIR=str(spans))
    return subprocess.run([sys.executable, str(ROOT / "cg_recall_cli.py"), *args], capture_output=True,
                          encoding="utf-8", timeout=120, env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL)


def test_archive_builds_the_index_and_search_ranks(spans):
    assert cg_recall_index.index_path(spans) == spans / "recall.sqlite"
    assert (spans / "recall.sqlite").is_file()
    assert cg_recall_index.search(spans, "csv exporter")[0][0] == "cc-s-beta"
    assert cg_recall_index.search(spans, "weather lunch")[0][0] == "cc-s-gamma"


def test_substring_typo_and_punctuation(spans):
    assert cg_recall_index.search(spans, "xporte")[0][0] == "cc-s-beta"
    assert cg_recall_index.search(spans, "compation")[0][0] == "cc-s-alpha"
    assert isinstance(cg_recall_index.search(spans, '"( AND * -x'), list)
    assert cg_recall_index.search(spans, "zzzzqqqq") == []


def test_missing_or_empty_index_returns_nothing(tmp_path):
    assert cg_recall_index.search(tmp_path, "anything") == []
    (tmp_path / "recall.sqlite").write_bytes(b"not a database")
    assert cg_recall_index.search(tmp_path, "anything") == []


def test_failed_swap_leaves_old_index_and_no_tmp(spans, monkeypatch):
    cg_recall_index.reindex(spans)
    idx = spans / "recall.sqlite"
    before = idx.read_bytes()

    def boom(*a, **k):
        raise OSError("simulated")
    monkeypatch.setattr(cg_recall_index.os, "replace", boom)
    with pytest.raises(OSError):
        cg_recall_index.reindex(spans)
    assert idx.read_bytes() == before
    assert not (spans / "recall.sqlite.tmp").exists()


def test_encrypted_spans_are_not_indexed(tmp_path):
    run = tmp_path / "r1"
    run.mkdir()
    (run / "0001.json").write_text(json.dumps({"format": 2, "locked": True, "messages": []}), encoding="utf-8")
    assert cg_recall_index.reindex(tmp_path) == 0


def test_cli_search_and_reindex_flags(spans, tmp_path):
    (spans / "recall.sqlite").unlink()
    r = _cli(tmp_path, spans, "--reindex")
    assert r.returncode == 0 and (spans / "recall.sqlite").is_file()
    r = _cli(tmp_path, spans, "--search", "csv exporter")
    assert r.returncode == 0 and "cc-s-beta" in r.stdout


def test_ranked_search_uses_a_fresh_index_and_ignores_a_stale_one(spans):
    hits = list(cg_recall_cli.ranked_search(spans, "compation"))
    assert hits and hits[0][0].parent.name == "cc-s-alpha"  # found through the typo fallback
    (spans / "recall.sqlite").unlink()
    assert list(cg_recall_cli.ranked_search(spans, "compation")) == []  # plain path unchanged
    assert list(cg_recall_cli.ranked_search(spans, "csv exporter"))[0][0].parent.name == "cc-s-beta"


def test_selftest_runs_green():
    r = subprocess.run([sys.executable, str(ROOT / "cg_recall_index.py"), "--selftest"], capture_output=True,
                       encoding="utf-8", timeout=120, cwd=str(ROOT), stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and " 0 failed" in r.stdout
