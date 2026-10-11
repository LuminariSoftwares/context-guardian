"""`context-guardian-recall`: the search command the compaction summary names.

The summary told the model to run `python scripts/guardian_recall.py "<term>"`.
No such file exists in this repo or in the wheel, so the one recovery path the
summary offered failed for every user. These tests pin the replacement: a
console script declared in pyproject.toml, searching the span files under
GUARDIAN_SPAN_DIR, and a summary that names it.
"""
import importlib
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))


def _pyproject():
    return (REPO_DIR / "pyproject.toml").read_text(encoding="utf-8")


def _recall_cli():
    if "cg_recall_cli" in sys.modules:
        return importlib.reload(sys.modules["cg_recall_cli"])
    return importlib.import_module("cg_recall_cli")


def _span(root, run_id, index, messages, summary="s"):
    d = Path(root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%04d.json" % index)
    p.write_text(json.dumps({"run_id": run_id, "index": index, "summary": summary,
                             "messages": messages}), encoding="utf-8")
    return p


def test_pyproject_declares_the_console_script():
    text = _pyproject()
    assert re.search(r'^context-guardian-recall\s*=\s*"cg_recall_cli:main"\s*$', text, re.M), (
        "[project.scripts] has no context-guardian-recall entry")
    modules = re.search(r"^py-modules\s*=\s*\[(.*?)\]", text, re.M | re.S).group(1)
    assert '"cg_recall_cli"' in modules, "the module is not shipped in the wheel"


def test_recall_finds_a_term_across_runs(tmp_path, monkeypatch, capsys):
    root = tmp_path / "spans"
    a = _span(root, "20260101-000000", 1, [{"role": "user", "content": "set retry_limit to 7 in app.toml"}])
    b = _span(root, "20260102-000000", 1, [
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "edit", "arguments": "{\"path\": \"Retry_Limit.md\"}"}}]}])
    _span(root, "20260102-000000", 2, [{"role": "user", "content": "unrelated"}])
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(root))

    assert _recall_cli().main(["retry_limit"]) == 0
    out = capsys.readouterr().out
    assert str(a) in out and str(b) in out, out
    assert "set retry_limit to 7" in out
    assert "unrelated" not in out


def test_run_filter_limits_the_search(tmp_path, monkeypatch, capsys):
    root = tmp_path / "spans"
    _span(root, "20260101-000000", 1, [{"role": "user", "content": "needle one"}])
    keep = _span(root, "20260102-000000", 1, [{"role": "user", "content": "needle two"}])
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(root))

    assert _recall_cli().main(["needle", "--run", "20260102-000000"]) == 0
    out = capsys.readouterr().out
    assert str(keep) in out and "needle two" in out
    assert "needle one" not in out


def test_no_hit_and_missing_dir_exit_non_zero(tmp_path, monkeypatch, capsys):
    root = tmp_path / "spans"
    _span(root, "r", 1, [{"role": "user", "content": "hay"}])
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(root))
    assert _recall_cli().main(["needle"]) == 1
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "nope"))
    assert _recall_cli().main(["needle"]) == 2
    capsys.readouterr()


def test_a_corrupt_span_is_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    root = tmp_path / "spans"
    (root / "r").mkdir(parents=True)
    (root / "r" / "0001.json").write_text("{not json", encoding="utf-8")
    good = _span(root, "r", 2, [{"role": "user", "content": "needle"}])
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(root))
    assert _recall_cli().main(["needle"]) == 0
    assert str(good) in capsys.readouterr().out


def test_the_source_names_no_missing_script():
    src = (REPO_DIR / "context_guardian.py").read_text(encoding="utf-8")
    assert "guardian_recall.py" not in src


@pytest.mark.asyncio
async def test_the_command_in_the_summary_finds_the_evicted_text(monkeypatch, tmp_path, capsys):
    """End to end: compact, read the command out of the summary the model
    receives, run it, and find the evicted text."""
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "1000")
    monkeypatch.setenv("GUARDIAN_COMPACT_THRESHOLD", "0.5")
    monkeypatch.setenv("GUARDIAN_KEEP_RECENT_MESSAGES", "2")
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    cg = (importlib.reload(sys.modules["context_guardian"])
          if "context_guardian" in sys.modules
          else importlib.import_module("context_guardian"))

    async def _fake_summarize(client, model, older_messages):
        return "condensed summary of the older turns, long enough to be accepted"
    monkeypatch.setattr(cg, "summarize_older_messages", _fake_summarize)

    messages = [{"role": "user", "content": "ZEBRA-4417 " + "x" * 4000}]
    messages += [{"role": "user", "content": "x" * 4000} for _ in range(4)]
    result = await cg.maybe_compact(client=object(), payload={"model": "m", "messages": messages})

    summary = result["messages"][0]["content"]
    m = re.search(r"`(context-guardian-recall [^`]+)`", summary)
    assert m, "the summary names no context-guardian-recall command: %r" % summary[-400:]
    argv = shlex.split(m.group(1).replace("<term>", "ZEBRA-4417"))[1:]
    assert "--run" in argv and cg.RUN_ID in argv

    assert _recall_cli().main(argv) == 0
    assert "ZEBRA-4417" in capsys.readouterr().out
