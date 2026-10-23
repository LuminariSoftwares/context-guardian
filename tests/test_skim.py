"""Phase 3 acceptance: skim the archive in a few hundred tokens, open only what is needed.

At span-write time a digest line goes to <run>/index.jsonl (first sentence of
the summary Guardian already made, top keywords, sizes). The MCP tools build on
it: list_digests skims newest-first under a hard token cap, search_spans ranks
by term hits then recency, read_span opens one span or a message range of it,
get_seal_status runs the Phase 1 verifier. Written before the code.
"""
import json
import math
from pathlib import Path

import pytest

from _proxy_stub import load

import cg_archive
import cg_mcp
import cg_recall_cli


def _mcp(name, **args):
    res = cg_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})["result"]
    return res


def _index(g):
    path = Path(g.SPAN_DIR) / g.RUN_ID / "index.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def g(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path)


def test_every_span_write_appends_a_digest_line(g):
    g.write_span([{"role": "user", "content": "Deploy the parser to staging. Then the parser docs."},
                  {"role": "assistant", "content": "parser deployed, staging green, parser docs next"}],
                 "The parser went to staging. Docs follow tomorrow.", 1)
    [line] = _index(g)
    assert set(line) == {"span", "ts", "messages", "chars", "est_tokens", "digest", "digest_from", "terms"}
    assert line["span"] == "0001" and line["messages"] == 2
    assert line["digest"] == "The parser went to staging."
    assert line["digest_from"] == "summary"
    assert line["terms"][0] == "parser" and len(line["terms"]) <= 8
    assert "the" not in line["terms"] and "then" not in line["terms"]
    assert line["est_tokens"] == int(line["chars"] / 3.5)


def test_a_digest_is_capped_at_160_chars(g):
    g.write_span([{"role": "user", "content": "x"}], "word " * 100 + ".", 1)
    assert len(_index(g)[0]["digest"]) <= 160


@pytest.mark.parametrize("max_tokens", [40, 80, 150, 400, 1200])
def test_skim_never_exceeds_max_tokens(g, max_tokens):
    for i in range(1, 31):
        g.write_span([{"role": "user", "content": "message %d " % i + "filler " * 50}],
                     "Span number %d covers the %s work in detail. More." % (i, "migration " * 8), i)
    res = _mcp("list_digests", max_tokens=max_tokens)
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert cg_archive.est_tokens(text) <= max_tokens, (cg_archive.est_tokens(text), text)
    assert math.ceil(len(text) / 3.5) == cg_archive.est_tokens(text)
    if max_tokens < 1200:
        assert "more span" in text, text


def test_skim_is_newest_first_and_counts_the_rest(g):
    for i in range(1, 6):
        g.write_span([{"role": "user", "content": "m%d" % i}], "Digest number %d here." % i, i)
    text = _mcp("list_digests", max_tokens=400)["content"][0]["text"]
    lines = text.splitlines()
    assert "Digest number 5 here." in lines[0] and "Digest number 4 here." in lines[1]
    limited = _mcp("list_digests", max=2)["content"][0]["text"]
    assert "Digest number 3" not in limited and "3 more spans" in limited


