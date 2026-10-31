"""P111 S2 (B2) acceptance -- one message bigger than the window. Written by the O5 overseer; DO NOT EDIT.

Why: README "What this does not do" (5): "It does not protect a single message that is bigger than the window" --
one huge paste overflows the backend however well the rest is compacted, and the commonest case (a big paste as
the FIRST message) never compacts at all because there is nothing older to summarise.

Contract (plan P111 rev 1, decision 3 + C2; two details pinned here by O5):
- Spill is the LAST resort: only when the request still exceeds the input budget after normal compaction -- and
  ALSO when there is nothing older to summarise (O5: a first-message paste must be covered too).
- The single largest message of what would be forwarded (any role, the newest user message included) is written as
  its own span: sealed in chain.jsonl like every span, span JSON carries `"oversized": true` and
  `"original_tokens": <int>`, and its `messages` hold the full original text.
- In the FORWARDED request that message becomes: as much of its head and tail as fits (head : tail about 2 : 1, cut
  at line boundaries), with one pointer line in between that matches POINTER below. The forwarded request fits the
  input budget (effective_threshold) by the proxy's own count. A message that is not the spilled one is forwarded
  unchanged.
- No spill when compaction alone fits -- a large but fitting message is forwarded verbatim.
- The compaction event gains `oversized_spilled` (count); `/guardian/stats` gains `oversized_spills` (running count);
  when nothing older could be summarised, an event named "oversized_spill" is logged instead, with the same field.
- README: bullet (5) is gone and a "Big pastes" section says a plain chat client sees only the cut and the recall
  MCP server opens the full text; docs/ARCHIVE_FORMAT.md documents `oversized`.
"""
import json
import re
from pathlib import Path

import cg_archive
from _proxy_stub import LOOPBACK, load, post, wire

ROOT = Path(__file__).resolve().parent.parent
POINTER = re.compile(r"\[context-guardian: message \d+ \(\d+ tokens\) archived as [^\s\]]+\d{4}\.json -- the middle "
                     r"\d+ tokens are omitted; with the recall MCP server, search_spans/read_span open the full text\]")


def big(n_lines=400, tag="L"):
    return "\n".join("%s%04d payload line with some ordinary words in it" % (tag, i) for i in range(n_lines))


def small(n, tag="s"):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "%s%02d short turn about the plan" % (tag, i)}
            for i in range(n)]


def proxy(monkeypatch, tmp_path, **env):
    base = {"GUARDIAN_NUM_CTX": "1000", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_KEEP_RECENT_MESSAGES": "2",
            "GUARDIAN_CALIBRATE": "0"}
    base.update(env)
    g = load(monkeypatch, tmp_path, **base)
    seen = wire(g)
    return g, seen


def send(g, messages):
    return post(g, json.dumps({"model": "m", "messages": messages}).encode())


def forwarded(seen):
    assert seen["chat"], "nothing was forwarded"
    return json.loads(seen["chat"][-1])["messages"]


def spans(g):
    d = Path(g.SPAN_DIR) / g.RUN_ID
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("[0-9][0-9][0-9][0-9].json"))]


