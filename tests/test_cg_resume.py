"""P141 S5: the resume block put back into Claude Code after a compaction.

Fixtures are inert (sentinel EXAMPLE-ONLY, paths under /home/user/project) and
are built with the repo's own capture(): a transcript is written in tmp_path
and PreCompact payloads are fed to it, never hand-written span files.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _child_env import child_env

import cg_claude_code as cc
import cg_resume

ROOT = Path(__file__).resolve().parent.parent
SID = "resume-test-session"


def _u(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _a(text, tool=None, path=None):
    blocks = [{"type": "text", "text": text}]
    if tool:
        blocks.append({"type": "tool_use", "id": "t1", "name": tool, "input": {"file_path": path}})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _r(text):
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def archive(tmp_path, batches, sid=SID):
    """Write batches of transcript lines and capture after each one."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    root = tmp_path / "spans"
    for batch in batches:
        with open(transcript, "a", encoding="utf-8", newline="\n") as fh:
            for obj in batch:
                fh.write(json.dumps(obj) + "\n")
        cc.capture({"session_id": sid, "transcript_path": str(transcript),
                    "hook_event_name": "PreCompact"}, root, time.monotonic() + 30)
    return root


BATCHES = [
    [_u("EXAMPLE-ONLY: build the sample exporter."),
     _a("We decided to go with CSV output.", "Edit", "/home/user/project/exporter.py"),
     _r("Traceback (most recent call last): FAILED tests/test_export.py")],
    [_a("Agreed: keep UTF-8.", "Write", "/home/user/project/tests/test_export.py"), _r("1 passed")],
    [_a("goal: add a README note", "MultiEdit", "/home/user/project/README.md"),
     _r("FAILED tests/test_readme.py - assert 0 == 1")],
]


@pytest.fixture()
def run_dir(tmp_path):
    return archive(tmp_path, BATCHES) / ("cc-" + SID)


def section(text, name, nxt):
    start = text.index(name)
    return text[start:text.index(nxt, start)] if nxt else text[start:]


def test_sections_in_order_and_bounded(run_dir):
    out = cg_resume.build_resume(run_dir)
    pos = [out.index(h) for h in ("Goal:", "Decisions:", "Files edited:", "Open errors:", "Recall:")]
    assert pos == sorted(pos) and len(out) <= 8000
    assert 'context-guardian-recall "' in out


def test_goal_and_later_goal_lines(run_dir):
    goal = section(cg_resume.build_resume(run_dir), "Goal:", "Decisions:")
    assert "sample exporter" in goal and "add a README note" in goal


def test_goal_is_cut_at_300_chars(tmp_path):
    root = archive(tmp_path, [[_u("EXAMPLE-ONLY " + "x" * 1000)]])
    goal = section(cg_resume.build_resume(root / ("cc-" + SID)), "Goal:", "Decisions:")
    assert goal.count("x") <= 300


def test_decisions_most_recent_eight(tmp_path):
    lines = [_u("EXAMPLE-ONLY goal")] + [_a("decision number %d" % i) for i in range(12)]
    out = cg_resume.build_resume(archive(tmp_path, [lines]) / ("cc-" + SID))
    dec = section(out, "Decisions:", "Files edited:")
    assert "decision number 11" in dec and "decision number 4" in dec
    assert "decision number 3" not in dec


def test_files_newest_first_and_distinct(tmp_path):
    lines = [_u("EXAMPLE-ONLY")] + [_a("e", "Edit", "/home/user/project/f%d.py" % i) for i in range(25)]
    lines.append(_a("again", "Write", "/home/user/project/f0.py"))
    out = cg_resume.build_resume(archive(tmp_path, [lines]) / ("cc-" + SID))
    files = section(out, "Files edited:", "Open errors:")
    assert files.count("/home/user/project/") == 20
    assert files.index("f0.py") < files.index("f24.py")


def test_open_errors_cleared_by_later_pass(run_dir):
    errors = section(cg_resume.build_resume(run_dir), "Open errors:", "Recall:")
    assert "test_readme.py" in errors and "test_export.py" not in errors


def test_pass_for_same_path_clears_only_that_path(tmp_path):
    lines = [_u("EXAMPLE-ONLY"), _r("FAILED tests/test_a.py\nFAILED tests/test_b.py"), _r("tests/test_a.py ok")]
    errors = section(cg_resume.build_resume(archive(tmp_path, [lines]) / ("cc-" + SID)), "Open errors:", "Recall:")
    assert "test_b.py" in errors and "test_a.py" not in errors


