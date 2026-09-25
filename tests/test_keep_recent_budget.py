"""Tests for cg-keep-recent-budget-v1: KEEP_RECENT as a token budget.

WHY THIS FILE EXISTS
    KEEP_RECENT_MESSAGES was a bare message count (8). In an agent session one
    "message" can be a 40 KB tool result, so keeping the newest 8 could be 90%
    of the window -- a compaction that frees nothing -- or, on short chat
    turns, far too little to work with. The default is now a TOKEN BUDGET:
    keep the newest messages totalling at most ~20% of the usable window
    (NUM_CTX - RESERVE_OUTPUT), never fewer than 4. Setting
    GUARDIAN_KEEP_RECENT_MESSAGES explicitly must restore the EXACT old
    count behaviour, which is what tests/test_guardian.py ("2") and
    tests/test_spans.py ("4") already pin.

    Config is read at import time, so every test reloads the module with its
    env set first (same fixture pattern as tests/test_guardian.py), and the
    budget-mode tests delenv GUARDIAN_KEEP_RECENT_MESSAGES outright so a
    stray value in the ambient environment cannot fake an old-behaviour pass.
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reload(monkeypatch, tmp_path, **env):
    """Re-import context_guardian with a known env, exactly like the other
    test files do. GUARDIAN_KEEP_RECENT_MESSAGES / _FRACTION are cleared
    unless the caller asks for them, so the default is the default."""
    monkeypatch.delenv("GUARDIAN_KEEP_RECENT_MESSAGES", raising=False)
    monkeypatch.delenv("GUARDIAN_KEEP_RECENT_FRACTION", raising=False)
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "32768")
    monkeypatch.setenv("GUARDIAN_RESERVE_OUTPUT", "8192")
    monkeypatch.setenv("GUARDIAN_VERSION_CHECK", "0")
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "guardian_log.json"))
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "guardian_spans"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


@pytest.fixture()
def budget_guardian(monkeypatch, tmp_path):
    """Budget mode: GUARDIAN_KEEP_RECENT_MESSAGES unset."""
    return _reload(monkeypatch, tmp_path)


def _user(text):
    return {"role": "user", "content": text}


def test_default_is_budget_mode(budget_guardian):
    g = budget_guardian
    assert g.KEEP_RECENT_EXPLICIT is False
    assert g.KEEP_RECENT_MESSAGES == 4
    assert g.KEEP_RECENT_FRACTION == 0.20


def test_explicit_env_is_old_count(monkeypatch, tmp_path):
    g = _reload(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="8")
    assert g.KEEP_RECENT_EXPLICIT is True
    # 40 tiny messages: even the budget walk would keep them all -- 8 proves
    # the old count, not the budget.
    tiny = [_user("a" * 35) for _ in range(40)]
    assert g.keep_recent_count(tiny) == 8
    # 40 messages that individually blow the whole budget: still exactly 8.
    huge = [_user("a" * 70000) for _ in range(40)]
    assert g.keep_recent_count(huge) == 8


def test_explicit_zero_keeps_none(monkeypatch, tmp_path):
    g = _reload(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="0")
    chat = [_user(f"turn {i}") for i in range(10)]
    assert g.partition_messages(chat)["to_keep"] == []


def test_budget_keeps_many_small_messages(budget_guardian):
    g = budget_guardian
    # 35 chars / 3.5 = 10 tokens per message.
    # (32768 - 8192) * 0.20 = 4915 tokens of budget.
    fifty = [_user("a" * 35) for _ in range(50)]  # 500 tokens: all fit
    assert g.keep_recent_count(fifty, num_ctx=32768, reserve=8192) == 50
    thousand = [_user("a" * 35) for _ in range(1000)]
    # 491 * 10 = 4910 <= 4915; the 492nd would cross it.
    assert g.keep_recent_count(thousand, num_ctx=32768, reserve=8192) == 491


def test_budget_never_below_floor(budget_guardian):
    g = budget_guardian
    # Each message is ~20,000 tokens, so the walk includes nothing at all;
    # the floor of 4 is what keeps the model any working context.
    monsters = [_user("a" * 70000) for _ in range(10)]
    assert g.keep_recent_count(monsters) == 4


def test_budget_floor_capped_by_length(budget_guardian):
    g = budget_guardian
    # Fewer messages than the floor: keep them all, never invent some.
    assert g.keep_recent_count([_user("a" * 35), _user("b" * 35)]) == 2


def test_budget_stops_at_first_overflow(budget_guardian):
    g = budget_guardian
    # Newest->oldest: 3 small (30 tokens), then a huge one, then 20 more
    # small ones. The walk must STOP at the huge message -- skipping it to
    # reach the 20 tiny older ones would resurrect a megabyte of compacted
    # history and defeat the compaction that was just paid for.
    messages = ([_user("a" * 35) for _ in range(20)]
                + [_user("b" * 70000)]
                + [_user("a" * 35) for _ in range(3)])
    assert g.keep_recent_count(messages) == 4  # max(3 included, floor 4)


def test_budget_reserve_clamp(budget_guardian):
    g = budget_guardian
    # reserve 8192 >= num_ctx 4096: clamp to half the window (2048), so
    # usable = 2048 and budget = int(2048 * 0.2) = 409. At 10 tokens per
    # message that is 40 messages; the 41st would cross 409.
    msgs = [_user("a" * 35) for _ in range(1000)]
    assert g.keep_recent_count(msgs, num_ctx=4096, reserve=8192) == 40


def test_partition_uses_budget_and_keeps_tool_pairs_whole(budget_guardian):
    g = budget_guardian
    # Budget walk: s3, s2, s1 (30 tokens) then the 20,000-char tool result
    # (~5,714 tokens) which alone busts the 4,915-token budget -> keep 4.
    # The raw cut therefore lands ON the tool result and the kept window
    # would start with an orphaned tool message; _safe_cut must walk it back
    # over the assistant turn that opened the call.
    assistant = {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "read", "arguments": "{}"}}]}
    tool_result = {"role": "tool", "tool_call_id": "call_1",
                   "content": "a" * 20000}
    messages = ([_user("a" * 35) for _ in range(28)]
                + [assistant, tool_result]
                + [_user("a" * 35) for _ in range(3)])
    part = g.partition_messages(messages)
    to_keep = part["to_keep"]
    # The budget (not a fixed count) chose the cut: 4 messages, and
    # _safe_cut kept ONE extra -- the assistant turn -- so the pair is whole.
    assert g.keep_recent_count(messages) == 4
    assert len(to_keep) == 5
    # Whatever the cut, the kept window is a valid message list.
    assert to_keep[0]["role"] != "tool"
    assert to_keep[0]["role"] == "assistant"
    assert "tool_calls" in to_keep[0]
    assert to_keep[1] is tool_result


def test_stats_reports_mode(budget_guardian):
    testclient = pytest.importorskip("fastapi.testclient")
    g = budget_guardian
    client = testclient.TestClient(g.app)
    body = client.get("/guardian/stats").json()
    # The value has to come off the route, not a constant in the test: bind
    # it to the module's own mode flag the way the route does.
    expected = "messages" if g.KEEP_RECENT_EXPLICIT else "budget"
    assert expected == "budget"  # env unset -> budget mode
    assert body["keep_recent_mode"] == expected
    assert body["keep_recent_fraction"] == g.KEEP_RECENT_FRACTION == 0.20
    assert body["keep_recent_messages"] == g.KEEP_RECENT_MESSAGES


def test_keep_recent_count_is_pure(budget_guardian):
    g = budget_guardian
    messages = [_user("a" * 35) for _ in range(50)]
    before = list(messages)
    count = g.keep_recent_count(messages)
    assert count == 50
    assert messages == before
    assert len(messages) == len(before) == 50
