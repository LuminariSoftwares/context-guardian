"""P124 acceptance (O5, 2026-11-01). DO NOT EDIT -- written before the change, from the contract.

Why: on the guardian-leaderboard keep-facts bench (qwen3-coder:30b, num_ctx 8192) the proxy kept 1 of 36 facts the
user had asked it to remember. The proxy's only memory is one rolling LLM summary rewritten at every compaction (44
times in 48 turns there); the summary never held more than 4-5 facts at once, and when a question about an earlier fact
reached the summariser it wrote "no value was mentioned", after which every summary carried none. The DSH engine pins
explicit keep/remember requests verbatim (cg_memory.js PIN_RE) and kept 12/12; the proxy has no pins at all.

Contract (C-Keep), both routes (/v1/chat/completions and /v1/messages):
  S1 pins
    * A message the proxy evicts that contains an explicit keep request -- the same wording cg_memory.js PIN_RE
      accepts ("remember this/that/these", "keep this for later", "for later use/reference", "don't forget",
      "make a note", "note this for later", "pin this") -- has its sentence(s) pinned for the conversation.
    * Every request the proxy compacts carries all pinned sentences VERBATIM in what it forwards upstream, whatever the
      LLM summary says, for the rest of the conversation (memo reuse / extend included).
    * Only explicit requests are pinned: a sentence without one of those phrases is never pinned.
    * The pinned text is capped (GUARDIAN_PIN_MAX_CHARS, default 1600 characters); over the cap the OLDEST pins are
      dropped first, and the forwarded request still fits the budget.
  S2 summary prompt
    * The condense prompt contains the literal strings "## Must Keep", "carry forward" and "do not answer"
      (case-insensitive): exact values go in a `## Must Keep` section, an earlier summary is carried forward, and a
      question inside the transcript is described, not answered.
    * Each bullet of a `## Must Keep` section in a summary is pinned like S1 for the rest of the conversation.
  S3 a short summary is retried once
    * (Tested on a summary that fits one call.) A summary shorter than GUARDIAN_MIN_SUMMARY_CHARS is asked for once more; if the second is usable the compaction
      happens, otherwise the request fails open exactly as today. Never more than 2 summary calls per summary.
  S6 the version the proxy reports is the running module's `__version__`, never stale installed metadata.
  S7 /v1/messages spills one oversized message the way /v1/chat/completions does (`oversized_spills` counts it).
"""
import asyncio
import json
import re

import httpx
import pytest

from _proxy_stub import LOOPBACK, load

GENERIC = "A general summary: the user planned several household projects in small, steady steps."
FACTS = [("staging port", "48213"), ("release codename", "VELLUM"), ("backup bucket", "kiln-archive-7"),
         ("build number", "90417"), ("lab room", "C-214"), ("deploy window", "04:40")]
FILLER = ("Let me think out loud about the kitchen renovation. The first idea was to start small and see what sticks. "
          "Most of the work is waiting for parts, which is the slow part every time. ") * 8


def _env(**extra):
    env = {"GUARDIAN_NUM_CTX": "4000", "GUARDIAN_RESERVE_OUTPUT": "500", "GUARDIAN_COMPACT_THRESHOLD": "0.8",
           "GUARDIAN_KEEP_RECENT_MESSAGES": "", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_CALIBRATE": "0"}
    env.update(extra)
    return env


def stub(g, summarise):
    """Recording upstream for both routes. `summarise(n, prompt)` returns the n-th summary's text (n from 1)."""
    seen = {"summaries": [], "forwarded": []}

    async def handler(request):
        body = json.loads(request.content)
        prompt = json.dumps(body)
        if "BEGIN_TRANSCRIPT" in prompt:
            seen["summaries"].append(prompt)
            text = summarise(len(seen["summaries"]), prompt)
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text},
                                                          "finish_reason": "stop"}]})
        seen["forwarded"].append(body)
        if request.url.path.endswith("/messages"):
            reply = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 10, "output_tokens": 1}}
        else:
            reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        async def _stream():
            yield json.dumps(reply).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_stream())

    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def client(g):
    from fastapi.testclient import TestClient
    return TestClient(g.app, base_url=LOOPBACK)


def post(g, route, messages):
    if route == "chat":
        r = client(g).post("/v1/chat/completions", json={"model": "m", "messages": messages})
    else:
        r = client(g).post("/v1/messages", json={"model": "m", "max_tokens": 10, "messages": messages})
    assert r.status_code == 200
    return r


def session(g, route, user_turns):
    """Drive a conversation the way a real client does: the WHOLE history is resent every turn."""
    msgs = []
    for text in user_turns:
        msgs.append({"role": "user", "content": text})
        post(g, route, msgs)
        msgs.append({"role": "assistant", "content": "Noted. " + FILLER[:400]})
    return msgs


def fact_turns(facts=FACTS, after=24):
    turns = []
    for k, v in facts:
        turns.append("Please remember this for later: the %s is %s. %s" % (k, v, FILLER))
        turns.append(FILLER)
    turns += [FILLER] * after
    return turns


def last_forwarded(seen):
    return json.dumps(seen["forwarded"][-1], ensure_ascii=False)


def compactions(g):
    return g._state["compactions_performed"]


