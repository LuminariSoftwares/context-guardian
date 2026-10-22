"""Item 7 (2026-10-14): POST /v1/messages gets the same compaction as
chat/completions, on Anthropic-format conversations (top-level `system`,
messages whose content is a string or a list of blocks).

Fail-open is the rule here as everywhere: a failed summary or any exception
forwards the client's bytes untouched, and when nothing is compacted the
request AND the streamed response are byte-identical.
"""
import json

import httpx
import pytest

from _proxy_stub import LOOPBACK, SUMMARY, load

SSE = (b'event: message_start\ndata: {"type":"message_start"}\n\n',
       b'event: content_block_delta\ndata: {"type":"content_block_delta",'
       b'"delta":{"type":"text_delta","text":"hi"}}\n\n',
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


def _wire(g, summary=SUMMARY):
    seen = {"messages": [], "summary_calls": [], "headers": []}

    def handler(request):
        if request.url.path.endswith("/api/show"):
            return httpx.Response(404, json={"error": "not ollama"})
        body = request.content
        if b"BEGIN_TRANSCRIPT" in body:
            seen["summary_calls"].append(json.loads(body))
            seen["summary_headers"] = dict(request.headers)
            if summary is None:
                return httpx.Response(500, json={"error": "down"})
            return httpx.Response(200, json={"type": "message", "content": [
                {"type": "text", "text": summary}], "stop_reason": "end_turn"})
        seen["messages"].append(body)
        seen["headers"].append(dict(request.headers))

        async def _stream():
            for frame in SSE:
                yield frame
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def _conv(turns, size):
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": "question %d " % i + "q" * size})
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu%d" % i, "name": "read", "input": {"path": "f%d" % i}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu%d" % i, "content": "r" * size}]})
        msgs.append({"role": "assistant", "content": "answer %d" % i})
    msgs.append({"role": "user", "content": "the live question"})
    return msgs


def _body(messages, system="You are terse.", **extra):
    payload = {"model": "m", "max_tokens": 512, "messages": messages}
    if system is not None:
        payload["system"] = system
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


def _post(g, body, stream=False):
    from fastapi.testclient import TestClient
    headers = {"content-type": "application/json", "x-api-key": "local",
               "anthropic-version": "2023-06-01"}
    client = TestClient(g.app, base_url=LOOPBACK)
    if stream:
        with client.stream("POST", "/v1/messages", content=body, headers=headers) as r:
            return r.status_code, b"".join(r.iter_bytes())
    r = client.post("/v1/messages", content=body, headers=headers)
    return r.status_code, r.content


@pytest.fixture()
def g(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="4000",
                GUARDIAN_RESERVE_OUTPUT="500", GUARDIAN_COMPACT_THRESHOLD="0.8")


def test_an_over_budget_conversation_is_compacted(g):
    seen = _wire(g)
    msgs = _conv(6, 1500)
    status, _ = _post(g, _body(msgs))
    assert status == 200
    sent = json.loads(seen["messages"][0])
    assert len(sent["messages"]) < len(msgs)
    assert sent["messages"][-1] == msgs[-1]
    first = sent["messages"][0]
    assert first["role"] == "user"
    blocks = first["content"] if isinstance(first["content"], list) else []
    assert not any(b.get("type") == "tool_result" for b in blocks), \
        "the kept window starts with an orphaned tool_result"
    assert sent["system"].startswith("You are terse.")
    assert SUMMARY in sent["system"]
    assert g._state["compactions_performed"] == 1
    # the summary call went to the same Anthropic endpoint with the client's key
    assert seen["summary_calls"][0]["model"] == "m"
    assert seen["summary_headers"]["x-api-key"] == "local"
    assert seen["summary_headers"]["anthropic-version"] == "2023-06-01"


def test_a_system_block_list_gets_a_text_block(g):
    seen = _wire(g)
    system = [{"type": "text", "text": "You are terse.", "cache_control": {"type": "ephemeral"}}]
    _post(g, _body(_conv(6, 1500), system=system))
    sent = json.loads(seen["messages"][0])["system"]
    assert sent[0] == system[0]
    assert sent[-1]["type"] == "text" and SUMMARY in sent[-1]["text"]


def test_under_budget_stream_is_byte_identical_both_ways(g):
    seen = _wire(g)
    body = _body(_conv(1, 50), stream=True)
    status, received = _post(g, body, stream=True)
    assert status == 200
    assert seen["messages"] == [body], "the request was not forwarded byte-for-byte"
    assert received == b"".join(SSE), "the stream was not passed through byte-for-byte"
    assert seen["summary_calls"] == []


def test_a_failed_summary_forwards_the_original_bytes(g):
    seen = _wire(g, summary=None)
    body = _body(_conv(6, 1500), stream=True)
    status, received = _post(g, body, stream=True)
    assert status == 200
    assert seen["messages"] == [body]
    assert received == b"".join(SSE)
    assert g._state["compactions_performed"] == 0


def test_an_exception_forwards_the_original_bytes(g, monkeypatch):
    seen = _wire(g)

    async def boom(*a, **k):
        raise RuntimeError("anthropic compaction bug")
    monkeypatch.setattr(g, "maybe_compact_anthropic", boom)
    body = _body(_conv(6, 1500))
    assert _post(g, body)[0] == 200
    assert seen["messages"] == [body]


def test_hostile_bodies_pass_through(g):
    seen = _wire(g)
    for body in (b"not json", b"[]", b'{"messages":"nope"}', b'{"messages":[1,2,3]}'):
        assert _post(g, body)[0] == 200
    assert len(seen["messages"]) == 4


def test_dry_run_applies_to_messages_too(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="4000",
             GUARDIAN_RESERVE_OUTPUT="500", GUARDIAN_COMPACT_THRESHOLD="0.8",
             GUARDIAN_DRY_RUN="1")
    seen = _wire(g)
    body = _body(_conv(6, 1500))
    _post(g, body)
    assert seen["messages"] == [body]
    assert g._state["dry_runs"] == 1
