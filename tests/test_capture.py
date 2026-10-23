"""Phase 4 acceptance: always-on capture (opt-in), the whole session and not just what is evicted.

GUARDIAN_CAPTURE=journal appends every NEW message of a run to
<run>/journal.jsonl. Clients resend the whole history on every request and
compaction rewrites it, so "new" is decided by hashing each message and
counting: a message is journaled the first time it appears, and a message that
genuinely repeats is journaled each time it repeats. Written before the code.
"""
import json
from pathlib import Path

import pytest

from _proxy_stub import LOOPBACK, SUMMARY, load, post, wire

import cg_mcp
import cg_recall_cli


def _journal(g):
    run = Path(g.SPAN_DIR) / g.RUN_ID
    files = sorted(run.glob("journal*.jsonl"), key=lambda p: (len(p.name), p.name))
    out = []
    for f in files:
        out += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def _conversation(turns, filler=0):
    """The client's own history after `turns` turns, resent whole each time."""
    msgs = [{"role": "system", "content": "You are a coding assistant."}]
    for t in range(turns):
        msgs.append({"role": "user", "content": "question %d %s" % (t, "x" * filler)})
        msgs.append({"role": "assistant", "content": "answer %d %s" % (t, "y" * filler)})
    return msgs


def _send(g, messages):
    resp = post(g, json.dumps({"model": "m", "messages": messages}).encode())
    assert resp.status_code == 200
    return resp


@pytest.fixture()
def capture(monkeypatch, tmp_path):
    def make(**env):
        base = {"GUARDIAN_CAPTURE": "journal", "GUARDIAN_NUM_CTX": "100000"}
        base.update(env)
        g = load(monkeypatch, tmp_path, **base)
        seen = wire(g)
        return g, seen
    return make


def test_five_resent_requests_journal_each_message_exactly_once(capture):
    g, _ = capture()
    for turn in range(1, 6):
        history = _conversation(turn)
        history[-1:] = []                      # the request carries the new user turn
        _send(g, history)
    _send(g, _conversation(5))                 # ...and the last answer arrives with the next one
    journal = _journal(g)
    contents = [e["message"]["content"] for e in journal]
    expected = [m["content"] for m in _conversation(5)]
    assert contents == expected, contents
    assert len(set(e["hash"] for e in journal)) == len(journal)
    # Each line says which request first carried it.
    assert journal[0]["request"] == 1 and journal[-1]["request"] == 6
    assert [e["n"] for e in journal] == list(range(len(journal)))


def test_a_message_that_really_repeats_is_journaled_each_time(capture):
    g, _ = capture()
    history = [{"role": "user", "content": "yes"}, {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "yes"}]
    _send(g, history[:1])
    _send(g, history)
    _send(g, history)
    assert [e["message"]["content"] for e in _journal(g)] == ["yes", "ok", "yes"]


def test_a_compaction_mid_conversation_neither_duplicates_nor_drops(capture, capsys):
    g, seen = capture(GUARDIAN_NUM_CTX="2000", GUARDIAN_COMPACT_THRESHOLD="0.5",
                      GUARDIAN_RESERVE_OUTPUT="100", GUARDIAN_KEEP_RECENT_MESSAGES="2")
    for turn in range(1, 6):
        _send(g, _conversation(turn, filler=400))
    assert g._state["compactions_performed"] >= 1
    assert any(SUMMARY in b.decode() for b in seen["chat"]), "no forwarded request carried the summary"
    contents = [e["message"]["content"] for e in _journal(g)]
    assert contents == [m["content"] for m in _conversation(5, filler=400)]
    assert not any("Context Guardian auto-compaction" in c for c in contents)

    g.flush_journal()                           # what shutdown does
    code = cg_recall_cli.main(["--verify", "--run", g.RUN_ID])
    out = capsys.readouterr().out
    assert code == 0, out


