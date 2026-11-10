"""A. Empty / degenerate-turn retry (GUARDIAN_EMPTY_RETRY).

Written before the implementation. Behaviour under test, in the words of the brief:
when the upstream returns a completed turn with no visible content (empty text, or only an
echo of the compaction tag the proxy injected), the SAME request is re-issued once with a
short nudge appended as trailing user text and the retry's result is spliced into the
response. One retry per turn at most; the nudge is never persisted or replayed; streaming
and non-streaming alike; default off; and every failure of the feature forwards what the
upstream said, untouched (ADR 0001).
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from _proxy_stub import LOOPBACK, load

GOOD = "here is the real answer"
MARKER = "[Context Guardian auto-compaction"


# ---- stubs ------------------------------------------------------------------------

def chat_json(content, finish="stop", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"id": "x", "object": "chat.completion",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}


def sse(*objs):
    out = b""
    for o in objs:
        out += b"data: " + (o if isinstance(o, bytes) else json.dumps(o).encode()) + b"\n\n"
    return out


def delta(**d):
    return {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def finish(reason="stop"):
    return {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def stream_of(*texts, reasoning=None):
    frames = [delta(role="assistant", content="")]
    if reasoning:
        frames.append(delta(reasoning_content=reasoning))
    frames += [delta(content=t) for t in texts]
    frames.append(finish())
    return sse(*frames) + b"data: [DONE]\n\n"


class R:
    """An upstream reply. Built fresh per call, with a generator body: an httpx.Response made
    from bytes is already 'consumed' and cannot be streamed through the proxy."""

    def __init__(self, status, body, ctype="application/json", **headers):
        self.status, self.body, self.headers = status, body, {"content-type": ctype, **headers}

    def make(self):
        body = self.body

        async def gen():
            yield body
        return httpx.Response(self.status, headers=self.headers, content=gen())


def wire_seq(g, responses):
    """Answer the n-th non-summary chat call with responses[n] (the last repeats).
    A response is bytes (SSE), a dict (JSON) or a ready httpx.Response."""
    seen = []

    def handler(request):
        if not request.url.path.endswith(("/chat/completions", "/messages")):
            return httpx.Response(404, json={"error": "not found"})      # window probes
        body = request.content
        seen.append(body)
        r = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(r, R):
            return r.make()
        if isinstance(r, bytes):
            return R(200, r, "text/event-stream").make()
        return R(200, json.dumps(r).encode(), "application/json").make()

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def req(stream=False, **extra):
    body = {"model": "m", "messages": [{"role": "system", "content": "be brief"},
                                       {"role": "user", "content": "question one"}], "stream": stream}
    body.update(extra)
    return json.dumps(body).encode()


def post(g, body, path="/v1/chat/completions"):
    return TestClient(g.app, base_url=LOOPBACK).post(path, content=body, headers={"content-type": "application/json"})


@pytest.fixture
def on(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path, GUARDIAN_EMPTY_RETRY="1", GUARDIAN_NUM_CTX="100000")


@pytest.fixture
def off(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_EMPTY_RETRY", raising=False)
    return load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="100000")


def last_user_text(body):
    m = json.loads(body)["messages"][-1]
    assert m["role"] == "user"
    c = m["content"]
    return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c)


# ---- pure helpers -----------------------------------------------------------------

def test_openai_emptiness():
    import cg_empty_retry as e
    empty = e.response_is_empty
    assert empty("chat", chat_json(""))
    assert empty("chat", chat_json("  \n "))
    assert empty("chat", chat_json(None))
    assert empty("chat", chat_json(MARKER + " — earlier conversation (3 messages) condensed"))
    assert not empty("chat", chat_json(GOOD))
    assert not empty("chat", chat_json("", tool_calls=[{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]))
    assert not empty("chat", chat_json("", finish="length"))       # ran out of room: a retry cannot fix that
    assert not empty("chat", chat_json("", finish="content_filter"))
    assert not empty("chat", {"choices": []})
    assert not empty("chat", {"error": "x"})
    assert not empty("chat", [1, 2])
    assert not empty("chat", None)


def test_anthropic_emptiness():
    import cg_empty_retry as e
    def msg(blocks, stop="end_turn"):
        return {"type": "message", "role": "assistant", "content": blocks, "stop_reason": stop}
    assert e.response_is_empty("messages", msg([]))
    assert e.response_is_empty("messages", msg([{"type": "text", "text": ""}]))
    assert e.response_is_empty("messages", msg([{"type": "thinking", "thinking": "hmm", "signature": "s"}]))
    assert e.response_is_empty("messages", msg([{"type": "text", "text": MARKER + " echoed"}]))
    assert not e.response_is_empty("messages", msg([{"type": "text", "text": GOOD}]))
    assert not e.response_is_empty("messages", msg([{"type": "tool_use", "id": "t", "name": "f", "input": {}}], "tool_use"))
    assert not e.response_is_empty("messages", msg([], "max_tokens"))
    assert not e.response_is_empty("messages", {"type": "error", "error": {}})


def test_nudge_is_appended_to_a_copy():
    import cg_empty_retry as e
    original = json.loads(req())
    snapshot = json.dumps(original)
    out = e.nudged_payload("chat", original, "NUDGE")
    assert json.dumps(original) == snapshot, "the caller's payload must not be touched"
    assert out["messages"][-1] == {"role": "user", "content": "NUDGE"}
    assert out["messages"][:-1] == original["messages"]

    a = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    out = e.nudged_payload("messages", a, "NUDGE")
    assert a["messages"][0]["content"] == "hi"
    assert out["messages"][-1]["role"] == "user" and len(out["messages"]) == 1   # joined, roles still alternate
    assert out["messages"][-1]["content"] == [{"type": "text", "text": "hi"}, {"type": "text", "text": "NUDGE"}]
    a = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]}
    assert e.nudged_payload("messages", a, "NUDGE")["messages"][-1] == {"role": "user", "content": "NUDGE"}
    a = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]}]}
    last = e.nudged_payload("messages", a, "NUDGE")["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[-1] == {"type": "text", "text": "NUDGE"}


# ---- non-streaming ------------------------------------------------------------------

def test_default_is_off(off):
    seen = wire_seq(off, [chat_json("")])
    r = post(off, req())
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == ""
    assert len(seen) == 1


def test_empty_turn_is_retried_once_and_spliced(on):
    seen = wire_seq(on, [chat_json(""), chat_json(GOOD)])
    body = req()
    r = post(on, body)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == GOOD
    assert len(seen) == 2
    assert seen[0] == body, "the first attempt is the client's own bytes"
    assert last_user_text(seen[1]) == on.EMPTY_RETRY_NUDGE
    # the retry is the same request plus the nudge, nothing else changed
    first, second = json.loads(seen[0]), json.loads(seen[1])
    assert second["messages"][:-1] == first["messages"]
    assert {k: v for k, v in second.items() if k != "messages"} == {k: v for k, v in first.items() if k != "messages"}


def test_a_good_turn_is_forwarded_byte_for_byte(on):
    seen = wire_seq(on, [chat_json(GOOD)])
    r = post(on, req())
    assert r.content == json.dumps(chat_json(GOOD)).encode()
    assert len(seen) == 1


def test_echo_of_the_compaction_tag_is_degenerate(on):
    seen = wire_seq(on, [chat_json(MARKER + " — earlier conversation (4 messages) condensed to stay within the context window:]"), chat_json(GOOD)])
    r = post(on, req())
    assert r.json()["choices"][0]["message"]["content"] == GOOD and len(seen) == 2


def test_at_most_one_retry_per_turn(on):
    seen = wire_seq(on, [chat_json(""), chat_json("")])
    r = post(on, req())
    assert len(seen) == 2
    assert r.json() == chat_json("")          # the second empty answer is simply returned


def test_tool_calls_and_length_are_not_retried(on):
    calls = [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    seen = wire_seq(on, [chat_json("", finish="tool_calls", tool_calls=calls)])
    post(on, req())
    assert len(seen) == 1
    seen = wire_seq(on, [chat_json("", finish="length")])
    post(on, req())
    assert len(seen) == 1


def test_nudge_is_never_persisted_or_replayed(on):
    seen = wire_seq(on, [chat_json(""), chat_json(GOOD), chat_json(GOOD)])
    body = req()
    post(on, body)
    post(on, body)                              # the client's next request: same history, no nudge
    assert len(seen) == 3
    assert on.EMPTY_RETRY_NUDGE.encode() not in seen[0]
    assert on.EMPTY_RETRY_NUDGE.encode() not in seen[2]
    assert seen[2] == body
    assert on.EMPTY_RETRY_NUDGE not in json.dumps(on._state, default=str)
    assert on.EMPTY_RETRY_NUDGE not in json.dumps(dict(on._usage_sessions), default=str)
    assert on.EMPTY_RETRY_NUDGE not in json.dumps(post.__globals__["TestClient"](on.app, base_url=LOOPBACK).get("/guardian/stats").json())


def test_counters(on):
    wire_seq(on, [chat_json(""), chat_json(GOOD), chat_json(""), chat_json("")])
    post(on, req())
    post(on, req())
    s = TestClient(on.app, base_url=LOOPBACK).get("/guardian/stats").json()
    assert s["empty_retries"] == 2 and s["empty_retry_recovered"] == 1


def test_dry_run_never_retries(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_EMPTY_RETRY="1", GUARDIAN_DRY_RUN="1")
    seen = wire_seq(g, [chat_json("")])
    post(g, req())
    assert len(seen) == 1


def test_compressed_upstream_bodies_are_left_alone(on):
    r = R(200, json.dumps(chat_json("")).encode(), "application/json", **{"content-encoding": "identity-x"})
    seen = wire_seq(on, [r, chat_json(GOOD)])
    post(on, req())
    assert len(seen) == 1


# ---- non-streaming fail-open (invariant 1) -------------------------------------------

def test_retry_upstream_error_returns_the_original(on):
    seen = wire_seq(on, [chat_json(""), R(500, b'{"error": "boom"}')])
    r = post(on, req())
    assert r.status_code == 200 and r.json() == chat_json("")
    assert len(seen) == 2


def test_retry_transport_failure_returns_the_original(on):
    state = {"n": 0}

    def handler(request):
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "not found"})
        state["n"] += 1
        if state["n"] == 1:
            return R(200, json.dumps(chat_json("")).encode()).make()
        raise httpx.ConnectError("down")
    on._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = post(on, req())
    assert r.status_code == 200 and r.json() == chat_json("")


def test_a_bug_in_the_feature_returns_the_original(on, monkeypatch):
    import cg_empty_retry as e
    seen = wire_seq(on, [chat_json(""), chat_json(GOOD)])

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(e, "response_is_empty", boom)
    r = post(on, req())
    assert r.status_code == 200 and r.json() == chat_json("")
    assert len(seen) == 1
    monkeypatch.undo()


def test_nudge_builder_failure_returns_the_original(on, monkeypatch):
    import cg_empty_retry as e
    seen = wire_seq(on, [chat_json(""), chat_json(GOOD)])
    monkeypatch.setattr(e, "nudged_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    r = post(on, req())
    assert r.json() == chat_json("") and len(seen) == 1


def test_unparseable_body_passes_through(on):
    r0 = R(200, b"not json at all")
    seen = wire_seq(on, [r0])
    r = post(on, req())
    assert r.content == b"not json at all" and len(seen) == 1


def test_upstream_error_status_passes_through(on):
    seen = wire_seq(on, [R(400, b'{"error": "bad"}')])
    r = post(on, req())
    assert r.status_code == 400 and len(seen) == 1


# ---- streaming ----------------------------------------------------------------------

def test_stream_empty_turn_is_retried_and_spliced(on):
    seen = wire_seq(on, [stream_of(), stream_of(GOOD)])
    r = post(on, req(stream=True))
    assert len(seen) == 2
    text = r.content.decode()
    assert GOOD in text
    assert text.count("[DONE]") == 1
    assert json.loads(seen[1])["stream"] is True
    assert last_user_text(seen[1]) == on.EMPTY_RETRY_NUDGE
    assert on.EMPTY_RETRY_NUDGE not in text


def test_stream_good_turn_is_byte_identical(on):
    raw = stream_of("hello ", "world")
    seen = wire_seq(on, [raw])
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1


def test_stream_reasoning_only_turn_is_empty(on):
    seen = wire_seq(on, [stream_of(reasoning="thinking hard"), stream_of(GOOD)])
    r = post(on, req(stream=True))
    assert len(seen) == 2 and GOOD in r.content.decode()
    assert "thinking hard" in r.content.decode()      # reasoning is forwarded as it arrives, never held


def test_stream_echo_of_the_tag_is_degenerate(on):
    pieces = [MARKER[:10], MARKER[10:], " — earlier conversation condensed"]
    seen = wire_seq(on, [stream_of(*pieces), stream_of(GOOD)])
    r = post(on, req(stream=True))
    text = r.content.decode()
    assert len(seen) == 2 and GOOD in text
    assert "earlier conversation condensed" not in text, "the echo is dropped, not shown"


def test_stream_text_that_only_starts_like_the_tag_is_released(on):
    raw = stream_of("[Context", " switch] is what I would call this", " pattern")
    seen = wire_seq(on, [raw])
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1


def test_stream_both_empty_returns_the_original_bytes(on):
    raw = stream_of()
    seen = wire_seq(on, [raw, raw])
    r = post(on, req(stream=True))
    assert len(seen) == 2 and r.content == raw


def test_stream_tool_call_is_visible(on):
    frames = sse(delta(role="assistant", content=""),
                 delta(tool_calls=[{"index": 0, "id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
                 finish("tool_calls")) + b"data: [DONE]\n\n"
    seen = wire_seq(on, [frames])
    r = post(on, req(stream=True))
    assert r.content == frames and len(seen) == 1


def test_stream_length_finish_is_not_retried(on):
    raw = sse(delta(role="assistant", content=""), finish("length")) + b"data: [DONE]\n\n"
    seen = wire_seq(on, [raw])
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1


def test_stream_retry_failure_returns_the_original_bytes(on):
    raw = stream_of()
    seen = wire_seq(on, [raw, R(503, b'{"error": "x"}')])
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 2


def test_stream_scanner_bug_degrades_to_passthrough(on, monkeypatch):
    import cg_empty_retry as e
    raw = stream_of()
    seen = wire_seq(on, [raw, stream_of(GOOD)])

    def boom(self, frame):
        raise RuntimeError("injected")
    monkeypatch.setattr(e.StreamScan, "classify", boom)
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1
    monkeypatch.undo()


def test_stream_retry_sender_bug_returns_the_original_bytes(on, monkeypatch):
    import cg_empty_retry as e
    raw = stream_of()
    seen = wire_seq(on, [raw, stream_of(GOOD)])
    monkeypatch.setattr(e, "nudged_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1


def test_stream_frames_split_across_chunks(on):
    raw = stream_of(GOOD)
    cut = len(raw) // 3

    async def pieces():
        yield raw[:cut]
        yield raw[cut:cut * 2]
        yield raw[cut * 2:]

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=pieces())
    on._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = post(on, req(stream=True))
    assert r.content == raw


# ---- Anthropic route ----------------------------------------------------------------

def a_msg(blocks, stop="end_turn"):
    return {"id": "m", "type": "message", "role": "assistant", "model": "m", "content": blocks,
            "stop_reason": stop, "usage": {"input_tokens": 4, "output_tokens": 1}}


def a_req(stream=False):
    return json.dumps({"model": "m", "max_tokens": 50, "stream": stream,
                       "messages": [{"role": "user", "content": "question one"}]}).encode()


def a_sse(events):
    return b"".join(b"event: " + t.encode() + b"\ndata: " + json.dumps(o).encode() + b"\n\n" for t, o in events)


def a_stream(text=None, thinking=False):
    ev = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 4, "output_tokens": 0}}})]
    idx = 0
    if thinking:
        ev += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 0})]
        idx = 1
    if text is not None:
        ev += [("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})]
        if text:
            ev += [("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}})]
        ev += [("content_block_stop", {"type": "content_block_stop", "index": idx})]
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
           ("message_stop", {"type": "message_stop"})]
    return a_sse(ev)


def test_anthropic_non_stream_retry(on):
    seen = wire_seq(on, [a_msg([{"type": "text", "text": ""}]), a_msg([{"type": "text", "text": GOOD}])])
    r = post(on, a_req(), "/v1/messages")
    assert r.json()["content"][0]["text"] == GOOD and len(seen) == 2
    assert json.loads(seen[0]) == json.loads(a_req())
    last = json.loads(seen[1])["messages"][-1]
    assert last["role"] == "user" and last["content"][-1] == {"type": "text", "text": on.EMPTY_RETRY_NUDGE}


def test_anthropic_stream_retry_renumbers_blocks(on):
    seen = wire_seq(on, [a_stream(text="", thinking=True), a_stream(text=GOOD)])
    r = post(on, a_req(stream=True), "/v1/messages")
    text = r.content.decode()
    assert len(seen) == 2 and GOOD in text
    assert text.count("event: message_start") == 1 and text.count("event: message_stop") == 1
    events = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1], "the spliced text block follows the thinking block"
    deltas = [e for e in events if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"]
    assert deltas and deltas[0]["index"] == 1


def test_anthropic_stream_good_turn_is_byte_identical(on):
    raw = a_stream(text=GOOD)
    seen = wire_seq(on, [raw])
    r = post(on, a_req(stream=True), "/v1/messages")
    assert r.content == raw and len(seen) == 1


def test_anthropic_stream_both_empty_returns_original(on):
    raw = a_stream(text="")
    seen = wire_seq(on, [raw, raw])
    r = post(on, a_req(stream=True), "/v1/messages")
    assert r.content == raw and len(seen) == 2


def test_marker_matches_the_one_the_proxy_injects(on):
    import cg_empty_retry as e
    assert e.MARKER == on.GUARDIAN_SUMMARY_MARKER


# ---- review round 1 (reviewer findings turned into tests) ------------------------------

def test_refusal_is_an_answer_not_an_empty_turn(on):
    refusal = chat_json(None)
    refusal["choices"][0]["message"]["refusal"] = "I can't help with that"
    seen = wire_seq(on, [refusal, chat_json(GOOD)])
    r = post(on, req())
    assert len(seen) == 1 and r.json() == refusal


def test_header_echo_followed_by_an_answer_is_an_answer(on):
    said = MARKER + " — earlier conversation (2 messages) condensed:]\n\nThe answer is 42."
    seen = wire_seq(on, [chat_json(said), chat_json(GOOD)])
    r = post(on, req())
    assert len(seen) == 1 and r.json()["choices"][0]["message"]["content"] == said
    raw = stream_of(MARKER + " — earlier ", "conversation condensed:]", "\n\nThe answer ", "is 42.")
    seen = wire_seq(on, [raw, stream_of(GOOD)])
    r = post(on, req(stream=True))
    assert len(seen) == 1 and r.content == raw


def test_stream_retry_drops_the_duplicate_role_frame(on):
    wire_seq(on, [stream_of(), stream_of(GOOD)])
    text = post(on, req(stream=True)).content.decode()
    assert text.count('"role"') == 1


def test_sse_with_an_odd_content_type_still_streams_and_is_not_an_error(on):
    raw = stream_of("hello")
    seen = wire_seq(on, [R(200, raw, "text/plain")])
    r = post(on, req(stream=True))
    assert r.content == raw and len(seen) == 1
    assert on._state["empty_retry_errors"] == 0


def test_a_plain_non_json_body_is_not_counted_as_an_error(on):
    wire_seq(on, [R(200, b"plain words")])
    post(on, req())
    assert on._state["empty_retry_errors"] == 0


def test_degenerate_stream_split_mid_separator_and_with_crlf(on):
    raw = stream_of().replace(b"\n\n", b"\r\n\r\n")
    cut = raw.index(b"\r\n\r\n") + 2            # inside the first separator

    async def pieces():
        yield raw[:cut]
        yield raw[cut:]

    state = {"n": 0}

    def handler(request):
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "x"})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=pieces())
        return R(200, stream_of(GOOD), "text/event-stream").make()
    on._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = post(on, req(stream=True))
    assert state["n"] == 2 and GOOD in r.content.decode()


def test_anthropic_failure_injections_return_the_original(on, monkeypatch):
    import cg_empty_retry as e
    empty = a_msg([{"type": "text", "text": ""}])
    # retry upstream error
    seen = wire_seq(on, [empty, R(500, b'{"error": "x"}')])
    r = post(on, a_req(), "/v1/messages")
    assert r.json() == empty and len(seen) == 2
    # nudge builder bug, plain and streaming
    monkeypatch.setattr(e, "nudged_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    seen = wire_seq(on, [empty, a_msg([{"type": "text", "text": GOOD}])])
    assert post(on, a_req(), "/v1/messages").json() == empty and len(seen) == 1
    raw = a_stream(text="")
    seen = wire_seq(on, [raw, a_stream(text=GOOD)])
    assert post(on, a_req(stream=True), "/v1/messages").content == raw and len(seen) == 1
    monkeypatch.undo()
    # scanner bug
    monkeypatch.setattr(e.StreamScan, "classify", lambda self, frame: (_ for _ in ()).throw(RuntimeError("injected")))
    seen = wire_seq(on, [raw, a_stream(text=GOOD)])
    assert post(on, a_req(stream=True), "/v1/messages").content == raw and len(seen) == 1
    monkeypatch.undo()


def test_anthropic_nudge_is_never_replayed(on):
    seen = wire_seq(on, [a_msg([{"type": "text", "text": ""}]), a_msg([{"type": "text", "text": GOOD}]), a_msg([{"type": "text", "text": GOOD}])])
    post(on, a_req(), "/v1/messages")
    post(on, a_req(), "/v1/messages")
    assert seen[2] == a_req() and on.EMPTY_RETRY_NUDGE.encode() not in seen[2]


def test_a_retry_that_is_not_positively_an_answer_never_replaces_the_original(on):
    empty = chat_json("")
    for bad in ({"error": {"message": "model unloaded"}}, chat_json("", finish="length"), {"choices": []}, {"id": "x"}):
        seen = wire_seq(on, [empty, bad])
        r = post(on, req())
        assert r.json() == empty and len(seen) == 2, bad
    a_empty = a_msg([{"type": "text", "text": ""}])
    seen = wire_seq(on, [a_empty, {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}])
    assert post(on, a_req(), "/v1/messages").json() == a_empty and len(seen) == 2
    assert on._state["empty_retry_recovered"] == 0


def test_has_answer_shapes():
    import cg_empty_retry as e
    assert e.response_has_answer("chat", chat_json(GOOD))
    assert e.response_has_answer("chat", chat_json("", finish="tool_calls", tool_calls=[{"id": "1"}]))
    assert not e.response_has_answer("chat", chat_json(""))
    assert not e.response_has_answer("chat", chat_json(GOOD, finish="length"))
    assert e.response_has_answer("messages", a_msg([{"type": "text", "text": GOOD}]))
    assert not e.response_has_answer("messages", a_msg([]))
    assert not e.response_has_answer("messages", {"type": "error"})
