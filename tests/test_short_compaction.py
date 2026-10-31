"""Item 4 (2026-10-14): a short conversation that is over budget is compacted.

Conversations of KEEP_RECENT_MESSAGES + 1 messages or fewer (5 by default) were
skipped outright, so three messages with one huge tool dump in the middle went
upstream over the window every time. They are now compacted, keeping the first
and the last user message verbatim and summarising what lies between.
"""
import json

from _proxy_stub import SUMMARY, load, post, wire

MARKER = "[Context Guardian auto-compaction"


def _three(middle_chars):
    return [{"role": "user", "content": "the task: fix the parser"},
            {"role": "assistant", "content": "y" * middle_chars},
            {"role": "user", "content": "and now the tests"}]


def test_a_huge_middle_message_is_compacted(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    msgs = _three(8000)
    post(g, json.dumps({"model": "m", "stream": True, "messages": msgs}).encode())
    sent = json.loads(seen["chat"][0])["messages"]
    assert len(sent) == 3
    assert sent[0]["role"] == "system" and sent[0]["content"].startswith(MARKER)
    assert SUMMARY in sent[0]["content"]
    assert sent[1] == msgs[0], "the first user message was not kept intact"
    assert sent[2] == msgs[2], "the last user message was not kept intact"
    assert g._state["compactions_performed"] == 1


def test_a_short_conversation_under_budget_is_untouched(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    body = json.dumps({"model": "m", "stream": True, "messages": _three(100)}).encode()
    post(g, body)
    assert seen["chat"] == [body]
    assert g._state["compactions_performed"] == 0


def test_a_system_prompt_stays_first(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    msgs = [{"role": "system", "content": "be terse"}] + _three(8000)
    post(g, json.dumps({"model": "m", "stream": True, "messages": msgs}).encode())
    sent = json.loads(seen["chat"][0])["messages"]
    assert sent[0] == msgs[0]
    assert sent[1]["content"].startswith(MARKER)
    assert sent[2:] == [msgs[1], msgs[3]]


def test_a_single_user_message_is_spilled_not_summarised(monkeypatch, tmp_path):
    """Nothing lies between the first and last user message: no summary is made.
    P111 S2: the message is archived whole and forwarded as head + tail + pointer,
    where it used to go upstream untouched, over the window."""
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    text = "\n".join("line %04d of the pasted text" % i for i in range(400))
    body = json.dumps({"model": "m", "stream": True,
                       "messages": [{"role": "user", "content": text}]}).encode()
    post(g, body)
    sent = json.loads(seen["chat"][0])["messages"]
    assert len(sent) == 1 and sent[0]["role"] == "user"
    assert sent[0]["content"] != text
    assert sent[0]["content"].startswith("line 0000") and sent[0]["content"].endswith("line 0399 of the pasted text")
    assert "[context-guardian: message 1 (" in sent[0]["content"]
    assert "archived as %s/0001.json --" % g.RUN_ID in sent[0]["content"], "the pointer names the span run-relative"
    assert str(tmp_path) not in sent[0]["content"], "an absolute path leaked into the prompt"
    assert g._state["summaries_generated"] == 0 and g._state["compactions_performed"] == 0
    spans = sorted((tmp_path / "spans" / g.RUN_ID).glob("[0-9][0-9][0-9][0-9].json"))
    assert len(spans) == 1
    span = json.loads(spans[0].read_text(encoding="utf-8"))
    assert span["oversized"] is True and span["messages"] == [{"role": "user", "content": text}]


def test_a_failing_short_partition_forwards_the_original(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)

    def boom(*a, **k):
        raise RuntimeError("partition bug")
    monkeypatch.setattr(g, "partition_short", boom)
    body = json.dumps({"model": "m", "stream": True, "messages": _three(8000)}).encode()
    assert post(g, body).status_code == 200
    assert seen["chat"] == [body]
