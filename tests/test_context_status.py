"""get_context_status: read-only context-window fullness. Fixtures are inert EXAMPLE-ONLY text built in tmp_path."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_mcp  # noqa: E402
import context_guardian  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    for k in ("CG_CC_WINDOW", "GUARDIAN_CHARS_PER_TOKEN", "GUARDIAN_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


def transcript(tmp_path, nbytes, model="claude-test-model", name="t.jsonl"):
    p = tmp_path / name
    head = json.dumps({"type": "assistant", "message": {"role": "assistant", "model": model,
                                                         "content": [{"type": "text", "text": "EXAMPLE-ONLY"}]}}) + "\n"
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 900}}) + "\n"
    body = head
    while len(body) + len(line) <= nbytes:
        body += line
    body += "y" * (nbytes - len(body) - 1) + "\n"
    p.write_bytes(body.encode())
    return p


def call(args):
    r = cg_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_context_status", "arguments": args}})["result"]
    assert r["isError"] is False
    assert json.loads(r["content"][0]["text"]) == r["structuredContent"]
    return r["structuredContent"]


def test_listed_read_only():
    tool = next(t for t in cg_mcp.TOOLS if t["name"] == "get_context_status")
    assert tool["annotations"]["readOnlyHint"] is True


def test_states(tmp_path, monkeypatch):
    monkeypatch.setenv("CG_CC_WINDOW", "20000")
    assert call({"transcript_path": str(transcript(tmp_path, 7000))})["state"] == "ok"
    assert call({"transcript_path": str(transcript(tmp_path, 56000))})["state"] == "high"
    d = call({"transcript_path": str(transcript(tmp_path, 70000))})
    assert d["state"] == "critical" and d["window_source"] == "env" and d["since_compaction_bytes"] == 70000


def test_windows(tmp_path, monkeypatch):
    p = str(transcript(tmp_path, 7000, model="some-unlisted-model"))
    assert call({"transcript_path": p})["window_source"] == "default"
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "4096")
    assert call({"transcript_path": p})["window"] == 200000
    big = str(transcript(tmp_path, 7000, model="claude-x[1m]", name="b.jsonl"))
    d = call({"transcript_path": big})
    assert d["window"] == 1_000_000 and d["window_source"] == "model"
    monkeypatch.setenv("CG_CC_WINDOW", "12345")
    assert call({"transcript_path": big})["window"] == 12345


def test_ratio_agrees_with_the_proxy(tmp_path):
    p = transcript(tmp_path, 8000)
    code = "import context_guardian as c, sys; print(c.CHARS_PER_TOKEN_ESTIMATE)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", cwd=ROOT,
                         env=dict(os.environ, GUARDIAN_CHARS_PER_TOKEN="4")).stdout
    os.environ["GUARDIAN_CHARS_PER_TOKEN"] = "4"
    try:
        d = call({"transcript_path": str(p)})
    finally:
        del os.environ["GUARDIAN_CHARS_PER_TOKEN"]
    assert d["ratio"] == float(out) == 4.0 and d["used_tokens_est"] == 2000
    assert context_guardian.CHARS_PER_TOKEN_ESTIMATE == 3.5


def test_unknown_never_errors(tmp_path):
    d = call({"transcript_path": str(tmp_path / "nope.jsonl")})
    assert d["state"] == "unknown" and d["reason"]
    d = call({})
    assert d["state"] == "unknown" and d["reason"]
    assert call({"transcript_path": 5})["state"] == "unknown"


def test_boundary_and_default_run_use_the_precompact_span(tmp_path):
    import cg_claude_code
    t = transcript(tmp_path, 30000)
    hook = {"session_id": "s6-cmp", "transcript_path": str(t), "cwd": str(tmp_path), "hook_event_name": "PreCompact"}
    root = tmp_path / "spans"
    root.mkdir()
    import time
    cg_claude_code.capture(hook, root, time.monotonic() + 30)
    extra = json.dumps({"type": "user", "message": {"role": "user", "content": "z" * 500}}) + "\n"
    with open(t, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(extra)
    d = call({})
    assert d["since_compaction_bytes"] == len(extra.encode())
    assert call({"transcript_path": str(t)})["since_compaction_bytes"] == len(extra.encode())
