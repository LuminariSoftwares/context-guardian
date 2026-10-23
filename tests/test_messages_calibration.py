"""Follow-up item 3: /v1/messages calibrates like /v1/chat/completions.

usage_prompt_tokens() knew the OpenAI usage block, Ollama's prompt_eval_count
and llama.cpp's timings, and returned None for the Messages shape --
{"usage": {"input_tokens": 1234, "cache_read_input_tokens": 500}} -- and the
/v1/messages route had no calibration tap at all, so on that route the
per-model estimate never corrected itself. Written before the code.
"""
import json

import httpx
import pytest

from _proxy_stub import LOOPBACK, load


@pytest.fixture()
def g(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_CALIBRATE", raising=False)
    return load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="100000", GUARDIAN_OLLAMA_CONTEXT="0")


def test_the_messages_usage_block_parses_to_the_prompt_total(g):
    body = b'{"usage":{"input_tokens":1234,"cache_read_input_tokens":500}}'
    assert g.usage_prompt_tokens(body, "application/json") == 1734
    full = {"usage": {"input_tokens": 1234, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 66, "output_tokens": 9}}
    assert g.usage_prompt_tokens(json.dumps(full).encode(), "application/json") == 1800
    assert g.usage_prompt_tokens(b'{"usage":{"input_tokens":42}}', "application/json") == 42


def test_the_openai_shapes_are_unchanged(g):
    assert g.usage_prompt_tokens(b'{"usage":{"prompt_tokens":77,"completion_tokens":3}}', "application/json") == 77
    assert g.usage_prompt_tokens(b'{"usage":{"output_tokens":5}}', "application/json") is None


SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"m1","role":"assistant","content":[],'
    b'"usage":{"input_tokens":2000,"cache_read_input_tokens":100,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


def test_a_streamed_messages_response_reports_its_input_usage(g):
    """The input usage is in message_start, at the TOP of the stream; the last
    frame carries only output_tokens and must not end the search."""
    assert g.usage_prompt_tokens(SSE, "text/event-stream") == 2100


def _wire_messages(g, chunks, content_type="text/event-stream"):
    upstream = {"bytes": b""}

    def handler(request):
        if not request.url.path.endswith("/messages"):
            return httpx.Response(404, json={"error": "not here"})

        async def _stream():
            for c in chunks:
                upstream["bytes"] += c
                yield c
        return httpx.Response(200, headers={"content-type": content_type}, content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return upstream


def _post(g):
    from fastapi.testclient import TestClient
    # Big enough that the estimate (>= 256 tokens) is a sample worth learning from.
    body = {"model": "claude-local", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "x" * 4000}]}
    return TestClient(g.app, base_url=LOOPBACK).post("/v1/messages", json=body)


def test_a_streamed_messages_response_feeds_the_calibrator(g):
    _wire_messages(g, [SSE[:90], SSE[90:200], SSE[200:]])
    assert g.CALIBRATOR.samples("claude-local") == 0
    assert _post(g).status_code == 200
    assert g.CALIBRATOR.samples("claude-local") == 1


def test_the_response_bytes_are_unchanged_with_the_tap_on(g):
    upstream = _wire_messages(g, [SSE[:37], SSE[37:111], SSE[111:]])
    resp = _post(g)
    assert resp.content == upstream["bytes"] == SSE


def test_a_non_streamed_messages_response_feeds_it_too(g):
    reply = json.dumps({"content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1500, "output_tokens": 2}}).encode()
    upstream = _wire_messages(g, [reply], "application/json")
    resp = _post(g)
    assert resp.content == upstream["bytes"]
    assert g.CALIBRATOR.samples("claude-local") == 1


def test_calibrate_off_leaves_the_route_untapped(g, monkeypatch):
    monkeypatch.setenv("GUARDIAN_CALIBRATE", "0")
    upstream = _wire_messages(g, [SSE])
    resp = _post(g)
    assert resp.content == upstream["bytes"]
    assert g.CALIBRATOR.samples("claude-local") == 0


@pytest.mark.asyncio
async def test_the_messages_estimate_applies_the_learned_factor(g):
    """Feeding the calibrator is half of it: the /v1/messages estimate must
    also be multiplied by the factor, as chat/completions' is."""
    for _ in range(g.CALIBRATION_MIN_SAMPLES):
        g.CALIBRATOR.observe("claude-local", 1000, 1250)       # the backend reads 25% more
    payload = {"model": "claude-local", "max_tokens": 10, "messages": [{"role": "user", "content": "x" * 3500}]}
    await g.maybe_compact_anthropic(client=None, payload=payload, headers={})
    raw = g._state["last_raw_estimate"]
    assert raw >= 1000
    assert g._state["last_known_total_tokens"] == int(raw * 1.25)
