"""Follow-up 2, item 2: a detected window goes stale.

_detected was filled once per model for the life of the process. Restart vLLM
or llama-server with a SMALLER window under the same model name and the proxy
kept budgeting against the old, larger one: the overflow this tool exists to
prevent. Now a model's window is asked again (a) on the request after the
upstream rejected one for context length, and (b) when GUARDIAN_DETECT_TTL
seconds have passed. A re-probe happens at most once per model per TTL window,
on the request path of one request only; the proxy never retries the rejected
request itself. Written before the code.

The rejection texts are the servers' own:
- vLLM: get_max_tokens() in vllm/entrypoints/serve/utils/api_utils.py raises
  ValueError("Input length (N) exceeds model's maximum context length (M)."),
  which create_error_response() (serve/exception_handling/error_response.py)
  maps to 400 BadRequestError.
- llama.cpp: tools/server/server-context.cpp sends "request (N tokens) exceeds
  the available context size (M tokens), try increasing it" (and "input (N
  tokens) is larger than the max context size (M tokens). skipping") as
  ERROR_TYPE_EXCEED_CONTEXT_SIZE, which format_error_response()
  (server-common.cpp) turns into code 400, type "exceed_context_size_error".
- Ollama: llm/llama_server.go returns 400 promptTooLongMessage, "the prompt is
  longer than the context length currently available to the model; ...", and
  413 "the input length exceeds the context length" from embeddings.
"""
import asyncio
import json

import httpx
import pytest

from _proxy_stub import LOOPBACK, load

VLLM = "http://127.0.0.1:8000/v1"
OLLAMA = "http://127.0.0.1:11434/v1"

VLLM_TOO_LONG = {"error": {"message": "Input length (9000) exceeds model's maximum context length (4096).",
                           "type": "BadRequestError", "param": None, "code": 400}}
LLAMA_TOO_LONG = {"error": {"code": 400, "message": "request (9000 tokens) exceeds the available context size "
                                                    "(4096 tokens), try increasing it",
                            "type": "exceed_context_size_error", "n_prompt_tokens": 9000, "n_ctx": 4096}}
OLLAMA_TOO_LONG = {"error": {"message": "the prompt is longer than the context length currently available to "
                                        "the model; shorten the prompt, adjust the context length in settings, "
                                        "or use a model with a longer context length",
                             "type": "invalid_request_error"}}
UNRELATED = {"error": {"message": "temperature must be non-negative", "type": "BadRequestError", "code": 400}}


class Upstream:
    """A server whose window can change under it, as across a restart."""

    def __init__(self, window=8192):
        self.window = window
        self.reject = None              # (status, body) for the next chat calls, or None
        self.probe_fails = False
        self.probe_delay = 0.0
        self.models = 0
        self.show = 0
        self.chat = 0

    async def handler(self, request):
        path = request.url.path
        if path.endswith("/v1/models") and request.method == "GET":
            self.models += 1
            if self.probe_delay:
                await asyncio.sleep(self.probe_delay)
            if self.probe_fails:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "m", "object": "model", "max_model_len": self.window}]})
        if path.endswith("/api/show"):
            self.show += 1
            if self.probe_fails:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"parameters": "num_ctx %d" % self.window})
        if path.endswith("/props"):
            return httpx.Response(404, json={"error": "no"})
        self.chat += 1
        status, body = self.reject or (200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        raw = json.dumps(body).encode()

        async def _stream():
            yield raw[:20]
            yield raw[20:]
        return httpx.Response(status, headers={"content-type": "application/json"}, content=_stream())


def wire(g, up):
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(up.handler))
    return up


def chat(g):
    from fastapi.testclient import TestClient
    return TestClient(g.app, base_url=LOOPBACK).post(
        "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})


def messages(g):
    from fastapi.testclient import TestClient
    return TestClient(g.app, base_url=LOOPBACK).post(
        "/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": 1000.0}

    def advance(seconds):
        now["t"] += seconds
    return now, advance


def _load(monkeypatch, tmp_path, clock, url=VLLM, **env):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=url, GUARDIAN_NUM_CTX="", **env)
    monkeypatch.setattr(g, "_clock", lambda: clock[0]["t"], raising=False)
    return g


# --- the acceptance test: 8192, restart at 4096, a context-length 400 -------------
@pytest.mark.parametrize("status,body", [(400, VLLM_TOO_LONG), (400, LLAMA_TOO_LONG), (400, OLLAMA_TOO_LONG),
                                         (413, {"error": "the input length exceeds the context length"})])
def test_after_a_context_length_rejection_the_next_request_budgets_against_the_new_window(
        monkeypatch, tmp_path, clock, status, body):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    assert chat(g).status_code == 200
    assert g._state["last_num_ctx"] == 8192

    up.window, up.reject = 4096, (status, body)            # restarted with a smaller window
    r = chat(g)
    assert r.status_code == status
    assert r.content == json.dumps(body).encode()          # the rejection reaches the client untouched
    assert up.chat == 2                                     # and is not retried by the proxy

    up.reject = None                                        # the client shortens and retries
    assert chat(g).status_code == 200
    assert g._state["last_num_ctx"] == 4096
    assert up.models == 2