# ---- S1 ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["chat", "messages"])
def test_s1_pinned_facts_survive_every_compaction(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_env())
    seen = stub(g, lambda n, p: GENERIC)
    session(g, route, fact_turns())
    assert compactions(g) >= 10, "the scripted session must compact many times (%d)" % compactions(g)
    sent = last_forwarded(seen)
    missing = [v for _, v in FACTS if v not in sent]
    assert not missing, "pinned facts lost after %d compactions: %s" % (compactions(g), missing)


@pytest.mark.parametrize("route", ["chat", "messages"])
def test_s1_only_explicit_requests_are_pinned(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_env())
    seen = stub(g, lambda n, p: GENERIC)
    turns = ["Important: the gate code is 9911. " + FILLER] + [FILLER] * 30
    session(g, route, turns)
    assert compactions(g) >= 5
    assert "9911" not in last_forwarded(seen), "a sentence with no keep request was pinned"


def test_s1_cap_drops_oldest_first_and_still_fits(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_env(GUARDIAN_PIN_MAX_CHARS="600"))
    seen = stub(g, lambda n, p: GENERIC)
    many = [("item %02d" % i, "VAL%02dX" % i) for i in range(20)]
    session(g, "chat", fact_turns(many, after=10))
    sent = last_forwarded(seen)
    assert "VAL19X" in sent, "the newest pin must survive the cap"
    assert "VAL00X" not in sent, "over the cap the oldest pin goes first"
    fwd = seen["forwarded"][-1]["messages"]
    assert g.estimate_tokens(fwd) <= g.effective_threshold(num_ctx=4000), "pins pushed the request over budget"


# ---- S2 ---------------------------------------------------------------------------------------------------------
def test_s2_prompt_asks_for_must_keep_carry_forward_and_no_answers(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_env())
    seen = stub(g, lambda n, p: GENERIC)
    session(g, "chat", [FILLER] * 20)
    assert seen["summaries"], "no summary call was made"
    for prompt in seen["summaries"]:
        low = prompt.lower()
        for phrase in ("## must keep", "carry forward", "do not answer"):
            assert phrase in low, "summary prompt lacks %r" % phrase


@pytest.mark.parametrize("route", ["chat", "messages"])
def test_s2_must_keep_bullets_are_pinned(monkeypatch, tmp_path, route):
    g = load(monkeypatch, tmp_path, **_env())

    def summarise(n, prompt):
        if n == 1:
            return GENERIC + "\n\n## Must Keep\n- the vendor ticket is TK-55819\n"
        return GENERIC
    seen = stub(g, summarise)
    session(g, route, [FILLER] * 30)
    assert compactions(g) >= 5
    assert "TK-55819" in last_forwarded(seen), "a Must Keep bullet was lost after later summaries"


# ---- S3 ---------------------------------------------------------------------------------------------------------
def test_s3_a_short_summary_is_retried_once_and_used(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_env(GUARDIAN_SUMMARY_MEMO="0", GUARDIAN_NUM_CTX="8000"))
    seen = stub(g, lambda n, p: "short" if n == 1 else GENERIC)
    msgs = [{"role": "user", "content": FILLER}, {"role": "assistant", "content": FILLER}] * 8
    msgs.append({"role": "user", "content": "next"})
    post(g, "chat", msgs)
    assert len(seen["summaries"]) == 2, "expected one retry (%d summary calls)" % len(seen["summaries"])
    assert compactions(g) == 1, "the retried summary was usable, so the request must be compacted"


def test_s3_two_short_summaries_fail_open_after_exactly_two_calls(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_env(GUARDIAN_SUMMARY_MEMO="0", GUARDIAN_NUM_CTX="8000"))
    seen = stub(g, lambda n, p: "short")
    msgs = [{"role": "user", "content": FILLER}, {"role": "assistant", "content": FILLER}] * 8
    msgs.append({"role": "user", "content": "next"})
    post(g, "chat", msgs)
    assert len(seen["summaries"]) == 2, "never more than one retry (%d calls)" % len(seen["summaries"])
    assert compactions(g) == 0
    assert seen["forwarded"][-1]["messages"] == msgs, "a failed summary must forward the original request"


# ---- S6 ---------------------------------------------------------------------------------------------------------
def test_s6_reported_version_is_the_running_module(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, **_env())
    import importlib.metadata as md
    monkeypatch.setattr(md, "version", lambda name: "0.0.1")
    assert g.guardian_version() == g.__version__
    stats = client(g).get("/guardian/stats").json()
    assert g.__version__ in json.dumps(stats) and "0.0.1" not in json.dumps(stats)


# ---- S7 ---------------------------------------------------------------------------------------------------------
def test_s7_messages_route_spills_one_oversized_message(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="1000", GUARDIAN_DETECT_CONTEXT="0",
             GUARDIAN_KEEP_RECENT_MESSAGES="2", GUARDIAN_CALIBRATE="0")
    seen = stub(g, lambda n, p: GENERIC)
    paste = "\n".join("P%04d payload line with some ordinary words in it" % i for i in range(400))
    post(g, "messages", [{"role": "user", "content": paste}])
    stats = client(g).get("/guardian/stats").json()
    assert stats["oversized_spills"] == 1, "the /v1/messages paste was not spilled"
    fwd = json.dumps(seen["forwarded"][-1])
    assert len(fwd) < len(paste), "the forwarded /v1/messages request was not cut"
    assert re.search(r"P0000", fwd) and re.search(r"P0399", fwd), "the cut keeps the start and the end"
