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
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA, GUARDIAN_NUM_CTX="")
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


# --- follow-up 2, item 3: /props may describe a different model --------------------
# Behind a model switcher, or in llama-server's router mode, /props can describe
# whichever model is loaded, not the one asked for. get_res_props
# (tools/server/server-context.cpp) names it in model_alias (meta.model_name: the
# first --alias, else the model's name, else the file name) and model_path. In
# router mode GET /props without ?model= answers a dummy (model_alias
# "llama-server", n_ctx 0) and GET /props?model=<name> is proxied to that
# model's instance (get_router_props, tools/server/server-models.cpp);
# ?autoload=false keeps the probe from loading a model (is_autoload).
TWO_MODELS = {"object": "list", "data": [{"id": "devstral", "object": "model"},
                                         {"id": "qwen-coder", "object": "model"}]}
ONE_MODEL = {"object": "list", "data": [{"id": "devstral", "object": "model"}]}


def test_props_naming_another_model_is_not_used(monkeypatch, tmp_path, caplog):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models=TWO_MODELS, props={"default_generation_settings": {"n_ctx": 4096},
                                      "model_alias": "qwen-coder", "model_path": "/models/qwen-coder.gguf"})
    with caplog.at_level("INFO", logger="context_guardian"):
        chat(g, "devstral")
        chat(g, "devstral")
    assert g._state["last_num_ctx"] == 32768
    assert g._detected["devstral"] is None
    why = [r.getMessage() for r in caplog.records if "/props" in r.getMessage()]
    assert len(why) == 1 and "qwen-coder" in why[0] and "devstral" in why[0], why


@pytest.mark.parametrize("props", [
    {"model_alias": "devstral", "model_path": "/models/Devstral-Small-Q4_K_M.gguf"},
    {"model_alias": "something-else", "model_path": "/models/devstral.gguf"},
    {"model_alias": "something-else", "model_path": "C:\\models\\devstral"},
    {"model_path": "/models/devstral.gguf"},
])
def test_props_naming_the_requested_model_is_used(monkeypatch, tmp_path, props):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models=TWO_MODELS, props=dict(props, default_generation_settings={"n_ctx": 4096}))
    chat(g, "devstral")
    assert g._state["last_num_ctx"] == 4096


def test_an_alias_from_the_models_card_counts_as_the_model(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models={"data": [{"id": "devstral", "aliases": ["dev"], "object": "model"},
                             {"id": "qwen-coder", "object": "model"}]},
         props={"default_generation_settings": {"n_ctx": 4096}, "model_alias": "devstral"})
    chat(g, "dev")
    assert g._state["last_num_ctx"] == 4096


def test_a_single_model_server_with_no_identification_is_used(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models=ONE_MODEL, props={"default_generation_settings": {"n_ctx": 4096}})
    chat(g, "anything")
    assert g._state["last_num_ctx"] == 4096


@pytest.mark.parametrize("models", [TWO_MODELS, None, 500])
def test_no_identification_and_not_exactly_one_model_is_not_used(monkeypatch, tmp_path, models):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models=models, props={"default_generation_settings": {"n_ctx": 4096}})
    chat(g, "devstral")
    assert g._state["last_num_ctx"] == 32768


def test_the_props_probe_names_the_model_and_does_not_load_it(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    asked = []

    def handler(request):
        if request.url.path.endswith("/props"):
            asked.append(dict(request.url.params))
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096},
                                             "model_alias": "dev stral/q4"})
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(404)

        async def _stream():
            yield b'{"choices":[]}'
        return httpx.Response(200, content=_stream())
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chat(g, "dev stral/q4")
    assert asked == [{"model": "dev stral/q4", "autoload": "false"}]
    assert g._state["last_num_ctx"] == 4096


def test_the_router_dummy_answer_is_not_used(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=LLAMA, GUARDIAN_NUM_CTX="")
    wire(g, models=ONE_MODEL, props={"role": "router", "model_alias": "llama-server", "model_path": "none",
                                     "default_generation_settings": {"params": {}, "n_ctx": 0}})
    chat(g, "devstral")
    assert g._state["last_num_ctx"] == 32768