def test_the_messages_route_sees_the_rejection_too(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    messages(g)
    up.window, up.reject = 4096, (400, LLAMA_TOO_LONG)
    assert messages(g).status_code == 400
    up.reject = None
    messages(g)
    assert g._detected["m"]["used"] == 4096


def test_an_ollama_upstream_is_asked_again_after_its_rejection(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock, url=OLLAMA)
    up = wire(g, Upstream(8192))
    chat(g)
    up.window, up.reject = 4096, (400, OLLAMA_TOO_LONG)
    chat(g)
    up.reject = None
    chat(g)
    assert up.show == 2 and g._state["last_num_ctx"] == 4096


def test_an_unrelated_400_does_not_reprobe(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    chat(g)
    up.reject = (400, UNRELATED)
    chat(g)
    up.reject = None
    chat(g)
    assert up.models == 1


def test_repeated_rejections_reprobe_once_per_ttl_window(monkeypatch, tmp_path, clock):
    """A request the right window still cannot hold keeps being rejected; that
    must not turn into a probe per request."""
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    chat(g)
    up.reject = (400, VLLM_TOO_LONG)
    for _ in range(5):
        chat(g)
    assert up.models == 2
    clock[1](601)                                           # a new TTL window: one more is allowed
    for _ in range(3):
        chat(g)
    assert up.models == 3


# --- the TTL ------------------------------------------------------------------------
def test_ttl_expiry_triggers_exactly_one_reprobe(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    chat(g)
    clock[1](599)
    chat(g)
    assert up.models == 1                                   # not yet
    clock[1](2)
    up.window = 4096
    for _ in range(4):
        chat(g)
    assert up.models == 2
    assert g._state["last_num_ctx"] == 4096


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_reprobe(monkeypatch, tmp_path, clock):
    """Only one request waits on a re-probe; the others go on with the last
    good window instead of each asking the server."""
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    assert await g.context_window(g._http_client, "m") == 8192
    clock[1](601)
    up.window, up.probe_delay = 4096, 0.05
    got = await asyncio.gather(*[g.context_window(g._http_client, "m") for _ in range(5)])
    assert up.models == 2
    assert sorted(got) == [4096] + [8192] * 4
    assert await g.context_window(g._http_client, "m") == 4096


@pytest.mark.asyncio
async def test_concurrent_first_requests_share_one_probe(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    up.probe_delay = 0.05
    got = await asyncio.gather(*[g.context_window(g._http_client, "m") for _ in range(5)])
    assert up.models == 1 and got == [8192] * 5


def test_a_probe_error_keeps_the_last_good_window(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    chat(g)
    before = dict(g._detected["m"])
    clock[1](601)
    up.probe_fails = True
    assert chat(g).status_code == 200
    assert up.models == 2
    assert g._state["last_num_ctx"] == 8192
    assert g._detected["m"] == before
    chat(g)
    assert up.models == 2                                   # and the failed probe counts: no retry storm


def test_ttl_zero_never_reprobes(monkeypatch, tmp_path, clock):
    g = _load(monkeypatch, tmp_path, clock, GUARDIAN_DETECT_TTL="0")
    up = wire(g, Upstream(8192))
    chat(g)
    clock[1](100000)
    up.window, up.reject = 4096, (400, VLLM_TOO_LONG)
    chat(g)
    up.reject = None
    chat(g)
    assert up.models == 1 and g._state["last_num_ctx"] == 8192


@pytest.mark.parametrize("raw,expected", [("", 600), ("30", 30), ("0", 0), ("junk", 600), ("-5", 600)])
def test_the_ttl_setting(monkeypatch, tmp_path, clock, raw, expected):
    g = _load(monkeypatch, tmp_path, clock, GUARDIAN_DETECT_TTL=raw)
    assert g.DETECT_TTL == expected


def test_stats_show_the_ttl(monkeypatch, tmp_path, clock):
    from fastapi.testclient import TestClient
    g = _load(monkeypatch, tmp_path, clock, GUARDIAN_DETECT_TTL="30")
    wire(g, Upstream(8192))
    assert TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()["detect_ttl"] == 30


def test_a_gzipped_rejection_is_read_and_passed_on_unchanged(monkeypatch, tmp_path, clock):
    import gzip
    g = _load(monkeypatch, tmp_path, clock)
    up = wire(g, Upstream(8192))
    chat(g)
    packed = gzip.compress(json.dumps(VLLM_TOO_LONG).encode())

    async def gz(request):
        if request.url.path.endswith("/v1/models"):
            return await up.handler(request)

        async def _stream():
            yield packed
        return httpx.Response(400, headers={"content-type": "application/json", "content-encoding": "gzip"},
                              content=_stream())
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(gz))
    up.window = 4096
    r = chat(g)
    assert r.status_code == 400 and json.loads(r.content) == VLLM_TOO_LONG
    wire(g, up)
    chat(g)
    assert g._state["last_num_ctx"] == 4096
