"""P111 S1 (B1) acceptance -- exact token counts, template included. Written by the O5 overseer; DO NOT EDIT.

Why: README "What this does not do" (3): the estimate is characters / 3.5, content-blind, "not always conservative"
-- dense JSON and CJK read LOW, so compaction fires late and the backend overflows first. llama.cpp and vLLM can
count for us, chat template included; the calibrated estimate stays the fallback everywhere else.

Contract (plan P111 rev 1, decisions 1-2 + C1). Two details O5 pins here so the test can call the module:
`count_request(..., upstream_root=None)` takes the upstream root as a keyword (default: GUARDIAN_UPSTREAM_URL), and
module state (probe results, the per-message cache, counters) lives in `cg_count` so a module reload resets it.
- `cg_count.py` (new, in pyproject py-modules; no new dependency): `probe_tokenizer(client, upstream_root, model)`
  -> "vllm" | "llamacpp" | "ollama" | ""; `count_request(client, messages, tools, model, upstream_root=None)` ->
  `(tokens, source)`, source in {"tokenize", "calibrated", "estimate"}; `count_text(client, text, model)`.
- vLLM: ONE `POST <root>/tokenize` with `messages` and `add_generation_prompt: true` -> its `count`.
- llama.cpp: `POST <root>/apply-template {"messages"}` -> `prompt`, then `POST <root>/tokenize {"content": prompt}`
  -> `len(tokens)`; and the per-message cache keyed by `cg_archive.message_hash`: on the next turn the OLD messages'
  text is not sent to the server again, only the new message's (plus at most a template-overhead probe), and the
  total stays exact (within 2 tokens here). More than GUARDIAN_COUNT_RECOUNT_MISSES new messages -> one
  whole-request recount.
- Failure, timeout (GUARDIAN_TOKENIZE_TIMEOUT) or GUARDIAN_TOKENIZE=0 -> the old estimate path, the request still
  goes through, failures counted in `tokenize_failures`. No tokenizer (Ollama without /api/tokenize) -> estimate.
- When the count is not exact, GUARDIAN_ESTIMATE_MARGIN multiplies it.
- `/guardian/stats` gains count_source, tokenize_failures, estimate_margin, count_cache_hits, count_cache_misses;
  every "compaction" event carries count_source; maybe_compact and maybe_compact_anthropic both use count_request.
- The exact count is what decides compaction: text the char estimate calls small but the tokenizer calls big compacts.
- README bullet (3) names llama.cpp, vLLM and GUARDIAN_ESTIMATE_MARGIN.
"""
import asyncio
import importlib
import json
import re
import sys
from pathlib import Path

import httpx

from _proxy_stub import LOOPBACK, SUMMARY, load, post

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LLAMA = "http://127.0.0.1:8080/v1"
VLLM = "http://127.0.0.1:8000/v1"
OLLAMA = "http://127.0.0.1:11434/v1"


def render(messages):
    """The stub's chat template: additive per message, so per-message counting can be exact."""
    return "BOS " + " ".join("<%s> %s </s>" % (m.get("role"), m.get("content") or "") for m in messages) + " <asst>"


def words(text):
    return len(text.split())


def stub(kind, fail_tokenize=False, window=4000):
    """A recording upstream of the given kind. Tokens = whitespace words."""
    seen = {"tokenize": [], "apply": [], "chat": [], "api_tokenize": 0, "summaries": 0}

    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/v1/models"):
            card = {"id": "m"}
            if kind == "vllm":
                card["max_model_len"] = window
            return httpx.Response(200, json={"data": [card]})
        if path == "/props" and kind == "llamacpp":
            return httpx.Response(200, json={"model_alias": "m",
                                             "default_generation_settings": {"n_ctx": window}})
        if path == "/api/show" and kind == "ollama":
            return httpx.Response(200, json={"parameters": "num_ctx %d" % window})
        if path == "/api/tokenize":
            seen["api_tokenize"] += 1
            return httpx.Response(404, json={"error": "not found"})
        if path == "/apply-template" and kind == "llamacpp":
            seen["apply"].append(body)
            return httpx.Response(200, json={"prompt": render(body.get("messages") or [])})
        if path == "/tokenize" and kind in ("llamacpp", "vllm"):
            seen["tokenize"].append(body)
            if fail_tokenize:
                return httpx.Response(500, json={"error": "boom"})
            if kind == "vllm":
                n = words(render(body.get("messages") or [])) if "messages" in body else words(body.get("prompt", ""))
                return httpx.Response(200, json={"count": n, "tokens": list(range(n)), "max_model_len": window})
            return httpx.Response(200, json={"tokens": list(range(words(body.get("content", ""))))})
        if path.endswith("/chat/completions"):
            if b"BEGIN_TRANSCRIPT" in request.content:
                seen["summaries"] += 1
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": SUMMARY}, "finish_reason": "stop"}]})
            seen["chat"].append(body)

            async def _stream():
                yield b"data: one\n\n"
                yield b"data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_stream())
        return httpx.Response(404, json={"error": "no route %s" % path})

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fresh_count(monkeypatch, **env):
    for k in ("GUARDIAN_TOKENIZE", "GUARDIAN_TOKENIZE_TIMEOUT", "GUARDIAN_ESTIMATE_MARGIN",
              "GUARDIAN_COUNT_CACHE", "GUARDIAN_COUNT_RECOUNT_MISSES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import cg_count
    return importlib.reload(cg_count)


def convo(n, tag="m"):
    out = []
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": "%s%03d alpha beta gamma delta" % (tag, i)})
    return out


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the module ------

def test_module_is_packaged_without_a_new_dependency():
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    mods = re.search(r"^py-modules = \[(.*?)\]", py, re.M).group(1)
    assert '"cg_count"' in mods
    block = re.search(r"^dependencies = \[\n?(.*?)\n?^\]|^dependencies = \[(.*?)\]\s*$", py, re.M | re.S)
    deps = block.group(1) or block.group(2)
    names = {re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0] for d in re.findall(r'"([^"]+)"', deps)}
    assert names <= {"fastapi", "uvicorn", "httpx", "python-dotenv"}, names


