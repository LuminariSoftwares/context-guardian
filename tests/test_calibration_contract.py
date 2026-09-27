"""Contract probe for P36 C1 -- the Python proxy learns its token-estimate error
from the backend's own usage figures.

Written by the overseer FROM THE CONTRACT before the code existed; seen red first.

The seam this guards (not in the original review): Ollama and llama.cpp report
only the prompt tokens they actually EVALUATED. When the KV cache is reused, that
number is far smaller than the prompt. A calibrator that averaged those samples
would shrink every estimate, compaction would fire late, and the request would
overflow -- the exact failure this proxy exists to prevent. So the factor is the
MAX of recent ratios (cache-deflated samples cannot pull it down), clamped, and
only applied after enough samples.
"""
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def g(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "100000")
    monkeypatch.setenv("GUARDIAN_COMPACT_THRESHOLD", "0.9")
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "guardian_log.json"))
    monkeypatch.delenv("GUARDIAN_CALIBRATE", raising=False)
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


# ---- usage_prompt_tokens: pure parser --------------------------------------------

def test_openai_json_usage(g):
    body = json.dumps({"choices": [], "usage": {"prompt_tokens": 1234, "completion_tokens": 5}})
    assert g.usage_prompt_tokens(body.encode(), "application/json") == 1234


def test_ollama_native_json(g):
    body = json.dumps({"message": {}, "prompt_eval_count": 777, "eval_count": 3})
    assert g.usage_prompt_tokens(body.encode(), "application/json") == 777


def test_llamacpp_timings_counts_cached_plus_new(g):
    body = json.dumps({"choices": [], "timings": {"prompt_n": 100, "cache_n": 900}})
    assert g.usage_prompt_tokens(body.encode(), "application/json") == 1000


def test_sse_final_chunk_usage(g):
    sse = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":4321,"completion_tokens":2}}\n\n'
           b"data: [DONE]\n\n")
    assert g.usage_prompt_tokens(sse, "text/event-stream") == 4321


def test_garbage_and_missing_usage_are_none(g):
    assert g.usage_prompt_tokens(b"not json", "application/json") is None
    assert g.usage_prompt_tokens(b'{"choices": []}', "application/json") is None
    assert g.usage_prompt_tokens(b"data: one\n\ndata: [DONE]\n\n", "text/event-stream") is None


# ---- Calibrator -----------------------------------------------------------------

def test_factor_is_one_until_five_samples(g):
    c = g.Calibrator()
    for _ in range(4):
        c.observe("m", 10000, 8000)
    assert c.factor("m") == 1.0
    c.observe("m", 10000, 8000)
    assert c.factor("m") == pytest.approx(0.8)


def test_cache_deflated_samples_cannot_pull_the_factor_down(g):
    c = g.Calibrator()
    for _ in range(5):
        c.observe("m", 10000, 9000)          # true ratio 0.9
    for _ in range(10):
        c.observe("m", 10000, 1500)          # KV cache reused: only the new tail evaluated
    assert c.factor("m") == pytest.approx(0.9)


def test_factor_is_clamped(g):
    c = g.Calibrator()
    for _ in range(5):
        c.observe("m", 10000, 2000)          # ratio 0.2 everywhere
    assert c.factor("m") == pytest.approx(0.7)
    d = g.Calibrator()
    for _ in range(5):
        d.observe("m", 10000, 30000)
    assert d.factor("m") == pytest.approx(1.3)


def test_tiny_or_empty_samples_are_ignored(g):
    c = g.Calibrator()
    for _ in range(10):
        c.observe("m", 100, 80)              # estimate under 256 tokens: noise
        c.observe("m", 10000, 0)
    assert c.factor("m") == 1.0
    assert c.samples("m") == 0


def test_models_are_calibrated_separately(g):
    c = g.Calibrator()
    for _ in range(5):
        c.observe("a", 10000, 8000)
    assert c.factor("b") == 1.0


# ---- end to end through the proxy -------------------------------------------------

def _wire(g, prompt_tokens):
    async def handler(request):
        async def _stream():
            yield json.dumps({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": prompt_tokens}}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=_stream())
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(g, model="m1", chars=35000):
    from fastapi.testclient import TestClient
    body = {"model": model, "messages": [{"role": "user", "content": "a" * chars}]}
    return TestClient(g.app).post("/v1/chat/completions", json=body)


def test_proxy_response_is_unchanged_and_sample_recorded(g):
    _wire(g, 8000)
    r = _chat(g)
    assert r.status_code == 200
    assert r.json()["usage"]["prompt_tokens"] == 8000
    from fastapi.testclient import TestClient
    stats = TestClient(g.app).get("/guardian/stats").json()
    assert stats["calibration"]["m1"]["samples"] == 1


def test_proxy_applies_factor_after_five_samples(g):
    _wire(g, 8000)                            # raw estimate 35000/3.5 = 10000 -> ratio 0.8
    for _ in range(5):
        _chat(g)
    _chat(g)
    assert g._state["last_raw_estimate"] == 10000
    assert g._state["last_known_total_tokens"] == 8000


def test_switch_off(g, monkeypatch):
    monkeypatch.setenv("GUARDIAN_CALIBRATE", "0")
    _wire(g, 8000)
    for _ in range(6):
        _chat(g)
    assert g._state["last_known_total_tokens"] == 10000
