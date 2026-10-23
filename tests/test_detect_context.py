"""Follow-up item 4: ask vLLM and llama.cpp for the window too, not only Ollama.

One-model-at-a-time servers (vLLM, llama-server) are switched between models
whose windows differ by 7x (38K to 256K), and GUARDIAN_NUM_CTX is one number
per process. With GUARDIAN_DETECT_CONTEXT=auto the proxy asks the upstream once
per model: Ollama /api/show on 11434 as before, then vLLM GET /v1/models
(max_model_len), then llama.cpp GET /props (the per-slot n_ctx). Safety rule:
an explicit GUARDIAN_NUM_CTX is only ever lowered by what a server reports,
never raised. Written before the code.
"""
import json

import httpx
import pytest

from _proxy_stub import LOOPBACK, load, post

VLLM = "http://127.0.0.1:8000/v1"
LLAMA = "http://127.0.0.1:8080/v1"
OLLAMA = "http://127.0.0.1:11434/v1"


def wire(g, models=None, props=None, show=None):
    """A recording upstream. `models` / `props` / `show` are a JSON body, an
    int status, an exception, or None for 404."""
    seen = {"models": 0, "props": 0, "show": 0, "chat": 0}

    def answer(spec):
        if spec is None:
            return httpx.Response(404, json={"error": "no such route"})
        if isinstance(spec, int):
            return httpx.Response(spec, json={"error": "boom"})
        if isinstance(spec, bytes):
            return httpx.Response(200, content=spec)
        if isinstance(spec, Exception):
            raise spec
        return httpx.Response(200, json=spec)

    def handler(request):
        path = request.url.path
        if path.endswith("/v1/models") and request.method == "GET":
            seen["models"] += 1
            return answer(models)
        if path.endswith("/props"):
            seen["props"] += 1
            return answer(props)
        if path.endswith("/api/show"):
            seen["show"] += 1
            return answer(show)
        seen["chat"] += 1

        async def _stream():
            yield b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def chat(g, model):
    r = post(g, json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode())
    assert r.status_code == 200
    return r


VLLM_MODELS = {"object": "list", "data": [
    {"id": "qwen3.6-reap", "object": "model", "max_model_len": 262144},
    {"id": "small", "object": "model", "max_model_len": 800}]}
LLAMA_PROPS = {"default_generation_settings": {"n_ctx": 4096}, "total_slots": 4,
               "model_path": "/models/devstral.gguf"}


def test_vllm_reporting_less_than_configured_lowers_the_window(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000")
    seen = wire(g, models=VLLM_MODELS)
    chat(g, "small")
    assert g._state["last_num_ctx"] == 800
    assert seen["show"] == 0


def test_vllm_reporting_more_than_an_explicit_num_ctx_does_not_raise_it(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000")
    wire(g, models=VLLM_MODELS)
    chat(g, "qwen3.6-reap")
    assert g._state["last_num_ctx"] == 1000


def test_without_an_explicit_num_ctx_the_detected_window_is_used(monkeypatch, tmp_path, caplog):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="")
    assert g.NUM_CTX == 32768 and g.NUM_CTX_EXPLICIT is False
    wire(g, models=VLLM_MODELS)
    with caplog.at_level("INFO", logger="context_guardian"):
        chat(g, "qwen3.6-reap")
        chat(g, "qwen3.6-reap")
    assert g._state["last_num_ctx"] == 262144
    lines = [r.getMessage() for r in caplog.records if "reports" in r.getMessage()]
    assert lines == ["vLLM reports 262144 for qwen3.6-reap"], lines


def test_llama_cpp_props_gives_the_per_slot_window(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    seen = wire(g, models={"object": "list", "data": [{"id": "devstral", "object": "model"}]},
                props=LLAMA_PROPS)
    chat(g, "devstral")
    assert seen["models"] == 1 and seen["props"] == 1
    assert g._state["last_num_ctx"] == 4096


def test_llama_cpp_cannot_raise_an_explicit_num_ctx_either(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="1000")
    wire(g, props=LLAMA_PROPS)
    chat(g, "devstral")
    assert g._state["last_num_ctx"] == 1000


@pytest.mark.parametrize("models,props", [
    (500, 500),
    (b"not json", b"not json"),
    (httpx.ConnectError("refused"), httpx.ConnectError("refused")),
    ({"data": [{"id": "other", "max_model_len": "lots"}]}, {"default_generation_settings": {"n_ctx": -5}}),
])
def test_any_detection_error_falls_back_and_the_request_passes(monkeypatch, tmp_path, models, props):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000")
    seen = wire(g, models=models, props=props)
    chat(g, "qwen")
    assert seen["chat"] == 1
    assert g._state["last_num_ctx"] == 1000


def test_detection_is_asked_once_per_model(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000")
    seen = wire(g, models=VLLM_MODELS)
    chat(g, "small")
    chat(g, "small")
    chat(g, "qwen3.6-reap")
    assert seen["models"] == 2


def test_detect_off_asks_nothing(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000",
             GUARDIAN_DETECT_CONTEXT="0")
    seen = wire(g, models=VLLM_MODELS, props=LLAMA_PROPS)
    chat(g, "small")
    assert seen["models"] == seen["props"] == seen["show"] == 0
    assert g._state["last_num_ctx"] == 1000


def test_ollama_is_still_asked_first_and_alone_when_it_answers(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    seen = wire(g, show={"parameters": "num_ctx 2048"}, models=VLLM_MODELS, props=LLAMA_PROPS)
    chat(g, "qwen")
    assert seen["show"] == 1 and seen["models"] == 0 and seen["props"] == 0
    assert g._state["last_num_ctx"] == 2048


def test_stats_name_the_source(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=VLLM, GUARDIAN_NUM_CTX="1000")
    wire(g, models=VLLM_MODELS)
    chat(g, "small")
    stats = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()
    assert stats["context_detected"] == {"small": {"window": 800, "source": "vLLM", "used": 800}}
    assert stats["detect_context"] == "auto"


def test_the_doctor_shows_the_detected_window():
    import cg_doctor
    stats = {"num_ctx": 1000, "context_detected": {"small": {"window": 800, "source": "vLLM", "used": 800}}}
    lines = [f["what"] for f in cg_doctor.detected_window_findings(stats)]
    assert any("vLLM reports 800 for small" in ln for ln in lines), lines
