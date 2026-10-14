"""Item 3 (2026-10-14): the budget base comes from Ollama's /api/show.

GUARDIAN_NUM_CTX is a number the operator has to keep in step with the model by
hand. When the upstream is Ollama, the proxy asks /api/show once per model and
uses what it says; any error falls back to the configured value.
"""
import httpx
import pytest

from _proxy_stub import load, post, wire

OLLAMA = "http://127.0.0.1:11434/v1"
OTHER = "http://127.0.0.1:1234/v1"


def _show(parameters=None, model_info=None, status=200):
    def answer(request):
        body = {"modelfile": "", "template": ""}
        if parameters is not None:
            body["parameters"] = parameters
        if model_info is not None:
            body["model_info"] = model_info
        return httpx.Response(status, json=body)
    return answer


def _chat(g, model="qwen"):
    return post(g, b'{"model":"%s","messages":[{"role":"user","content":"hi"}]}'
                % model.encode())


def test_num_ctx_from_api_show_is_the_budget_base(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    seen = wire(g, show=_show(parameters="stop \"<|im_end|>\"\nnum_ctx                        2048"))
    assert _chat(g).status_code == 200
    assert seen["show"] == [{"model": "qwen"}]
    assert g._state["last_num_ctx"] == 2048
    assert g._state["last_input_budget"] == g.effective_threshold(num_ctx=2048)


def test_api_show_is_called_once_per_model(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    seen = wire(g, show=_show(parameters="num_ctx 2048"))
    _chat(g, "a")
    _chat(g, "a")
    _chat(g, "b")
    assert [s["model"] for s in seen["show"]] == ["a", "b"]


def test_context_length_from_model_info_is_used_when_smaller(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    wire(g, show=_show(model_info={"general.architecture": "qwen2",
                                   "qwen2.context_length": 600}))
    _chat(g)
    assert g._state["last_num_ctx"] == 600


def test_a_larger_trained_context_does_not_raise_the_budget(monkeypatch, tmp_path):
    """context_length is the model's trained maximum, not the window Ollama
    runs it at. Trusting a bigger number would compact LATE -- the overflow
    this proxy exists to stop -- so it can only lower the configured value."""
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    wire(g, show=_show(model_info={"llama.context_length": 131072}))
    _chat(g)
    assert g._state["last_num_ctx"] == 1000


@pytest.mark.parametrize("answer", [
    _show(status=500),
    _show(parameters="num_ctx lots"),
    lambda request: httpx.Response(200, content=b"not json"),
])
def test_any_api_show_error_falls_back_and_the_request_passes(monkeypatch, tmp_path, answer):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    seen = wire(g, show=answer)
    assert _chat(g).status_code == 200
    assert len(seen["chat"]) == 1
    assert g._state["last_num_ctx"] == 1000


def test_an_api_show_that_raises_falls_back_and_the_request_passes(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)

    def refuse(request):
        raise httpx.ConnectError("refused")
    seen = wire(g, show=refuse)
    assert _chat(g).status_code == 200
    assert len(seen["chat"]) == 1
    assert g._state["last_num_ctx"] == 1000


def test_a_non_ollama_upstream_is_not_asked(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OTHER)
    seen = wire(g, show=_show(parameters="num_ctx 2048"))
    _chat(g)
    assert seen["show"] == []
    assert g._state["last_num_ctx"] == 1000


@pytest.mark.parametrize("setting,url,asked", [
    ("1", OTHER, True), ("0", OLLAMA, False), ("auto", OLLAMA, True)])
def test_the_switch_overrides_the_guess(monkeypatch, tmp_path, setting, url, asked):
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=url,
             GUARDIAN_OLLAMA_CONTEXT=setting)
    seen = wire(g, show=_show(parameters="num_ctx 2048"))
    _chat(g)
    assert bool(seen["show"]) is asked


def test_stats_show_what_ollama_reported(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=OLLAMA)
    wire(g, show=_show(parameters="num_ctx 2048"))
    _chat(g)
    stats = TestClient(g.app).get("/guardian/stats").json()
    assert stats["ollama_context"] == {"qwen": 2048}
    assert stats["last_num_ctx"] == 2048