def test_probe_tells_the_upstreams_apart(monkeypatch):
    cc = fresh_count(monkeypatch)
    for kind, root, want in (("vllm", VLLM, "vllm"), ("llamacpp", LLAMA, "llamacpp"), ("ollama", OLLAMA, "")):
        _, client = stub(kind)
        got = run(cc.probe_tokenizer(client, root, "m"))
        assert got == want, (kind, got)


def test_vllm_counts_the_whole_templated_request_in_one_call(monkeypatch):
    cc = fresh_count(monkeypatch)
    seen, client = stub("vllm")
    msgs = convo(6)
    n, source = run(cc.count_request(client, msgs, None, "m", upstream_root=VLLM))
    assert source == "tokenize"
    assert n == words(render(msgs))
    calls = [b for b in seen["tokenize"] if "messages" in b]
    assert len(calls) == 1 and calls[0].get("add_generation_prompt") is True


def test_llamacpp_applies_the_template_then_tokenizes(monkeypatch):
    cc = fresh_count(monkeypatch)
    seen, client = stub("llamacpp")
    msgs = convo(4)
    n, source = run(cc.count_request(client, msgs, None, "m", upstream_root=LLAMA))
    assert source == "tokenize"
    assert n == words(render(msgs))
    assert seen["apply"] and seen["tokenize"]
    assert any(b.get("content") == render(msgs) for b in seen["tokenize"]) or len(seen["apply"]) >= 1


def test_next_turn_sends_only_the_new_message(monkeypatch):
    cc = fresh_count(monkeypatch)
    seen, client = stub("llamacpp")
    msgs = convo(10)
    run(cc.count_request(client, msgs, None, "m", upstream_root=LLAMA))
    seen["apply"].clear()
    seen["tokenize"].clear()
    turn2 = msgs + [{"role": "user", "content": "newmsg zeta eta theta"}]
    n, source = run(cc.count_request(client, turn2, None, "m", upstream_root=LLAMA))
    assert source == "tokenize"
    assert abs(n - words(render(turn2))) <= 2, (n, words(render(turn2)))
    sent = json.dumps(seen["apply"]) + json.dumps(seen["tokenize"])
    assert "newmsg" in sent
    assert "m000" not in sent and "m005" not in sent, "old messages were re-sent for counting"
    assert cc_stat(cc, "count_cache_hits") >= 10


def cc_stat(cc, name):
    st = cc.stats() if hasattr(cc, "stats") else {}
    return st.get(name, 0)


def test_many_new_messages_trigger_one_whole_request_recount(monkeypatch):
    cc = fresh_count(monkeypatch, GUARDIAN_COUNT_RECOUNT_MISSES="2")
    seen, client = stub("llamacpp")
    msgs = convo(6)
    run(cc.count_request(client, msgs, None, "m", upstream_root=LLAMA))
    seen["apply"].clear()
    turn2 = msgs + convo(3, tag="z")
    n, _ = run(cc.count_request(client, turn2, None, "m", upstream_root=LLAMA))
    assert n == words(render(turn2))
    whole = [b for b in seen["apply"] if len(b.get("messages") or []) == len(turn2)]
    assert len(whole) == 1