def test_open_returns_byte_identical_message_content(g):
    tricky = [
        {"role": "user", "content": "line one\n  indented line two  \n\ttab and trailing space "},
        {"role": "assistant", "content": "unicode: café ✓ — 日本語, and a fake header:\n[7] user: not real"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "edit", "arguments": "{\"path\": \"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "edited ok"},
    ]
    path = Path(g.write_span(tricky, "A summary that is long enough.", 1))
    stored = json.loads(path.read_text(encoding="utf-8"))["messages"]
    text = _mcp("read_span", run=g.RUN_ID, index=1)["content"][0]["text"]
    for i, m in enumerate(stored):
        if set(m) == {"role", "content"} and isinstance(m["content"], str):
            expected = "[%d] %s: %s" % (i, m["role"], m["content"])
        else:
            expected = "[%d] %s: %s" % (i, m["role"], json.dumps(m, ensure_ascii=False))
        assert expected in text, (expected, text)
    ranged = _mcp("read_span", run=g.RUN_ID, index=1, start=1, end=1)["content"][0]["text"]
    assert "[1] assistant: " + stored[1]["content"] in ranged
    assert "[0] user:" not in ranged and "[2] assistant:" not in ranged


def test_open_truncates_with_a_start_end_notice(g):
    big = [{"role": "user", "content": "%d " % i + "y" * 3000} for i in range(12)]
    g.write_span(big, "A summary that is long enough.", 1)
    text = _mcp("read_span", run=g.RUN_ID, index=1)["content"][0]["text"]
    assert len(text) < cg_mcp.READ_SPAN_MAX_CHARS + 600
    assert "truncated" in text and "start=" in text, text[-400:]


def test_search_finds_a_planted_term_ranked_by_hits_then_recency(g):
    g.write_span([{"role": "user", "content": "the ZEBRA-9931 flag only"}], "Older span digest.", 1)
    g.write_span([{"role": "user", "content": "unrelated chatter"},
                  {"role": "assistant", "content": "deploy ZEBRA-9931 to staging now"}],
                 "Middle span digest.", 2)
    g.write_span([{"role": "user", "content": "zebra-9931 mentioned again"}], "Newest span digest.", 3)
    text = _mcp("search_spans", term="zebra-9931 deploy")["content"][0]["text"]
    hits = [ln for ln in text.splitlines() if ".json msg" in ln]
    assert hits[0].startswith("%s/0002.json msg 1 [assistant]" % g.RUN_ID), hits
    # Same number of hits: the newer span first.
    assert hits[1].startswith("%s/0003.json" % g.RUN_ID) and hits[2].startswith("%s/0001.json" % g.RUN_ID), hits


def test_reindex_equals_live_indexing(g, capsys):
    g.write_span([{"role": "user", "content": "alpha beta gamma alpha"}], "First summary. More.", 1)
    g.write_span([{"role": "user", "content": "delta epsilon delta"}], "Second summary! More.", 2)
    g.write_span([{"role": "user", "content": "zeta eta zeta zeta"}], "Third summary? More.", 3)
    live = _index(g)
    index = Path(g.SPAN_DIR) / g.RUN_ID / "index.jsonl"
    index.unlink()
    assert cg_recall_cli.main(["--reindex"]) == 0
    capsys.readouterr()
    assert _index(g) == live


def test_reindex_without_a_summary_labels_the_digest(g, capsys):
    run = Path(g.SPAN_DIR) / "20250101-000000"
    run.mkdir(parents=True)
    (run / "0001.json").write_text(json.dumps({
        "run_id": "20250101-000000", "index": 1, "at": "2025-01-01 00:00:00",
        "messages": [{"role": "system", "content": "sys"},
                     {"role": "user", "content": "Please port the billing module. It is old."}]}),
        encoding="utf-8")
    assert cg_recall_cli.main(["--reindex", "--run", "20250101-000000"]) == 0
    capsys.readouterr()
    [line] = [json.loads(ln) for ln in (run / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert line["digest_from"] == "first-user-message"
    assert line["digest"] == "Please port the billing module."


def test_get_seal_status_reports_ok_then_the_break(g):
    paths = [Path(g.write_span([{"role": "user", "content": "m%d" % i}], "Summary %d." % i, i))
             for i in (1, 2)]
    res = _mcp("get_seal_status")
    assert res["isError"] is False and "OK" in res["content"][0]["text"]
    paths[0].write_text("{}", encoding="utf-8")
    text = _mcp("get_seal_status", run=g.RUN_ID)["content"][0]["text"]
    assert "content-changed" in text and "0001.json" in text


@pytest.mark.asyncio
async def test_the_archive_pointer_names_the_tools(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="2")

    async def _fake(client, model, older, headers=None):
        return "condensed summary of the older turns, long enough to be accepted"
    monkeypatch.setattr(g, "summarize_older_messages", _fake)
    messages = [{"role": "user", "content": "x" * 1500} for _ in range(5)]
    out = await g.maybe_compact(client=object(), payload={"model": "m", "messages": messages})
    note = out["messages"][0]["content"]
    for tool in ("list_digests", "search_spans", "read_span"):
        assert tool in note, note


def test_the_engine_uses_the_same_stopwords():
    """cg_seal.js indexes the engine's spans with a copy of the list; a copy
    that drifts makes the engine's keywords differ from a --reindex of them."""
    import re
    js = (Path(__file__).resolve().parent.parent / "cg_seal.js").read_text(encoding="utf-8")
    words = re.search(r"export const STOPWORDS = new Set\(\('([^']*)'\)", js).group(1).split()
    assert set(words) == set(cg_archive.STOPWORDS)
