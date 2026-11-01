"""Edges of pinned facts, the Must Keep chain and the summary retry that tests/test_p124_keep.py does not pin down."""
import asyncio
import json

import httpx

from _proxy_stub import load
from test_p124_keep import GENERIC, _env, compactions, fact_turns, last_forwarded, session, stub


def _g(monkeypatch, tmp_path, **extra):
    return load(monkeypatch, tmp_path, **_env(**extra))


def user(text):
    return {"role": "user", "content": text}


def test_a_standalone_request_takes_the_next_sentence_with_it(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    pins = g.extract_pins([user("Remember this. The cache dir is /srv/cache-7. Unrelated chatter follows here.")])
    assert pins == ["Remember this. The cache dir is /srv/cache-7."]


def test_only_what_the_user_typed_is_pinned(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    msgs = [
        {"role": "assistant", "content": "I will remember this: the code is 111."},
        {"role": "tool", "content": "Please remember this: the token is 222."},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "Don't forget 333 please."}]},
        {"role": "user", "content": [{"type": "text", "text": "Don’t forget that the code word is plover."}]},
    ]
    assert g.extract_pins(msgs) == ["Don’t forget that the code word is plover."]


def test_split_must_keep_reads_bullets_and_leaves_other_text_alone(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    text = "Prose before.\n\n## Must Keep\n- port 48213\n* codename VELLUM\n2. bucket kiln-7\n\n## Next\nafter"
    body, bullets = g.split_must_keep(text)
    assert bullets == ["port 48213", "codename VELLUM", "bucket kiln-7"]
    assert "Prose before." in body and "after" in body and "48213" not in body
    assert g.split_must_keep("## Must Keep\n- none\n")[1] == []
    plain = "A summary with no section.  "
    assert g.split_must_keep(plain) == (plain, [])


def test_build_pins_dedupes_caps_and_can_be_switched_off(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    ev = [user("Please remember this: the port is 1."), user("Please remember this: the port is 1."),
          user("Please remember this: the host is h2.")]
    assert g.build_pins(ev, ["port is 9"]) == ["port is 9", "Please remember this: the port is 1.",
                                               "Please remember this: the host is h2."]
    assert g.build_pins(ev, ["the port is 1"]) == ["Please remember this: the port is 1.",
                                                   "Please remember this: the host is h2."]
    assert g.build_pins(ev, [], cap=0) == []
    one = g.build_pins([user("Remember this: " + "x" * 400)], [], cap=100)
    assert len(one) == 1 and len(one[0]) <= 100
    off = _g(monkeypatch, tmp_path, GUARDIAN_PIN_MAX_CHARS="0")
    seen = stub(off, lambda n, p: GENERIC)
    session(off, "chat", fact_turns(after=12))
    assert compactions(off) > 0 and "48213" not in last_forwarded(seen)


def test_a_failing_pin_extraction_still_compacts(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    seen = stub(g, lambda n, p: GENERIC)
    monkeypatch.setattr(g, "extract_pins", lambda m: 1 / 0)
    session(g, "chat", fact_turns(after=12))
    assert compactions(g) > 0
    assert GENERIC in last_forwarded(seen) and "48213" not in last_forwarded(seen)


def test_a_failed_summary_call_is_not_retried(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path, GUARDIAN_SUMMARY_MEMO="0", GUARDIAN_NUM_CTX="8000")
    calls = []

    async def handler(request):
        if "BEGIN_TRANSCRIPT" in request.content.decode():
            calls.append(1)
            return httpx.Response(500, json={"error": "down"})

        async def _stream():
            yield json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_stream())
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from test_p124_keep import FILLER, post
    msgs = [{"role": "user", "content": FILLER}, {"role": "assistant", "content": FILLER}] * 8
    msgs.append({"role": "user", "content": "next"})
    post(g, "chat", msgs)
    assert len(calls) == 1 and compactions(g) == 0


def _pieces_client(prompts, replies):
    async def handler(request):
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        text = replies(len(prompts))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text},
                                                      "finish_reason": "stop"}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_every_piece_of_a_summary_made_in_pieces_carries_the_new_prompt(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    prompts = []
    client = _pieces_client(prompts, lambda n: GENERIC)
    older = [user("message %d %s" % (i, "word " * 150)) for i in range(6)]
    out = asyncio.run(g.summarize_older_messages(client, "m", older, None, budget_tokens=700))
    assert out == GENERIC and len(prompts) >= 2
    for p in prompts:
        low = p.lower()
        assert "## must keep" in low and "carry forward" in low and "do not answer" in low


def test_a_must_keep_bullet_from_a_middle_piece_survives_a_last_piece_that_omits_it(monkeypatch, tmp_path):
    g = _g(monkeypatch, tmp_path)
    prompts = []
    client = _pieces_client(prompts, lambda n: GENERIC + ("\n\n## Must Keep\n- ticket TK-1\n" if n == 1 else ""))
    older = [user("message %d %s" % (i, "word " * 150)) for i in range(6)]

    async def run():
        token = g._summary_budget.set(700)
        try:
            return await g._memo_summary(older, lambda msgs: g.summarize_older_messages(client, "m", msgs, None))
        finally:
            g._summary_budget.reset(token)
    result = asyncio.run(run())
    assert len(prompts) >= 2 and "TK-1" not in result["summary"]
    assert result["must_keep"] == ["ticket TK-1"]
