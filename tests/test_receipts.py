"""Phase 2 acceptance: recall receipts -- real numbers, counts only, local only.

Every recall path (the CLI, the MCP tools) and the proxy's own compactions
append a counts-only line to receipts.jsonl, and `context-guardian-recall
--receipts` adds them up. The privacy promise is tested the only way that
means anything: a unique string is planted in the conversation, the query and
the path, and must appear in none of what the receipts write or print.
"""
import json
from pathlib import Path

import pytest

from _proxy_stub import LOOPBACK, load, post, wire

import cg_mcp
import cg_recall_cli

PLANTED = "PLANTED-q7Zx41"


def _receipts(path):
    if not Path(path).exists():
        return []
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def archive(monkeypatch, tmp_path):
    """A span archive under a directory whose NAME is planted too, so a path
    leaking into a receipt is caught as surely as message text."""
    root = tmp_path / ("dir-" + PLANTED) / "spans"
    run = root / "20261023-120000"
    run.mkdir(parents=True)
    (run / "0001.json").write_text(json.dumps({
        "run_id": "20261023-120000", "index": 1, "summary": "the summary " + PLANTED,
        "messages": [{"role": "user", "content": "the secret detail " + PLANTED},
                     {"role": "assistant", "content": "noted " + PLANTED}]}), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(root))
    monkeypatch.setenv("GUARDIAN_RECEIPTS_PATH", str(receipts))
    monkeypatch.delenv("GUARDIAN_RECEIPTS", raising=False)
    return root, receipts


def _mcp(name, **args):
    return cg_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


def test_recall_calls_are_counted(archive, capsys):
    root, receipts = archive
    assert cg_recall_cli.main([PLANTED]) == 0
    assert _mcp("search_spans", term="secret detail")["isError"] is False
    assert _mcp("read_span", run="20261023-120000", index=1)["isError"] is False
    capsys.readouterr()
    events = _receipts(receipts)
    assert [e["tool"] for e in events] == ["cli.search", "mcp.search_spans", "mcp.read_span"]
    search = events[1]
    assert search["query_terms"] == 2 and search["spans_matched"] == 1 and search["chars_returned"] > 0
    assert events[2]["spans_opened"] == 1 and events[2]["chars_returned"] > 0
    for e in events:
        assert set(e) == {"ts", "tool", "query_terms", "spans_matched", "spans_opened", "chars_returned"}

    assert cg_recall_cli.main(["--receipts"]) == 0
    out = capsys.readouterr().out
    assert "3 recall call" in out, out
    assert "1 span" in out and "opened" in out, out


def test_no_text_query_or_path_ever_reaches_a_receipt(archive, capsys, monkeypatch, tmp_path):
    root, receipts = archive
    cg_recall_cli.main(["the secret detail " + PLANTED])
    _mcp("search_spans", term=PLANTED)
    _mcp("read_span", run="20261023-120000", index=1)
    _mcp("list_spans", run="20261023-120000")
    capsys.readouterr()
    raw = receipts.read_text(encoding="utf-8")
    assert PLANTED not in raw
    assert "20261023-120000" not in raw and str(tmp_path) not in raw and "dir-" not in raw

    assert cg_recall_cli.main(["--receipts", "--share"]) == 0
    shared = capsys.readouterr().out
    assert shared.strip(), "the --share block is empty"
    for leak in (PLANTED, "20261023-120000", str(tmp_path), str(root), "/", "\\"):
        assert leak not in shared, (leak, shared)


def test_a_corrupt_receipts_line_is_skipped_not_fatal(archive, capsys):
    root, receipts = archive
    cg_recall_cli.main([PLANTED])
    with open(receipts, "a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")
        fh.write('["a list", "not an object"]\n')
        fh.write('{"tool": "cli.search", "ts": "not a date", "query_terms": "seven"}\n')
    cg_recall_cli.main([PLANTED])
    capsys.readouterr()
    assert cg_recall_cli.main(["--receipts"]) == 0
    assert "2 recall call" in capsys.readouterr().out


def test_since_filters_by_time(archive, capsys):
    root, receipts = archive
    receipts.write_text(json.dumps({"ts": "2001-01-01T00:00:00+00:00", "tool": "cli.search",
                                    "query_terms": 1, "spans_matched": 1, "spans_opened": 0,
                                    "chars_returned": 10}) + "\n", encoding="utf-8")
    cg_recall_cli.main([PLANTED])
    capsys.readouterr()
    assert cg_recall_cli.main(["--receipts", "--since", "7d"]) == 0
    assert "1 recall call" in capsys.readouterr().out
    assert cg_recall_cli.main(["--receipts"]) == 0
    assert "2 recall call" in capsys.readouterr().out


def test_off_switch_writes_nothing(archive, monkeypatch, capsys):
    root, receipts = archive
    monkeypatch.setenv("GUARDIAN_RECEIPTS", "0")
    cg_recall_cli.main([PLANTED])
    _mcp("search_spans", term=PLANTED)
    capsys.readouterr()
    assert not receipts.exists()


def test_the_proxy_counts_archiving_and_recall_it_sees(monkeypatch, tmp_path):
    receipts = tmp_path / "receipts.jsonl"
    g = load(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="2",
             GUARDIAN_RECEIPTS_PATH=str(receipts))
    wire(g)
    history = [{"role": "user", "content": PLANTED + " " + "x" * 1500} for _ in range(5)]
    assert post(g, json.dumps({"model": "m", "messages": history}).encode()).status_code == 200
    compaction = [e for e in _receipts(receipts) if e["tool"] == "proxy.compaction"]
    assert len(compaction) == 1
    assert compaction[0]["chars_archived"] > 0 and compaction[0]["pointer"] is True
    assert compaction[0]["tokens_saved"] > 0

    # The model then calls the recall tool. Clients resend the whole history,
    # so the same call arrives on every later request: counted once.
    call = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "mcp__context-guardian__search_spans",
                     "arguments": json.dumps({"term": PLANTED})}}]}
    small = [{"role": "user", "content": "hi"}, call,
             {"role": "tool", "tool_call_id": "call_1", "content": "found"}]
    for _ in range(3):
        assert post(g, json.dumps({"model": "m", "messages": small}).encode()).status_code == 200
    seen = [e for e in _receipts(receipts) if e["tool"] == "proxy.recall_seen"]
    assert sum(e["calls"] for e in seen) == 1

    from fastapi.testclient import TestClient
    stats = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()["receipts"]
    assert stats["enabled"] is True
    assert stats["compactions_with_pointer"] == 1 and stats["recall_calls_seen"] == 1
    assert stats["chars_archived"] == compaction[0]["chars_archived"]
    assert PLANTED not in receipts.read_text(encoding="utf-8")


def test_a_receipts_failure_never_costs_the_request(monkeypatch, tmp_path):
    blocker = tmp_path / "receipts.jsonl"
    blocker.mkdir()                      # a directory where the file should be
    g = load(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="2",
             GUARDIAN_RECEIPTS_PATH=str(blocker))
    wire(g)
    history = [{"role": "user", "content": "x" * 1500} for _ in range(5)]
    assert post(g, json.dumps({"model": "m", "messages": history}).encode()).status_code == 200
    assert g._state["compactions_performed"] == 1


def test_health_page_has_a_receipts_line(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    g = load(monkeypatch, tmp_path)
    html = TestClient(g.app, base_url=LOOPBACK).get("/guardian/health").text
    assert 'id="receipts"' in html and "s.receipts" in html