def events(g, name):
    p = Path(g.LOG_PATH)
    return [e for e in (json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("event") == name] if p.exists() else []


def oversized_span(g):
    found = [s for s in spans(g) if s.get("oversized") is True]
    assert len(found) == 1, [sorted(s) for s in spans(g)]
    return found[0]


# ------------------------------------------------------------------------------------------------------------------

def test_no_spill_when_compaction_alone_fits(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    fitting = small(50) + [{"role": "user", "content": big(10, "F")}]   # ~150 tokens: large, but fits after compaction
    assert send(g, fitting).status_code == 200
    fwd = forwarded(seen)
    assert events(g, "compaction"), "the fixture must compact (it is over the 500-token input budget)"
    assert fwd[-1]["content"] == fitting[-1]["content"]
    assert not any(s.get("oversized") for s in spans(g))
    assert all(not e.get("oversized_spilled") for e in events(g, "compaction"))


def test_newest_user_message_is_spilled_with_head_tail_and_pointer(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    paste = big(400, "N")
    convo = small(12) + [{"role": "user", "content": paste}]
    assert send(g, convo).status_code == 200
    last = forwarded(seen)[-1]["content"]
    assert last != paste
    assert POINTER.search(last), last[-400:]
    assert "N0000 " in last and "N0399 " in last, "head and tail must both survive"
    assert "N0200 " not in last, "the middle must be cut"


def test_the_forward_fits_the_budget(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    convo = small(12) + [{"role": "user", "content": big(400, "B")}]
    send(g, convo)
    fwd = forwarded(seen)
    assert g.estimate_tokens(fwd) <= g.effective_threshold(num_ctx=1000), g.estimate_tokens(fwd)


def test_head_is_about_twice_the_tail_and_cut_at_lines(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    send(g, small(12) + [{"role": "user", "content": big(400, "H")}])
    last = forwarded(seen)[-1]["content"]
    head, _, tail = POINTER.split(last, maxsplit=1)[0], None, POINTER.split(last, maxsplit=1)[1]
    head_lines = [x for x in head.splitlines() if re.match(r"H\d{4} ", x)]
    tail_lines = [x for x in tail.splitlines() if re.match(r"H\d{4} ", x)]
    assert head_lines and tail_lines
    assert all(x.endswith("words in it") for x in head_lines + tail_lines), "cut mid-line"
    ratio = len(head_lines) / len(tail_lines)
    assert 1.4 <= ratio <= 2.8, ratio


def test_the_span_is_sealed_marked_and_holds_the_full_text(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    paste = big(400, "S")
    send(g, small(12) + [{"role": "user", "content": paste}])
    span = oversized_span(g)
    assert isinstance(span.get("original_tokens"), int) and span["original_tokens"] > 1000
    assert any(m.get("content") == paste for m in span["messages"])
    report = cg_archive.verify_run(Path(g.SPAN_DIR) / g.RUN_ID)
    assert report.status == "ok", (report.status, report.kind, report.detail)


def test_a_middle_message_can_be_the_one_spilled(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    convo = small(12) + [{"role": "assistant", "content": big(400, "A")},
                         {"role": "user", "content": "thanks, now summarise it"}]
    send(g, convo)
    fwd = forwarded(seen)
    assert fwd[-1]["content"] == "thanks, now summarise it"
    assert any(POINTER.search(m.get("content") or "") for m in fwd[:-1])
    assert oversized_span(g)


def test_a_first_message_paste_is_covered(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    paste = big(400, "P")
    assert send(g, [{"role": "system", "content": "You help."}, {"role": "user", "content": paste}]).status_code == 200
    fwd = forwarded(seen)
    assert fwd[0]["content"] == "You help."
    assert POINTER.search(fwd[-1]["content"])
    assert g.estimate_tokens(fwd) <= g.effective_threshold(num_ctx=1000)
    ev = events(g, "oversized_spill") + events(g, "compaction")
    assert any(e.get("oversized_spilled") == 1 for e in ev), ev


def test_events_and_stats_count_spills(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    g, seen = proxy(monkeypatch, tmp_path)
    send(g, small(12) + [{"role": "user", "content": big(400, "E")}])
    assert any(e.get("oversized_spilled") == 1 for e in events(g, "compaction") + events(g, "oversized_spill"))
    st = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()
    assert st.get("oversized_spills") == 1


def test_readme_and_archive_format():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "It does not protect a single message that is bigger than the window" not in readme
    assert re.search(r"^#+ .*Big pastes", readme, re.M)
    section = readme[readme.index("Big pastes"):][:3000]
    assert "recall" in section and "plain" in section.lower()
    assert "oversized" in (ROOT / "docs" / "ARCHIVE_FORMAT.md").read_text(encoding="utf-8")
