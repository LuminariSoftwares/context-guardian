"""P111 S3 review addendum (O5 2026-10-30): a spilled first-message paste reports what was FORWARDED. DO NOT EDIT.

S2 forwards a single paste bigger than the window as head + pointer + tail, with no compaction (nothing older to
summarise). /guardian/usage must then show the size of that cut request -- the proxy's count of what it forwarded --
not the original paste, and `last_compaction_at` stays null because nothing was compacted.
"""
import json

from _proxy_stub import LOOPBACK, load, post, wire


def test_spilled_first_paste_reports_the_forwarded_size(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_NUM_CTX="1000", GUARDIAN_DETECT_CONTEXT="0",
             GUARDIAN_KEEP_RECENT_MESSAGES="2", GUARDIAN_CALIBRATE="0")
    seen = wire(g)
    paste = "\n".join("P%04d payload line with some ordinary words in it" % i for i in range(400))
    assert post(g, json.dumps({"model": "m", "messages": [{"role": "user", "content": paste}]}).encode()).status_code == 200
    from fastapi.testclient import TestClient
    tc = TestClient(g.app, base_url=LOOPBACK)
    stats = tc.get("/guardian/stats").json()
    assert stats["oversized_spills"] == 1, "the paste should have been spilled (S2)"
    body = tc.get("/guardian/usage").json()
    forwarded = json.loads(seen["chat"][-1])["messages"]
    assert body["used_after_compaction"] == g.estimate_tokens(forwarded) + g.estimate_tool_tokens({})
    assert body["used_after_compaction"] < stats["last_known_total_tokens"], "not the original paste"
    assert body["pct"] <= 100.0 and body["last_compaction_at"] is None
