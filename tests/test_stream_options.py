"""Item 5 (2026-10-14): opt-in stream_options injection.

A streamed OpenAI-style reply carries no usage block unless the request asked
for one, so the calibrator never learns from streaming clients. With
GUARDIAN_INJECT_STREAM_OPTIONS=1 the proxy asks on the client's behalf. Off by
default, because a client that did not ask for the extra usage frame may not
expect it; never added over the client's own stream_options.
"""
import json

import pytest

from _proxy_stub import load, post, wire


def _body(**extra):
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


def test_injected_on_a_streaming_request_when_enabled(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_INJECT_STREAM_OPTIONS="1")
    seen = wire(g)
    post(g, _body(stream=True))
    sent = json.loads(seen["chat"][0])
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_off_by_default(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    body = _body(stream=True)
    post(g, body)
    assert seen["chat"] == [body]


def test_never_added_over_the_clients_own(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_INJECT_STREAM_OPTIONS="1")
    seen = wire(g)
    body = _body(stream=True, stream_options={"include_usage": False})
    post(g, body)
    assert seen["chat"] == [body]


@pytest.mark.parametrize("stream", [None, False])
def test_not_added_to_a_non_streaming_request(monkeypatch, tmp_path, stream):
    g = load(monkeypatch, tmp_path, GUARDIAN_INJECT_STREAM_OPTIONS="1")
    seen = wire(g)
    body = _body() if stream is None else _body(stream=stream)
    post(g, body)
    assert seen["chat"] == [body]


def test_a_failing_injection_forwards_the_original(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_INJECT_STREAM_OPTIONS="1")
    seen = wire(g)

    def boom(payload):
        raise RuntimeError("injection bug")
    monkeypatch.setattr(g, "with_stream_options", boom)
    body = _body(stream=True)
    assert post(g, body).status_code == 200
    assert seen["chat"] == [body]
