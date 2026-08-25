"""Tests for 0.3.0: span archiving, bounded summaries, and env-tunable timeouts.

WHY THIS FILE EXISTS
    These behaviours shipped in the copy that actually ran and were covered by
    `scripts/test_guardian_partition.py`, a standalone script with ZERO test
    functions -- every assertion was inline in a `main()` nobody invoked from
    CI. So the feature the whole recall design rests on had no enforced test.

    The 6-round partition loop below is that script's logic, kept because it is
    good, rewritten so pytest actually runs it.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKER = "[Context Guardian auto-compaction"


@pytest.fixture()
def guardian(monkeypatch, tmp_path):
    """Config is read at import time, so every env var must be set BEFORE the
    import and the module must be reloaded per test."""
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "1000")
    monkeypatch.setenv("GUARDIAN_COMPACT_THRESHOLD", "0.5")
    monkeypatch.setenv("GUARDIAN_KEEP_RECENT_MESSAGES", "4")
    monkeypatch.setenv("GUARDIAN_KEEP_SUMMARIES", "1")
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "guardian_log.json"))
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


def summary_msg(n):
    return {"role": "system", "content": "%s - round %d] gist %d" % (MARKER, n, n)}


# --- classification ---------------------------------------------------------

def test_a_guardian_summary_is_recognised(guardian):
    assert guardian.is_guardian_summary(summary_msg(1))


def test_a_real_system_prompt_is_not_a_summary(guardian):
    assert not guardian.is_guardian_summary(
        {"role": "system", "content": "You are a coding assistant."})


def test_a_user_message_quoting_the_marker_is_not_a_summary(guardian):
    """Role matters. A user pasting Guardian's own output must not be able to
    get their message retired as if Guardian had written it."""
    assert not guardian.is_guardian_summary(
        {"role": "user", "content": MARKER + " - round 1] gist"})


# --- partition, over repeated compactions -----------------------------------

def test_summaries_stay_bounded_and_nothing_is_dropped(guardian):
    """Six rounds. The accumulation bug this guards against only appears after
    several compactions in one session, which is why it survived so long."""
    keep_summaries = guardian.KEEP_SUMMARIES
    real_system = {"role": "system", "content": "You are a coding assistant."}
    messages = [real_system]
    for i in range(8):
        messages.append({"role": "user", "content": "turn %d question" % i})
        messages.append({"role": "assistant", "content": "turn %d answer" % i})

    retired_total = 0
    for rnd in range(1, 7):
        part = guardian.partition_messages(messages)

        # nothing vanishes: every retired summary is inside the span being written
        for r in part["retired_summaries"]:
            assert r in part["to_summarize"], (
                "round %d retired a summary that is not in the span -- that is "
                "silent data loss, not compaction" % rnd)
        retired_total += len(part["retired_summaries"])

        # the client's own system prompt is never retired or misread
        assert real_system in part["real_system"]
        assert not any(guardian.is_guardian_summary(m) for m in part["real_system"])

        new_summary = summary_msg(rnd)
        messages = (part["real_system"] + part["kept_summaries"]
                    + [new_summary] + part["to_keep"])

        in_window = [m for m in messages if guardian.is_guardian_summary(m)]
        assert len(in_window) <= keep_summaries + 1, (
            "round %d left %d summaries in the window (cap %d). Summaries that "
            "are never themselves compacted crowd out the conversation they "
            "exist to make room for." % (rnd, len(in_window), keep_summaries + 1))
        assert new_summary in messages

        messages.append({"role": "user", "content": "new question after %d" % rnd})
        messages.append({"role": "assistant", "content": "new answer after %d" % rnd})

    assert retired_total > 0, (
        "six rounds retired nothing -- this test would pass on a partition "
        "function that does nothing at all")
    assert any(m.get("content") == "new answer after 6" for m in messages)


# --- the span archive itself ------------------------------------------------

def test_write_span_puts_the_messages_on_disk(guardian, tmp_path):
    msgs = [{"role": "user", "content": "the detail the summary will drop"}]
    path = guardian.write_span(msgs, "a summary", 1)
    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["messages"] == msgs
    assert data["summary"] == "a summary"
    assert data["index"] == 1
    assert data["run_id"] == guardian.RUN_ID


def test_write_span_leaves_no_partial_file(guardian):
    """It writes .part then os.replace. A leftover .part means a reader can see
    a half-written span and treat it as the archive."""
    guardian.write_span([{"role": "user", "content": "x"}], "s", 2)
    assert list(Path(guardian.SPAN_DIR).glob("*/*.part")) == []


def test_write_span_returns_none_instead_of_raising(guardian, monkeypatch):
    """Called on the request path. Losing the archive is bad; losing the user's
    request is worse -- so a failure must be reported, never raised."""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(guardian.json, "dump", boom)
    assert guardian.write_span([{"role": "user", "content": "x"}], "s", 3) is None


def test_prune_keeps_at_most_keep_spans(guardian, monkeypatch):
    monkeypatch.setattr(guardian, "KEEP_SPANS", 3)
    for i in range(6):
        guardian.write_span([{"role": "user", "content": "m%d" % i}], "s", i)
    assert len(list(Path(guardian.SPAN_DIR).glob("*/*.json"))) <= 3


# --- 0.2.0 behaviour that must survive the 0.3.0 promotion ------------------

def test_tools_are_still_counted(guardian):
    assert guardian.estimate_tool_tokens({"tools": [{"name": "x" * 350}]}) > 0


def test_upstream_timeouts_are_env_tunable(monkeypatch, tmp_path):
    """Backported from 0.2.0, which had these as env vars while the copy that
    ran hardcoded them at the call site."""
    monkeypatch.setenv("GUARDIAN_UPSTREAM_TIMEOUT", "42.0")
    monkeypatch.setenv("GUARDIAN_UPSTREAM_CONNECT_TIMEOUT", "7.5")
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "l.json"))
    mod = importlib.reload(sys.modules["context_guardian"]) \
        if "context_guardian" in sys.modules \
        else importlib.import_module("context_guardian")
    assert mod.UPSTREAM_TIMEOUT_SECONDS == 42.0
    assert mod.UPSTREAM_CONNECT_TIMEOUT_SECONDS == 7.5


def test_upstream_timeout_defaults_are_not_httpx_defaults(guardian):
    """httpx defaults to ~5s. A local model can think for minutes before its
    first byte, so a generic HTTP default is a wrong default here."""
    assert guardian.UPSTREAM_TIMEOUT_SECONDS >= 60.0


def test_stats_reports_the_new_config(guardian):
    from fastapi.testclient import TestClient
    data = TestClient(guardian.app).get("/guardian/stats").json()
    assert data["upstream_timeout_seconds"] == guardian.UPSTREAM_TIMEOUT_SECONDS
    assert "spans" in data.get("code_version", "")
