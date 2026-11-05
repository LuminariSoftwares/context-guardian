"""cg_breakdown: the six-way split of a request, and its place on /guardian/stats."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cg_breakdown  # noqa: E402
import context_guardian as g  # noqa: E402

TOOL = {"type": "function", "function": {"name": "read_file", "description": "Read a file " * 5,
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
FULL = {"model": "m", "tools": [TOOL], "messages": [
    {"role": "system", "content": "You are careful. " * 20},
    {"role": "user", "content": "please read the notes " * 10},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                           "function": {"name": "read_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "line of notes " * 200},
    {"role": "assistant", "content": "The notes say hello. " * 15}]}


def split(p):
    return cg_breakdown.breakdown_tokens(p, estimate_messages=g.estimate_tokens, estimate_tools=g.estimate_tool_tokens)


def est(p):
    return g.estimate_tokens(p.get("messages") or []) + g.estimate_tool_tokens(p)


def test_six_keys_nonnegative_ints():
    b = split(FULL)
    assert set(b) == set(cg_breakdown.KEYS) == {"system", "tool_schemas", "history", "tool_results", "summary", "pins"}
    assert all(isinstance(v, int) and v >= 0 for v in b.values())


def test_parts_land_in_their_category():
    b = split(FULL)
    assert b["system"] > 0 and b["tool_schemas"] > 0 and b["tool_results"] > b["system"] and b["history"] > 0
    assert b["summary"] == 0 and b["pins"] == 0


def test_sum_matches_request_estimate():
    plain = {"messages": [{"role": "user", "content": "q " * 300}, {"role": "assistant", "content": "a " * 300}]}
    for p in (FULL, plain):
        assert abs(sum(split(p).values()) - est(p)) <= max(1, 0.01 * est(p))


def test_no_tools_gives_zero_schemas():
    assert split({"messages": [{"role": "user", "content": "hi " * 50}]})["tool_schemas"] == 0


def test_summary_message_counted_as_summary_not_system():
    p = {"messages": [{"role": "system", "content": g.GUARDIAN_SUMMARY_MARKER + " x]:\n\n" + "s " * 200},
                      {"role": "developer", "content": "dev rules " * 20}, {"role": "user", "content": "hi"}]}
    b = split(p)
    assert b["summary"] > b["system"] > 0


def test_anthropic_tool_result_blocks_and_top_level_system():
    p = {"system": "sys " * 50, "messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "r " * 300},
        {"type": "text", "text": "go on and do it " * 5}]}]}
    b = split(p)
    assert b["tool_results"] > 0 and b["system"] > 0 and b["history"] > 0


def test_standalone_defaults_and_odd_input():
    assert sum(cg_breakdown.breakdown_tokens(FULL).values()) > 0
    assert cg_breakdown.breakdown_tokens("nope") == dict.fromkeys(cg_breakdown.KEYS, 0)
    assert cg_breakdown.breakdown_tokens({"messages": "x"}) == dict.fromkeys(cg_breakdown.KEYS, 0)


def test_note_breakdown_stores_and_fails_open(monkeypatch):
    monkeypatch.setattr(g, "_breakdown", dict.fromkeys(cg_breakdown.KEYS, 0))
    g.note_breakdown(FULL)
    kept = dict(g._breakdown)
    assert kept["tool_schemas"] > 0
    monkeypatch.setattr(cg_breakdown, "breakdown_tokens", lambda *a, **k: 1 / 0)
    g.note_breakdown(FULL)
    assert g._breakdown == kept


def test_stats_carries_breakdown_and_nothing_else_new():
    from fastapi.testclient import TestClient
    d = TestClient(g.app, base_url="http://127.0.0.1:8786").get("/guardian/stats").json()
    assert set(d["breakdown"]) == set(cg_breakdown.KEYS)
    json.dumps(d)
