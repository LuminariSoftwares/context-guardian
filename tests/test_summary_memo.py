"""Follow-up item 2: the same history must not be re-summarised on every request.

The proxy is invisible to its client, so the client keeps resending its whole,
uncompacted history. Once a conversation crosses the budget, every request
used to summarise the evicted prefix from scratch: one summariser call (seconds
to minutes on a local model) and one new span holding mostly the same messages,
every turn. Measured on b053b7e with these stubs: 3 requests, 3 full summaries,
3 overlapping spans, on both routes.

The per-run summary memo remembers, for each compaction, the identity hashes of
the evicted range, its summary and its span. An equal range reuses; a range
that extends a remembered one summarises only the new messages on top of the
old summary; anything else falls back to a full summary. Written before the
code; every test here fails on b053b7e.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from _proxy_stub import LOOPBACK, load

import cg_archive
import cg_recall_cli

SUMMARY = "condensed summary of the older turns, long enough to be accepted"


def _probe_env(**extra):
    env = {"GUARDIAN_NUM_CTX": "4000", "GUARDIAN_RESERVE_OUTPUT": "500",
           "GUARDIAN_COMPACT_THRESHOLD": "0.8", "GUARDIAN_KEEP_RECENT_MESSAGES": ""}
    env.update(extra)
    return env


def stub(g, delay=0.0, fail=False):
    """A recording upstream for both routes. Summary calls are told apart by
    the fence marker and counted, with the transcript they were given.
    `delay` holds each summary call open that long; `fail` answers it 500."""
    seen = {"summaries": [], "chat": [], "messages": []}

    async def handler(request):
        body = json.loads(request.content)
        prompt = json.dumps(body)
        if "BEGIN_TRANSCRIPT" in prompt:
            seen["summaries"].append(body)
            if delay:
                await asyncio.sleep(delay)
            if fail:
                return httpx.Response(500, json={"error": "summariser down"})
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"content": [{"type": "text", "text": SUMMARY}]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": SUMMARY},
                                                          "finish_reason": "stop"}]})
        # Forwarded calls are streamed through the proxy, so the body must be a
        # stream, as in _proxy_stub.wire: a pre-read json= body cannot be.
        if request.url.path.endswith("/messages"):
            seen["messages"].append(body)
            reply = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 10, "output_tokens": 1}}
        else:
            seen["chat"].append(body)
            reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        async def _stream():
            yield json.dumps(reply).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def client(g):
    from fastapi.testclient import TestClient
    return TestClient(g.app, base_url=LOOPBACK)


def chat_history(turns):
    msgs = [{"role": "system", "content": "You are a coding assistant."}]
    for t in range(turns):
        msgs.append({"role": "user", "content": "question %d %s" % (t, "q" * 1100)})
        msgs.append({"role": "assistant", "content": "answer %d %s" % (t, "a" * 1100)})
    return msgs


def messages_history(turns):
    """Anthropic shape, with a tool_use / tool_result pair in every turn."""
    msgs = []
    for t in range(turns):
        msgs.append({"role": "user", "content": "question %d %s" % (t, "q" * 1200)})
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": "reading file %d" % t},
            {"type": "tool_use", "id": "tu%d" % t, "name": "read", "input": {"path": "f%d.py" % t}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu%d" % t, "content": "x" * 1200}]})
        msgs.append({"role": "assistant", "content": "answer %d %s" % (t, "a" * 300)})
    return msgs


def post_chat(g, messages):
    r = client(g).post("/v1/chat/completions", json={"model": "m", "messages": messages})
    assert r.status_code == 200
    return r


def post_messages(g, messages):
    r = client(g).post("/v1/messages", json={"model": "m", "max_tokens": 10, "system": "be brief",
                                             "messages": messages})
    assert r.status_code == 200
    return r


def spans(g):
    run = Path(g.SPAN_DIR) / g.RUN_ID
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("[0-9]*.json"))]


def assert_no_span_repeats_a_message(g):
    seen = {}
    for span in spans(g):
        for m in span["messages"]:
            h = cg_archive.message_hash(m)
            assert h not in seen, "span %s repeats a message span %s already holds" % (span["index"], seen[h])
            seen[h] = span["index"]


# (a) -------------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["chat", "messages"])
def test_a_three_request_probe_summarises_once_then_extends(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g)
    history = chat_history if route == "chat" else messages_history
    post = post_chat if route == "chat" else post_messages
    base = 6 if route == "chat" else 5
    for extra in range(3):
        post(g, history(base + extra))
    full = [s for s in seen["summaries"] if "[earlier summary]" not in json.dumps(s)]
    assert len(seen["summaries"]) <= 3 and len(full) == 1, (len(seen["summaries"]), len(full))
    assert g._state["summaries_generated"] == 1
    assert g._state["summaries_extended"] + g._state["summaries_reused"] == 2
    assert_no_span_repeats_a_message(g)
    if g._state["summaries_extended"]:
        later = [s for s in spans(g) if s.get("continues")]
        assert later and all(s["continues"].endswith(".json") for s in later)


# (b) -------------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["chat", "messages"])
def test_b_an_identical_resend_makes_no_summary_call_and_no_span(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g)
    history = chat_history(6) if route == "chat" else messages_history(5)
    post = post_chat if route == "chat" else post_messages
    post(g, history)
    calls, nspans = len(seen["summaries"]), len(spans(g))
    assert calls == 1 and nspans == 1
    post(g, history)
    assert len(seen["summaries"]) == calls and len(spans(g)) == nspans
    assert g._state["summaries_reused"] == 1
    assert g._state["compactions_performed"] == 2       # still logged: the request is still smaller
    assert g._state["tokens_saved_total"] > 0


# (c) -------------------------------------------------------------------------------
def test_c_an_edited_earlier_message_forces_a_full_summary(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g)
    post_chat(g, chat_history(6))
    edited = chat_history(7)
    edited[1] = {"role": "user", "content": "question 0 EDITED " + "q" * 1100}
    post_chat(g, edited)
    full = [s for s in seen["summaries"] if "[earlier summary]" not in json.dumps(s)]
    assert len(full) == 2 and g._state["summaries_generated"] == 2


# (d) -------------------------------------------------------------------------------
def test_d_memo_off_restores_todays_behaviour(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env(
        GUARDIAN_SUMMARY_MEMO="0", GUARDIAN_NUM_CTX="9000", GUARDIAN_COMPACT_THRESHOLD="0.4"))
    seen = stub(g)
    for extra in range(3):
        post_chat(g, chat_history(6 + extra))
    assert len(seen["summaries"]) == 3
    assert all("[earlier summary]" not in json.dumps(s) for s in seen["summaries"])
    assert len(spans(g)) == 3 and not any(s.get("continues") for s in spans(g))
    assert g._state["summaries_generated"] == 3


# (e) -------------------------------------------------------------------------------
def test_e_a_reused_summary_still_makes_valid_requests_on_both_routes(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g)
    h = messages_history(5)
    post_messages(g, h)
    post_messages(g, h)                                  # reused
    assert g._state["summaries_reused"] == 1
    for body in seen["messages"]:
        msgs = body["messages"]
        assert msgs[0]["role"] == "user"
        assert not (isinstance(msgs[0]["content"], list)
                    and any(b.get("type") == "tool_result" for b in msgs[0]["content"]))
        uses = set()
        for m in msgs:
            for b in m["content"] if isinstance(m["content"], list) else []:
                if b.get("type") == "tool_use":
                    uses.add(b["id"])
                if b.get("type") == "tool_result":
                    assert b["tool_use_id"] in uses, "tool_result without its tool_use"
        assert SUMMARY in json.dumps(body["system"])
    assert seen["messages"][0] == seen["messages"][1]    # reuse forwards the same request

    c = chat_history(6)
    post_chat(g, c)
    post_chat(g, c)
    assert seen["chat"][-1] == seen["chat"][-2]
    assert SUMMARY in seen["chat"][-1]["messages"][1]["content"]


# (f) -------------------------------------------------------------------------------
def test_f_verify_stays_ok_across_reused_and_extended_spans(monkeypatch, tmp_path, capsys):
    g = load(monkeypatch, tmp_path, **_probe_env())
    stub(g)
    for extra in (0, 0, 1, 2, 2):
        post_chat(g, chat_history(6 + extra))
    assert g._state["summaries_reused"] >= 1 and g._state["summaries_extended"] >= 1
    assert cg_recall_cli.main(["--verify", "--run", g.RUN_ID]) == 0, capsys.readouterr().out


def test_the_memo_is_bounded(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env(GUARDIAN_SUMMARY_MEMO="2"))
    stub(g)
    for start in range(4):                               # four unrelated conversations
        h = chat_history(6)
        h[1] = {"role": "user", "content": "conversation %d " % start + "q" * 1100}
        post_chat(g, h)
    assert len(g._summary_memo) == 2


def test_counters_are_in_stats_and_receipts(monkeypatch, tmp_path):
    receipts = tmp_path / "r.jsonl"
    g = load(monkeypatch, tmp_path, **_probe_env(GUARDIAN_RECEIPTS_PATH=str(receipts)))
    stub(g)
    post_chat(g, chat_history(6))
    post_chat(g, chat_history(6))
    post_chat(g, chat_history(7))
    stats = client(g).get("/guardian/stats").json()
    assert (stats["summaries_generated"], stats["summaries_reused"], stats["summaries_extended"]) == (1, 1, 1)
    events = [json.loads(ln) for ln in receipts.read_text(encoding="utf-8").splitlines()]
    kinds = [(e.get("reused"), e.get("extended")) for e in events if e["tool"] == "proxy.compaction"]
    assert kinds == [(False, False), (True, False), (False, True)]
    summary = cg_archive.receipts_summary(cg_archive.read_receipts(receipts), Path(g.SPAN_DIR))
    assert (summary["summaries_generated"], summary["summaries_reused"], summary["summaries_extended"]) == (1, 1, 1)


def test_a_memo_error_means_no_memo(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g)
    post_chat(g, chat_history(6))

    def boom(*a, **k):
        raise RuntimeError("memo broke")
    monkeypatch.setattr(g, "memo_lookup", boom)
    post_chat(g, chat_history(6))
    assert len(seen["summaries"]) == 2                   # fell back to a full summary
    assert g._state["summaries_generated"] == 2


# --- follow-up 2, item 4: concurrent identical compactions --------------------------
# _memo_summary looked the memo up, awaited the summariser and only then stored,
# so two requests with the same evicted range in flight together each made a
# summariser call and wrote a duplicate span. Written before the code.
async def _concurrently(g, route, n=2):
    path, body = (("/v1/chat/completions", {"model": "m", "messages": chat_history(6)}) if route == "chat" else
                  ("/v1/messages", {"model": "m", "max_tokens": 10, "system": "be brief",
                                    "messages": messages_history(5)}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=g.app), base_url=LOOPBACK) as c:
        return await asyncio.gather(*[c.post(path, json=body) for _ in range(n)])


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["chat", "messages"])
async def test_two_concurrent_identical_compactions_make_one_summary_and_one_span(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g, delay=0.2)
    replies = await _concurrently(g, route)
    assert [r.status_code for r in replies] == [200, 200]
    assert len(seen["summaries"]) == 1
    assert len(spans(g)) == 1
    forwarded = [f for f in seen[route] if "messages" in f]            # not the /api/show probe
    assert len(forwarded) == 2 and forwarded[0] == forwarded[1]
    assert all(SUMMARY in json.dumps(f) for f in forwarded)            # both compacted
    assert (g._state["summaries_generated"], g._state["summaries_reused"]) == (1, 1)
    assert g._summary_inflight == {}                                    # cleaned up


@pytest.mark.asyncio
async def test_a_failed_shared_summary_forwards_both_unchanged(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g, delay=0.2, fail=True)
    replies = await _concurrently(g, "chat", n=3)
    assert [r.status_code for r in replies] == [200] * 3
    assert len(seen["summaries"]) == 1
    assert spans(g) == []
    forwarded = [f for f in seen["chat"] if "messages" in f]
    assert len(forwarded) == 3 and all(f["messages"] == chat_history(6) for f in forwarded)  # all unchanged
    assert g._state["summaries_rejected_empty"] == 3
    assert g._summary_inflight == {}


@pytest.mark.asyncio
async def test_a_de_dup_error_means_summarise_alone(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    seen = stub(g, delay=0.1)

    class Broken(dict):
        def get(self, *a, **k):
            raise RuntimeError("de-dup broke")
    monkeypatch.setattr(g, "_summary_inflight", Broken())
    replies = await _concurrently(g, "chat")
    assert [r.status_code for r in replies] == [200, 200]
    assert len(seen["summaries"]) == 2                                  # as before the de-dup
    assert all(SUMMARY in json.dumps(f) for f in seen["chat"] if "messages" in f)


@pytest.mark.asyncio
async def test_the_in_flight_table_is_bounded(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_probe_env())
    stub(g, delay=0.1)
    monkeypatch.setattr(g, "SUMMARY_INFLIGHT_MAX", 0)
    seen_sizes = []
    real = g._memo_summary

    async def spy(*a, **k):
        out = await real(*a, **k)
        seen_sizes.append(len(g._summary_inflight))
        return out
    monkeypatch.setattr(g, "_memo_summary", spy)
    await _concurrently(g, "chat")
    assert seen_sizes and max(seen_sizes) == 0
