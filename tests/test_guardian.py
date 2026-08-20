"""
Basic tests for context_guardian.py.

Run with: pytest
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def guardian(monkeypatch, tmp_path):
    """Import (or re-import) context_guardian with test-friendly env vars
    set BEFORE module import, since its config is read at import time."""
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "1000")
    monkeypatch.setenv("GUARDIAN_COMPACT_THRESHOLD", "0.5")
    monkeypatch.setenv("GUARDIAN_KEEP_RECENT_MESSAGES", "2")
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "guardian_log.json"))

    if "context_guardian" in sys.modules:
        module = importlib.reload(sys.modules["context_guardian"])
    else:
        module = importlib.import_module("context_guardian")
    return module


def test_estimate_tokens_basic(guardian):
    messages = [{"role": "user", "content": "a" * 35}]  # 35 chars / 3.5 = 10 tokens
    assert guardian.estimate_tokens(messages) == 10


def test_estimate_tokens_ignores_non_string_content_blocks(guardian):
    messages = [{"role": "user", "content": [{"type": "text", "text": "a" * 35}]}]
    assert guardian.estimate_tokens(messages) == 10


def test_estimate_tokens_empty(guardian):
    assert guardian.estimate_tokens([]) == 0


@pytest.mark.asyncio
async def test_maybe_compact_below_threshold_is_noop(guardian):
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "short message"}],
    }
    result = await guardian.maybe_compact(client=None, payload=payload)
    assert result == payload
    assert guardian._state["compactions_performed"] == 0


@pytest.mark.asyncio
async def test_maybe_compact_triggers_and_fails_open_on_summarization_error(guardian, monkeypatch):
    async def _failing_summarize(client, model, older_messages):
        return None  # simulates a failed summarization call

    monkeypatch.setattr(guardian, "summarize_older_messages", _failing_summarize)

    long_text = "x" * 4000  # comfortably over the 500-token test threshold
    messages = [{"role": "user", "content": long_text} for _ in range(5)]
    payload = {"model": "test-model", "messages": messages}

    result = await guardian.maybe_compact(client=object(), payload=payload)

    # Fail-open: original payload returned unchanged, no compaction counted.
    assert result == payload
    assert guardian._state["compactions_performed"] == 0


@pytest.mark.asyncio
async def test_maybe_compact_triggers_and_compacts_on_success(guardian, monkeypatch):
    async def _fake_summarize(client, model, older_messages):
        return "condensed summary of older turns"

    monkeypatch.setattr(guardian, "summarize_older_messages", _fake_summarize)

    long_text = "x" * 4000
    messages = [{"role": "user", "content": long_text} for _ in range(5)]
    payload = {"model": "test-model", "messages": messages}

    result = await guardian.maybe_compact(client=object(), payload=payload)

    assert guardian._state["compactions_performed"] == 1
    # Recent messages (KEEP_RECENT_MESSAGES=2) plus one summary message.
    assert len(result["messages"]) == 3
    assert "condensed summary of older turns" in result["messages"][0]["content"]


def test_stats_endpoint_reports_config(guardian):
    from fastapi.testclient import TestClient

    client = TestClient(guardian.app)
    resp = client.get("/guardian/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["num_ctx"] == 1000
    assert data["compact_threshold"] == 0.5
    assert "requests_seen" in data


# --- tool-definition accounting (0.2.0) -------------------------------------
# Regression tests for the bug this release fixes: Guardian estimated the
# request from `messages` alone and never looked at `tools`, so for any agentic
# or MCP-backed client it was measuring a fraction of the real payload.

def test_estimate_tool_tokens_counts_tools(guardian):
    tools = [{"name": "x", "description": "y" * 28}]
    # serialised length / 3.5, so just assert it is non-trivially counted
    assert guardian.estimate_tool_tokens({"tools": tools}) > 5


def test_estimate_tool_tokens_counts_legacy_functions(guardian):
    fns = [{"name": "x", "description": "y" * 28}]
    assert (guardian.estimate_tool_tokens({"functions": fns})
            == guardian.estimate_tool_tokens({"tools": fns}))


def test_estimate_tool_tokens_zero_without_tools(guardian):
    assert guardian.estimate_tool_tokens({"messages": []}) == 0


def test_estimate_tool_tokens_survives_unserialisable_payload(guardian):
    """Never raise on the request path -- a weird payload must not 500."""
    assert guardian.estimate_tool_tokens({"tools": object()}) >= 0


def test_estimate_tokens_still_ignores_tools(guardian):
    """The two halves stay separate so the log can distinguish them."""
    messages = [{"role": "user", "content": "a" * 35}]
    assert guardian.estimate_tokens(messages) == 10


def test_count_tools_can_be_disabled(monkeypatch, tmp_path):
    import importlib
    import sys
    monkeypatch.setenv("GUARDIAN_COUNT_TOOLS", "0")
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "g.json"))
    module = importlib.reload(sys.modules["context_guardian"])
    assert module.COUNT_TOOLS is False
    assert module.estimate_tool_tokens({"tools": [{"name": "x" * 100}]}) == 0
