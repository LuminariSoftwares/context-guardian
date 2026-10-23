"""Phase 5 acceptance: the Claude Code adapter (`context-guardian-claude-code`).

Claude Code talks to Anthropic's API directly, so the proxy cannot sit in
front of it -- but its hooks hand a command the session id and the transcript
path (https://code.claude.com/docs/en/hooks), and the transcript is a JSONL
file it keeps appending to (https://code.claude.com/docs/en/sessions). The
adapter copies the NEW lines into the archive as sealed spans under a
"cc-<session_id>" run. Driven as Claude Code drives it: a subprocess with the
hook JSON on stdin. Written before the code.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _child_env import child_env

import cg_recall_cli

ROOT = Path(__file__).resolve().parent.parent
SID = "0f8e7d6c-1111-2222-3333-444455556666"


def _lines(n, start=0):
    out = []
    for i in range(start, start + n):
        if i % 2 == 0:
            out.append({"type": "user", "uuid": "u%d" % i, "sessionId": SID,
                        "message": {"role": "user", "content": "question %d about the GEMSTONE-%d plan" % (i, i)}})
        else:
            out.append({"type": "assistant", "uuid": "a%d" % i, "sessionId": SID,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "answer %d" % i}]}})
    return [json.dumps(x) + "\n" for x in out]


@pytest.fixture()
def env(tmp_path):
    spans = tmp_path / "spans"
    return child_env(pythonpath=ROOT, GUARDIAN_SPAN_DIR=spans, GUARDIAN_DATA_DIR=tmp_path / "data",
                     GUARDIAN_LOG_PATH=tmp_path / "logs" / "context_guardian_log.json",
                     GUARDIAN_RECEIPTS_PATH=tmp_path / "receipts.jsonl")


def run(env, *args, stdin="", timeout=60):
    return subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), *args], input=stdin,
                          capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8")


def hook(transcript, event="PreCompact"):
    return json.dumps({"session_id": SID, "transcript_path": str(transcript), "cwd": str(transcript.parent),
                       "hook_event_name": event, "trigger": "auto", "custom_instructions": None})


def spans_of(tmp_path):
    run_dir = tmp_path / "spans" / ("cc-" + SID)
    return sorted(run_dir.glob("[0-9]*.json")), run_dir


def test_replaying_a_transcript_twice_writes_no_duplicate_spans(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(_lines(6)), encoding="utf-8")
    first = run(env, "capture", stdin=hook(transcript))
    assert first.returncode == 0, first.stderr
    files, _ = spans_of(tmp_path)
    assert len(files) == 1
    span = json.loads(files[0].read_text(encoding="utf-8"))
    assert span["message_count"] == 6 and span["source"] == "claude-code"
    assert [m["uuid"] for m in span["messages"]] == ["u0", "a1", "u2", "a3", "u4", "a5"]

    again = run(env, "capture", stdin=hook(transcript, "SessionEnd"))
    assert again.returncode == 0
    assert len(spans_of(tmp_path)[0]) == 1, "the same lines were archived twice"

    # New lines, plus a half-written one with no newline yet: only complete
    # lines are copied, and the partial one waits for the next capture.
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("".join(_lines(3, start=6)))
        fh.write('{"type": "user", "uuid": "partial"')
    assert run(env, "capture", stdin=hook(transcript)).returncode == 0
    files, _ = spans_of(tmp_path)
    assert len(files) == 2
    second = json.loads(files[1].read_text(encoding="utf-8"))
    assert [m["uuid"] for m in second["messages"]] == ["u6", "a7", "u8"]


def test_malformed_stdin_exits_0_with_a_log_line(env, tmp_path):
    for bad in ("", "not json", "[1, 2]", json.dumps({"session_id": "../../etc", "transcript_path": "x"}),
                json.dumps({"session_id": SID, "transcript_path": str(tmp_path / "missing.jsonl")})):
        res = run(env, "capture", stdin=bad)
        assert res.returncode == 0, (bad, res.stderr)
        assert "context-guardian" in res.stderr, (bad, res.stderr)
    log = tmp_path / "logs" / "claude_code_capture.log"
    assert log.exists() and len(log.read_text(encoding="utf-8").splitlines()) >= 5
    assert not (tmp_path / "etc").exists() and not list((tmp_path).glob("spans/cc-*"))


@pytest.mark.skipif(os.name == "nt", reason="a FIFO is how this test makes a read hang; POSIX only")
def test_capture_has_a_hard_timeout(env, tmp_path):
    fifo = tmp_path / "never.jsonl"
    os.mkfifo(fifo)
    started = time.monotonic()
    res = run(env, "capture", "--timeout", "0.5", stdin=hook(fifo), timeout=30)
    assert res.returncode == 0
    assert time.monotonic() - started < 10
    assert "timed out" in res.stderr


def test_capture_output_verifies_and_is_searchable(env, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(_lines(4)), encoding="utf-8")
    assert run(env, "capture", stdin=hook(transcript)).returncode == 0
    files, run_dir = spans_of(tmp_path)
    assert (run_dir / "chain.jsonl").exists() and (run_dir / "index.jsonl").exists()
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    assert cg_recall_cli.main(["--verify", "--run", "cc-" + SID]) == 0
    assert "OK" in capsys.readouterr().out
    assert cg_recall_cli.main(["GEMSTONE-2"]) == 0
    assert "cc-" + SID in capsys.readouterr().out
    index = json.loads((run_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert index["digest"].startswith("question 0") and index["digest_from"] == "first-user-message"


SETTINGS = {
    "model": "sonnet",
    "permissions": {"allow": ["Bash(npm test)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo checked"}]}],
        "PreCompact": [{"matcher": "manual", "hooks": [{"type": "command", "command": "my-own-script"}]}],
    },
}


def test_install_without_apply_changes_nothing(env, tmp_path):
    settings = tmp_path / "settings.json"
    original = json.dumps(SETTINGS, indent=2) + "\n"
    settings.write_text(original, encoding="utf-8")
    res = run(env, "install", "--settings", str(settings))
    assert res.returncode == 0, res.stderr
    assert settings.read_text(encoding="utf-8") == original
    assert "claude mcp add" in res.stdout and "PreCompact" in res.stdout and "SessionEnd" in res.stdout
    assert not list(tmp_path.glob("settings.json.bak-*"))


def test_install_apply_preserves_existing_hooks_byte_for_byte(env, tmp_path):
    settings = tmp_path / "settings.json"
    original = json.dumps(SETTINGS, indent=2) + "\n"
    settings.write_text(original, encoding="utf-8")
    res = run(env, "install", "--apply", "--settings", str(settings))
    assert res.returncode == 0, res.stderr
    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    assert "+" in res.stdout and "---" in res.stdout              # a diff was printed
    new_text = settings.read_text(encoding="utf-8")
    new = json.loads(new_text)
    # Every unrelated hook, and every other setting, is still there unchanged,
    # down to the text of its block.
    for block in ('"PreToolUse": [\n      {\n        "matcher": "Bash"',
                  json.dumps(SETTINGS["hooks"]["PreCompact"][0], indent=2).replace("\n", "\n      ")):
        assert block in new_text, block
    assert new["model"] == "sonnet" and new["permissions"] == SETTINGS["permissions"]
    assert new["hooks"]["PreToolUse"] == SETTINGS["hooks"]["PreToolUse"]
    assert new["hooks"]["PreCompact"][0] == SETTINGS["hooks"]["PreCompact"][0]
    ours = [h for g in new["hooks"]["PreCompact"] + new["hooks"]["SessionEnd"] for h in g["hooks"]
            if "capture" in h["command"]]
    assert len(ours) == 2 and all(h["type"] == "command" and h["timeout"] >= 10 for h in ours)

    # Idempotent: a second install adds nothing.
    assert run(env, "install", "--apply", "--settings", str(settings)).returncode == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == new

    # And uninstall gives back the original file, byte for byte.
    res = run(env, "uninstall", "--apply", "--settings", str(settings))
    assert res.returncode == 0, res.stderr
    assert settings.read_text(encoding="utf-8") == original


def test_install_refuses_a_settings_file_it_cannot_parse(env, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")
    res = run(env, "install", "--apply", "--settings", str(settings))
    assert res.returncode != 0
    assert settings.read_text(encoding="utf-8") == "{ not json"


def test_status_reports_hooks_sessions_and_seal(env, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(_lines(2)), encoding="utf-8")
    run(env, "capture", stdin=hook(transcript))
    before = run(env, "status", "--settings", str(settings)).stdout
    assert "not installed" in before and "1 session" in before
    run(env, "install", "--apply", "--settings", str(settings))
    after = run(env, "status", "--settings", str(settings)).stdout
    assert "installed" in after and "not installed" not in after
    assert "verify OK" in after and "last capture" in after


def test_the_console_script_is_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'context-guardian-claude-code = "cg_claude_code:main"' in text
    assert '"cg_claude_code"' in text


def test_transcript_roles_and_a_digest_without_user_text(env, tmp_path, monkeypatch, capsys):
    """Found replaying a real transcript: search printed `[?]` for every role
    (transcript lines carry it inside "message"), and a span whose user lines
    were all tool results got an empty digest."""
    transcript = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Ran the ORCHID-5 migration. All green."}]}}]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert run(env, "capture", stdin=hook(transcript)).returncode == 0
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    assert cg_recall_cli.main(["ORCHID-5"]) == 0
    assert "msg 1 [assistant]" in capsys.readouterr().out
    _, run_dir = spans_of(tmp_path)
    index = json.loads((run_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert index["digest"] == "Ran the ORCHID-5 migration." and index["digest_from"] == "first-message"
