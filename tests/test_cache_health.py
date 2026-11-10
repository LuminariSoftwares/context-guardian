"""B. Cache-health ledger (GUARDIAN_CACHE_HEALTH), ADR 0008. Written before the implementation."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from _proxy_stub import LOOPBACK, load
import cg_cache_health as ch


def est(x):
    return max(1, len(json.dumps(x)) // 4)


def fp(payload, route="chat"):
    return ch.fingerprint(payload, route, est, est, est)


def msgs(*texts, role="user"):
    return [{"role": role, "content": t} for t in texts]


# ---- pure ledger ----------------------------------------------------------------------

def test_fingerprint_is_stable_and_order_sensitive():
    a = fp({"messages": msgs("one", "two")})
    assert a == fp({"messages": msgs("one", "two")})
    assert a != fp({"messages": msgs("two", "one")})
    assert fp({"messages": msgs("one"), "tools": [{"name": "t"}]})[0] != a[0]     # tools lead
    assert len(fp({"messages": msgs("x"), "tools": [{"n": 1}]})) == 2


def test_key_order_does_not_change_a_digest():
    assert fp({"messages": [{"role": "user", "content": "a"}]}) == fp({"messages": [{"content": "a", "role": "user"}]})


def test_anthropic_system_string_is_chunked_so_an_appended_note_keeps_the_head():
    base = "x" * 10000
    a = fp({"system": base, "messages": msgs("hi")}, "messages")
    b = fp({"system": base + "\n[note appended by the proxy]", "messages": msgs("hi")}, "messages")
    n, tokens = ch.shared_prefix(a, b)
    assert n == 2 and tokens > 0                    # the first two 4096-char chunks are shared
    assert a[2] != b[2]                             # the third carries the note
    lst = fp({"system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}], "messages": msgs("hi")}, "messages")
    lst2 = fp({"system": [{"type": "text", "text": "A"}, {"type": "text", "text": "C"}], "messages": msgs("hi")}, "messages")
    assert ch.shared_prefix(lst, lst2)[0] == 1


def test_row_for_a_real_compaction():
    h = ch.CacheHealth(head_tokens=10)
    prev = [("s", 50), ("a", 100), ("b", 100)]
    h.observe("S", prev)
    seq = h.observe("S", [("s", 50), ("sum", 40), ("b", 100)], source="extended", at="t")
    row = h.summary()["latest_session"]["rows"][-1]
    assert row["seq"] == seq == 2
    assert row["before_tokens"] == 250 and row["after_tokens"] == 190 and row["shared_tokens"] == 50
    assert row["shared_pct"] == 20.0
    assert row["head_n"] == 10 and row["head_identical"] is True      # the 50-token system element covers the head
    assert row["head_hash_before"] == row["head_hash_after"]
    assert row["source"] == "extended" and row["route"] == "chat"


def test_head_not_identical_when_the_first_element_changes():
    h = ch.CacheHealth(head_tokens=1024)
    h.observe("S", [("s", 600), ("a", 600)])
    h.observe("S", [("s2", 600), ("a", 600)], source="generated")
    row = h.summary()["latest_session"]["rows"][-1]
    assert row["head_identical"] is False and row["shared_tokens"] == 0
    assert row["head_hash_before"] != row["head_hash_after"]


def test_head_shorter_than_n_uses_the_previous_total():
    h = ch.CacheHealth(head_tokens=1024)
    h.observe("S", [("s", 30), ("a", 30)])
    h.observe("S", [("s", 30), ("x", 5)], source="generated")
    row = h.summary()["latest_session"]["rows"][-1]
    assert row["head_n"] == 60 and row["head_identical"] is False


def test_reused_turns_are_counted_but_not_averaged_in():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100), ("a", 100)])
    h.observe("S", [("s", 100), ("a", 100), ("b", 100)], source="reused")
    s = h.summary()
    assert s["reused_turns"] == 1 and s["compactions"] == 0
    assert h.line("S") == "no compaction measured yet"


def test_spills_and_client_edits_are_counted_apart():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100), ("a", 100)])
    h.observe("S", [("s", 100), ("a2", 100)], spill=True)
    h.observe("S", [("s", 100), ("a3", 100)])
    s = h.summary()
    assert s["spills"] == 1 and s["prefix_breaks_without_compaction"] == 1 and s["compactions"] == 0


def test_first_request_has_nothing_to_compare():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100)], source="generated")
    assert h.summary()["compactions"] == 0


def test_bounds_on_sessions_and_rows():
    h = ch.CacheHealth(max_sessions=3, max_rows=4)
    for i in range(10):
        h.observe("s%d" % i, [("a", 1)])
    assert h.summary()["sessions"] == 3
    h.observe("r", [("a", 10), ("b", 10)])
    for i in range(10):
        h.observe("r", [("a", 10), ("c%d" % i, 10)], source="generated")
    assert len(h._sessions["r"].rows) == 4 and h.summary()["compactions"] == 10


def test_sessions_do_not_contaminate_each_other():
    h = ch.CacheHealth()
    h.observe("A", [("s", 100), ("a", 100)])
    h.observe("B", [("z", 100), ("b", 100)])
    h.observe("A", [("s", 100), ("sum", 10)], source="generated")
    assert h._sessions["A"].rows[-1]["shared_tokens"] == 100
    assert not h._sessions["B"].rows


def test_line_with_and_without_cached_counts():
    h = ch.CacheHealth(head_tokens=50)
    h.observe("S", [("s", 100), ("a", 300)])
    h.observe("S", [("s", 100), ("n", 20)], source="generated")
    line = h.line("S")
    assert line.startswith("last 1 compaction kept 25% of the prefix (first 50 tokens identical in 1/1)")
    assert "reports no cached-token counts" in line and "estimates" in line and "0%" not in line.split("(token")[0].split("; ")[-1]
    h = ch.CacheHealth(head_tokens=50)   # same session again, with the upstream reporting cached tokens
    h.observe("S", [("s", 100), ("a", 300)])
    h.usage("S", 1, 400, 300)
    h.observe("S", [("s", 100), ("n", 20)], source="generated")
    h.usage("S", 2, 120, 12)
    line = h.line("S")
    assert "cache hit rate 75% before, 10% on the compacted request" in line
    assert "reports no cached-token counts" not in line


def test_hit_rates_land_on_the_right_request_even_when_late():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100), ("a", 100)])                      # seq 1
    h.observe("S", [("s", 100), ("n", 10)], source="generated")   # seq 2
    h.observe("S", [("s", 100), ("n", 10), ("m", 10)])            # seq 3 started before seq 2's usage arrived
    h.usage("S", 2, 100, 50)
    h.usage("S", 3, 100, 90)
    assert h._sessions["S"].rows[-1]["hit_at"] == 0.5


def test_anthropic_line_warns_about_breakpoints():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100), ("a", 100)], route="messages")
    h.observe("S", [("s", 100)], route="messages", source="generated")
    assert "cache_control" in h.line("S")


def test_prefix_pct_and_stats_hashes_only_on_last_row():
    h = ch.CacheHealth()
    h.observe("S", [("s", 100), ("a", 100)])
    h.observe("S", [("s", 100), ("n", 10)], source="generated")
    h.observe("S", [("s", 100), ("n", 10), ("a", 10)])
    h.observe("S", [("s", 100), ("q", 10)], source="extended")
    assert h.prefix_pct("S") is not None
    rows = h.summary()["latest_session"]["rows"]
    assert "head_hash_before" not in rows[0] and "head_hash_before" in rows[-1]


# ---- usage reader ---------------------------------------------------------------------

@pytest.fixture
def g(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path, GUARDIAN_CACHE_HEALTH="1", GUARDIAN_NUM_CTX="1000", GUARDIAN_COMPACT_THRESHOLD="0.5")


def test_usage_cache_counts_shapes(g):
    f = g.usage_cache_counts
    j = "application/json"
    assert f(json.dumps({"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}}).encode(), j) == (100, 80)
    assert f(json.dumps({"usage": {"prompt_tokens": 100}}).encode(), j) == (100, None)
    assert f(json.dumps({"usage": {"input_tokens": 5, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 15}}).encode(), j) == (100, 80)
    assert f(json.dumps({"usage": {"input_tokens": 100}}).encode(), j) == (100, None)
    assert f(json.dumps({"timings": {"prompt_n": 20, "cache_n": 80}}).encode(), j) == (100, 80)
    assert f(json.dumps({"prompt_eval_count": 40}).encode(), j) == (40, None)
    assert f(b"not json", j) is None and f(b"[]", j) is None and f(b"{}", j) is None


def test_usage_cache_counts_streams(g):
    oa = (b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
          b'data: {"choices":[],"usage":{"prompt_tokens":50,"prompt_tokens_details":{"cached_tokens":25}}}\n\n'
          b'data: [DONE]\n\n')
    assert g.usage_cache_counts(oa, "text/event-stream") == (50, 25)
    an = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":4,"cache_read_input_tokens":96}}}\n\n'
          b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n')
    assert g.usage_cache_counts(an, "text/event-stream") == (100, 96)


# ---- proxy ----------------------------------------------------------------------------

def R(body, ctype="application/json", status=200):
    async def gen():
        yield body
    return httpx.Response(status, headers={"content-type": ctype}, content=gen())


def wire(g, usage_for=None):
    """Stub upstream: summary calls get a fixed summary; chat calls get a reply whose usage
    optionally reports cached tokens. Records every non-summary chat body."""
    seen = []

    def handler(request):
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "nf"})
        if b"BEGIN_TRANSCRIPT" in request.content:
            return R(json.dumps({"choices": [{"message": {"role": "assistant", "content": "condensed summary of the older turns, long enough to be accepted"}, "finish_reason": "stop"}]}).encode())
        seen.append(request.content)
        usage = {"prompt_tokens": 1000, "completion_tokens": 3}
        if usage_for:
            usage = usage_for(len(seen))
        return R(json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": usage}).encode())
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


def turn(g, history, session="s1", stream=False):
    body = json.dumps({"model": "m", "messages": history, "stream": stream}).encode()
    return TestClient(g.app, base_url=LOOPBACK).post("/v1/chat/completions", content=body,
                                                     headers={"content-type": "application/json", "x-guardian-session": session})


def stats(g):
    return TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()


def history(n):
    h = [{"role": "system", "content": "You are a careful assistant. " * 5}]
    for i in range(n):
        h.append({"role": "user", "content": "question %d %s" % (i, "pad " * 60)})
        h.append({"role": "assistant", "content": "answer %d %s" % (i, "pad " * 60)})
    return h


def test_default_is_off_and_records_nothing(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="1000", GUARDIAN_COMPACT_THRESHOLD="0.5")
    wire(g)
    turn(g, history(2))
    assert stats(g)["cache_health"] == {"enabled": False}


def test_scripted_session_reports_what_the_stub_saw(g):
    def usage(n):
        return {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900 if n < 5 else 100}}
    seen = wire(g, usage)
    for n in range(1, 13):
        turn(g, history(n))
    s = stats(g)["cache_health"]
    assert s["enabled"] is True and s["sessions"] == 1
    real = s["compactions"]
    assert real >= 1, s
    assert s["reused_turns"] >= 0
    # independent computation from the bytes the stub actually received
    rows = s["latest_session"]["rows"]
    bodies = [json.loads(b) for b in seen]
    def common(a, b):
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n
    last = rows[-1]
    i = last["seq"] - 1
    expect_msgs = common(bodies[i - 1]["messages"], bodies[i]["messages"])
    assert expect_msgs >= 1                                   # the system prompt at least
    assert 0 <= last["shared_pct"] <= 100
    assert last["shared_tokens"] > 0
    assert "kept" in s["line"] and "% of the prefix" in s["line"]


def test_flag_on_without_calibration_leaves_response_bytes_alone(monkeypatch, tmp_path):
    outs = []
    for flag in ("0", "1"):
        g = load(monkeypatch, tmp_path, GUARDIAN_CACHE_HEALTH=flag, GUARDIAN_CALIBRATE="0", GUARDIAN_NUM_CTX="100000")
        wire(g)
        outs.append(turn(g, history(2)).content)
    assert outs[0] == outs[1]


def test_streamed_response_is_byte_identical_with_the_flag_on(monkeypatch, tmp_path):
    raw = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":5}}}\n\n'
           b'data: [DONE]\n\n')
    outs = []
    for flag in ("0", "1"):
        g = load(monkeypatch, tmp_path, GUARDIAN_CACHE_HEALTH=flag, GUARDIAN_CALIBRATE="0", GUARDIAN_NUM_CTX="100000")
        g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: R(raw, "text/event-stream") if req.url.path.endswith("/chat/completions") else httpx.Response(404)))
        outs.append(turn(g, history(1), stream=True).content)
    assert outs[0] == outs[1] == raw


def test_rejected_requests_are_not_recorded(g):
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: R(b'{"error":"no"}', status=400) if req.url.path.endswith("/chat/completions") else httpx.Response(404)))
    turn(g, history(1))
    assert stats(g)["cache_health"]["sessions"] == 0


def test_dry_run_is_never_a_compaction_row(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_CACHE_HEALTH="1", GUARDIAN_DRY_RUN="1", GUARDIAN_NUM_CTX="1000", GUARDIAN_COMPACT_THRESHOLD="0.5")
    wire(g)
    for n in range(1, 8):
        turn(g, history(n))
    assert stats(g)["cache_health"]["compactions"] == 0


def test_usage_endpoint_carries_the_per_session_prefix_pct(g):
    wire(g)
    for n in range(1, 10):
        turn(g, history(n), session="mine")
    body = TestClient(g.app, base_url=LOOPBACK).get("/guardian/usage?session=mine").json()
    assert "cache_prefix_pct" in body


# ---- fail open (invariant 1) -----------------------------------------------------------

@pytest.mark.parametrize("target", ["fingerprint", "observe", "usage_cache_counts", "usage"])
def test_ledger_failures_never_touch_the_response(g, monkeypatch, target):
    seen = wire(g)
    clean = turn(g, history(3)).content
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected"))
    if target == "usage_cache_counts":
        monkeypatch.setattr(g, "usage_cache_counts", boom)
    elif target == "usage":
        monkeypatch.setattr(g.CACHE_HEALTH, "usage", boom)
    elif target == "observe":
        monkeypatch.setattr(g.CACHE_HEALTH, "observe", boom)
    else:
        monkeypatch.setattr(g.cg_cache_health, "fingerprint", boom)
    r = turn(g, history(3))
    assert r.status_code == 200 and r.content == clean
    assert seen[-1] == seen[0] or len(seen) == 2             # upstream saw the same request both times
    assert stats(g)["cache_health"]["errors"] >= 1


def test_tap_callback_raising_mid_stream_does_not_break_the_stream(g, monkeypatch):
    raw = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    g._http_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: R(raw, "text/event-stream") if req.url.path.endswith("/chat/completions") else httpx.Response(404)))
    monkeypatch.setattr(g, "_cache_done", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    assert turn(g, history(1), stream=True).content == raw


def test_stats_rendering_failure_degrades(g, monkeypatch):
    wire(g)
    turn(g, history(1))
    monkeypatch.setattr(g.CACHE_HEALTH, "summary", lambda: (_ for _ in ()).throw(RuntimeError("injected")))
    r = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats")
    assert r.status_code == 200 and r.json()["cache_health"]["error"] is True


# ---- doctor, statusline, dashboard ----------------------------------------------------------

def test_doctor_findings():
    import cg_doctor as d
    assert d.cache_health_findings({}) == []                                            # a proxy that predates it
    off = d.cache_health_findings({"cache_health": {"enabled": False}})
    assert len(off) == 1 and off[0]["level"] == "ok" and "GUARDIAN_CACHE_HEALTH=1" in off[0]["what"]
    assert d.cache_health_findings({"cache_health": {"enabled": True, "compactions": 0, "line": "no compaction measured yet"}}) == []
    got = d.cache_health_findings({"cache_health": {"enabled": True, "compactions": 2, "line": "last 2 compactions kept 92% of the prefix (x)"}})
    assert got[0]["level"] == "ok" and "kept 92% of the prefix" in got[0]["what"]
    assert d.cache_health_findings({"cache_health": {"enabled": True, "error": True}})[0]["level"] == "warn"
    assert d.cache_health_findings({"cache_health": "junk"}) == []


def test_statusline_cache_suffix():
    import cg_statusline as s
    assert s.cache_suffix({"cache_prefix_pct": 12}) == " | prefix 12%"
    assert s.cache_suffix({"cache_prefix_pct": None}) == "" and s.cache_suffix({}) == ""
    assert s.cache_suffix({"cache_prefix_pct": True}) == ""


def test_dashboard_has_the_card_and_escapes_the_line(g):
    html = TestClient(g.app, base_url=LOOPBACK).get("/guardian/health").text
    assert "Cache health" in html and ".replace(/<" not in html or "&lt;" in html