def test_tokenizer_failure_falls_back_and_is_counted(monkeypatch):
    cc = fresh_count(monkeypatch)
    _, client = stub("llamacpp", fail_tokenize=True)
    n, source = run(cc.count_request(client, convo(4), None, "m", upstream_root=LLAMA))
    assert source in ("calibrated", "estimate") and n > 0
    assert cc_stat(cc, "tokenize_failures") >= 1


def test_tokenize_off_never_calls_the_server(monkeypatch):
    cc = fresh_count(monkeypatch, GUARDIAN_TOKENIZE="0")
    seen, client = stub("llamacpp")
    _, source = run(cc.count_request(client, convo(4), None, "m", upstream_root=LLAMA))
    assert source in ("calibrated", "estimate")
    assert not seen["tokenize"] and not seen["apply"]


def test_margin_multiplies_only_the_estimate(monkeypatch):
    cc = fresh_count(monkeypatch)
    _, client = stub("ollama")
    msgs = convo(8)
    base, src = run(cc.count_request(client, msgs, None, "m", upstream_root=OLLAMA))
    assert src in ("calibrated", "estimate")
    cc = fresh_count(monkeypatch, GUARDIAN_ESTIMATE_MARGIN="2")
    _, client = stub("ollama")
    doubled, _ = run(cc.count_request(client, msgs, None, "m", upstream_root=OLLAMA))
    assert abs(doubled - 2 * base) <= 2, (base, doubled)
    cc = fresh_count(monkeypatch, GUARDIAN_ESTIMATE_MARGIN="2")
    _, client = stub("llamacpp")
    exact, src = run(cc.count_request(client, msgs, None, "m", upstream_root=LLAMA))
    assert src == "tokenize" and exact == words(render(msgs))


def test_ollama_tokenize_is_probed_at_most_once_per_model(monkeypatch):
    cc = fresh_count(monkeypatch)
    seen, client = stub("ollama")
    for _ in range(3):
        run(cc.count_request(client, convo(4), None, "m", upstream_root=OLLAMA))
    assert seen["api_tokenize"] <= 1


# ------------------------------------------------------------------ the proxy -------

def proxy(monkeypatch, tmp_path, kind, root, **env):
    env = dict({"GUARDIAN_NUM_CTX": ""}, **env)
    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL=root, **env)
    import cg_count
    importlib.reload(cg_count)
    seen, client = stub(kind)
    g._http_client = client
    return g, seen


def chat(g, messages):
    return post(g, json.dumps({"model": "m", "messages": messages}).encode())


def test_stats_report_the_count_source(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    g, _ = proxy(monkeypatch, tmp_path, "llamacpp", LLAMA)
    assert chat(g, convo(4)).status_code == 200
    st = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()
    for key in ("count_source", "tokenize_failures", "estimate_margin", "count_cache_hits", "count_cache_misses"):
        assert key in st, key
    assert st["count_source"] == "tokenize"


def test_the_exact_count_decides_compaction(monkeypatch, tmp_path):
    """60 one-letter words per message: ~34 tokens by chars/3.5, 60+ by the tokenizer. A 1,000-token window with a
    0.5 threshold compacts on the exact count and would not on the estimate."""
    dense = [{"role": "user" if i % 2 == 0 else "assistant", "content": " ".join(["x"] * 60) + " t%02d" % i}
             for i in range(13)]
    est_chars = sum(len(m["content"]) for m in dense) / 3.5
    g, seen = proxy(monkeypatch, tmp_path, "llamacpp", LLAMA, GUARDIAN_NUM_CTX="1000",
                    GUARDIAN_DETECT_CONTEXT="0", GUARDIAN_KEEP_RECENT_MESSAGES="4")
    assert est_chars < g.effective_threshold(num_ctx=1000), "fixture must stay under the estimate's threshold"
    assert chat(g, dense).status_code == 200
    assert seen["summaries"] >= 1, "the tokenizer said over budget; the proxy did not compact"
    ev = [json.loads(line) for line in Path(g.LOG_PATH).read_text(encoding="utf-8").splitlines()
          if '"compaction"' in line]
    assert ev and ev[-1].get("count_source") == "tokenize"


def test_both_compaction_paths_use_count_request():
    src = (ROOT / "context_guardian.py").read_text(encoding="utf-8")
    for fn in ("async def maybe_compact(", "async def maybe_compact_anthropic("):
        body = src.split(fn, 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]
        assert "count_request" in body, fn


def test_readme_bullet_names_the_exact_upstreams():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GUARDIAN_ESTIMATE_MARGIN" in readme
    assert "it is not always conservative" not in readme or "llama.cpp" in readme.split(
        "it is not always conservative", 1)[1][:600]