def test_open_errors_capped_at_five(tmp_path):
    lines = [_u("EXAMPLE-ONLY")] + [_r("error %d in step" % i) for i in range(9)]
    errors = section(cg_resume.build_resume(archive(tmp_path, [lines]) / ("cc-" + SID)), "Open errors:", "Recall:")
    assert errors.count("error ") == 5 and "error 8" in errors


@pytest.mark.parametrize("limit", [400, 1000, 2500])
def test_max_chars_respected(tmp_path, limit):
    lines = [_u("EXAMPLE-ONLY " + "g" * 500)] + [_a("decided %s" % ("d" * 200), "Edit", "/home/user/project/%d.py" % i)
                                                  for i in range(30)]
    out = cg_resume.build_resume(archive(tmp_path, [lines]) / ("cc-" + SID), max_chars=limit)
    assert 0 < len(out) <= limit


def test_empty_or_missing_run_is_empty(tmp_path):
    (tmp_path / "empty").mkdir()
    assert cg_resume.build_resume(tmp_path / "empty") == ""
    assert cg_resume.build_resume(tmp_path / "missing") == ""


def test_our_hooks_matcher_only_on_session_start():
    hooks = cc.our_hooks("x capture")
    assert cc.EVENTS == ("PreCompact", "SessionEnd", "SessionStart")
    assert hooks["SessionStart"]["matcher"] == "compact"
    assert "matcher" not in hooks["PreCompact"] and "matcher" not in hooks["SessionEnd"]


def test_install_writes_third_event_to_tmp_settings(tmp_path):
    settings = tmp_path / "settings-copy.json"
    env = child_env(GUARDIAN_SPAN_DIR=str(tmp_path / "spans"))
    res = subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), "install", "--settings", str(settings),
                          "--apply"], capture_output=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    assert res.returncode == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["matcher"] == "compact"


def _hook(tmp_path, root, **payload):
    env = child_env(GUARDIAN_SPAN_DIR=str(root))
    body = {"session_id": SID, "transcript_path": str(tmp_path / "transcript.jsonl"),
            "hook_event_name": "SessionStart"}
    body.update(payload)
    return subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), "capture"], input=json.dumps(body),
                          capture_output=True, encoding="utf-8", env=env, cwd=str(tmp_path), timeout=60)


def test_session_start_compact_prints_one_json_line(tmp_path):
    root = archive(tmp_path, BATCHES)
    res = _hook(tmp_path, root, source="compact")
    assert res.returncode == 0
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    out = json.loads(lines[0])["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart" and "Goal:" in out["additionalContext"]


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_session_start_other_sources_are_silent_and_archive_nothing(tmp_path, source):
    root = archive(tmp_path, BATCHES)
    before = sorted(os.listdir(root / ("cc-" + SID)))
    with open(tmp_path / "transcript.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_u("EXAMPLE-ONLY later line")) + "\n")
    res = _hook(tmp_path, root, source=source)
    assert res.returncode == 0 and res.stdout.strip() == ""
    assert sorted(os.listdir(root / ("cc-" + SID))) == before


def test_session_start_compact_never_archives(tmp_path):
    root = archive(tmp_path, BATCHES)
    before = sorted(os.listdir(root / ("cc-" + SID)))
    with open(tmp_path / "transcript.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_u("EXAMPLE-ONLY later line")) + "\n")
    _hook(tmp_path, root, source="compact")
    assert sorted(os.listdir(root / ("cc-" + SID))) == before


def test_bad_session_id_and_unknown_session_are_silent(tmp_path):
    root = archive(tmp_path, BATCHES)
    for sid in ("../bad", "never-seen"):
        res = _hook(tmp_path, root, source="compact", session_id=sid)
        assert res.returncode == 0 and res.stdout.strip() == ""


def test_resume_failure_logs_and_prints_nothing(tmp_path, monkeypatch, capsys):
    def boom(_run_dir):
        raise RuntimeError("boom")
    monkeypatch.setattr(cg_resume, "build_resume", boom)
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "logs" / "x.log"))
    cc._resume({"session_id": SID, "source": "compact"}, tmp_path)
    assert capsys.readouterr().out == ""
    assert "resume block failed" in (tmp_path / "logs" / "claude_code_capture.log").read_text(encoding="utf-8")