def test_the_cap_stops_capture_without_touching_responses(capture, caplog):
    g, seen = capture(GUARDIAN_CAPTURE_MAX_MB="0.004")      # 4 KB
    with caplog.at_level("WARNING", logger="context_guardian"):
        for turn in range(1, 25):
            history = _conversation(turn, filler=200)
            body = json.dumps({"model": "m", "messages": history}).encode()
            assert post(g, body).status_code == 200
            assert seen["chat"][-1] == body                 # forwarded byte for byte
    run = Path(g.SPAN_DIR) / g.RUN_ID
    files = sorted(run.glob("journal*.jsonl"))
    assert len(files) >= 2, "no rotation to journal.N.jsonl"
    total = sum(f.stat().st_size for f in files)
    assert total <= 4 * 1000 + 2000, total                  # the cap, plus at most one line
    stops = [r for r in caplog.records if "capture" in r.getMessage().lower()
             and "cap" in r.getMessage().lower()]
    assert len(stops) == 1, [r.getMessage() for r in stops]


def test_capture_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_CAPTURE", raising=False)
    g = load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="100000")
    wire(g)
    _send(g, _conversation(2))
    assert not list((Path(g.SPAN_DIR)).glob("*/journal*.jsonl"))


def test_a_capture_failure_never_costs_the_request(capture, caplog):
    g, seen = capture()
    (Path(g.SPAN_DIR) / g.RUN_ID / "journal.jsonl").mkdir(parents=True)
    with caplog.at_level("WARNING", logger="context_guardian"):
        _send(g, _conversation(2))
    assert len(seen["chat"]) == 1
    assert any("journal" in r.getMessage().lower() for r in caplog.records)


def test_search_and_open_cover_the_journal(capture, capsys, monkeypatch):
    g, _ = capture()
    history = [{"role": "user", "content": "the PLANTED-JRNL-77 detail"},
               {"role": "assistant", "content": "noted\nwith a second line"}]
    _send(g, history)
    _send(g, history + [{"role": "user", "content": "next"}])
    assert cg_recall_cli.main(["PLANTED-JRNL-77"]) == 0
    assert "journal" in capsys.readouterr().out
    res = cg_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_spans", "arguments": {"term": "planted-jrnl-77"}}})["result"]
    assert "journal" in res["content"][0]["text"]
    res = cg_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "read_span", "arguments": {"run": g.RUN_ID, "index": 0, "start": 1, "end": 1}}})["result"]
    text = res["content"][0]["text"]
    assert res["isError"] is False and "[1] assistant: noted\nwith a second line" in text, text
    assert "PLANTED-JRNL-77" not in text


def test_journal_seals_verify_and_catch_an_edit(capture, capsys):
    g, _ = capture()
    for turn in range(1, 4):
        _send(g, _conversation(turn))
    g.flush_journal()
    assert cg_recall_cli.main(["--verify", "--run", g.RUN_ID]) == 0
    chain = [json.loads(ln) for ln in (Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl")
             .read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "journal" for e in chain)
    path = Path(g.SPAN_DIR) / g.RUN_ID / "journal.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("answer 1", "answer X"), encoding="utf-8")
    capsys.readouterr()
    assert cg_recall_cli.main(["--verify", "--run", g.RUN_ID]) != 0
    assert "content-changed" in capsys.readouterr().out


def test_lines_written_after_the_last_seal_are_reported_not_a_break(capture, capsys):
    g, _ = capture()
    _send(g, _conversation(1))
    g.flush_journal()
    _send(g, _conversation(2))                 # appended, not yet sealed
    assert cg_recall_cli.main(["--verify", "--run", g.RUN_ID]) == 0
    assert "after the last seal" in capsys.readouterr().out


def test_stats_show_the_capture(capture):
    from fastapi.testclient import TestClient
    g, _ = capture()
    _send(g, _conversation(2))
    cap = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()["capture"]
    assert cap["mode"] == "journal" and cap["messages"] == 5 and cap["stopped"] is False
