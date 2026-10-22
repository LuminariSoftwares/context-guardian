"""Item 2 (2026-10-14): GUARDIAN_DRY_RUN=1 computes the compaction and forwards
the original request unchanged.

The point of a dry run is to see what the proxy WOULD do to a live session
before trusting it to do it. So the upstream must receive exactly the bytes the
client sent, and the log must carry the same record a real compaction writes.
"""
import json

import pytest

from _proxy_stub import LOOPBACK, SUMMARY, events, load, post, wire


def _over_budget_body():
    msgs = [{"role": "user", "content": "x" * 4000} for _ in range(5)]
    # Deliberately NOT json.dumps' default spacing, so a re-serialised body
    # cannot pass for the original.
    return json.dumps({"model": "m", "stream": True, "messages": msgs},
                      separators=(",", ":")).encode()


def test_dry_run_forwards_the_original_bytes_and_logs_the_event(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_DRY_RUN="1",
             GUARDIAN_KEEP_RECENT_MESSAGES="2")
    seen = wire(g)
    body = _over_budget_body()
    r = post(g, body)
    assert r.status_code == 200
    assert seen["chat"] == [body], "dry run changed what reached the upstream"

    dry = events(g, "dry_run")
    assert len(dry) == 1
    assert events(g, "compaction") == []
    ev = dry[0]
    assert ev["messages_before"] == 5 and ev["messages_after"] == 3
    assert ev["tokens_saved"] > 0
    assert ev["summary_preview"] == SUMMARY
    assert ev["span_written"] is False
    assert g._state["dry_runs"] == 1
    assert g._state["compactions_performed"] == 0
    assert g._state["tokens_saved_total"] == 0


def test_dry_run_event_has_the_same_fields_as_a_compaction(monkeypatch, tmp_path):
    real = load(monkeypatch, tmp_path / "real", GUARDIAN_KEEP_RECENT_MESSAGES="2")
    wire(real)
    post(real, _over_budget_body())
    real_keys = set(events(real, "compaction")[0])

    dry = load(monkeypatch, tmp_path / "dry", GUARDIAN_DRY_RUN="1",
               GUARDIAN_KEEP_RECENT_MESSAGES="2")
    wire(dry)
    post(dry, _over_budget_body())
    assert set(events(dry, "dry_run")[0]) == real_keys


def test_dry_run_is_a_counter_on_stats_and_the_dashboard(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    g = load(monkeypatch, tmp_path, GUARDIAN_DRY_RUN="1")
    stats = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()
    assert stats["dry_run"] is True and stats["dry_runs"] == 0
    html = TestClient(g.app, base_url=LOOPBACK).get("/guardian/health").text
    assert 'card("Dry runs"' in html


def test_a_failing_dry_run_still_forwards_the_request(monkeypatch, tmp_path):
    """Fail-open: an exception inside the dry-run bookkeeping is a lost log
    line, never a lost request."""
    g = load(monkeypatch, tmp_path, GUARDIAN_DRY_RUN="1",
             GUARDIAN_KEEP_RECENT_MESSAGES="2")
    seen = wire(g)

    def boom(*a, **k):
        raise RuntimeError("dry-run bookkeeping broke")
    monkeypatch.setattr(g, "_compaction_event", boom)
    body = _over_budget_body()
    r = post(g, body)
    assert r.status_code == 200
    assert seen["chat"] == [body]


@pytest.mark.parametrize("dry", ["0", ""])
def test_dry_run_is_off_by_default(monkeypatch, tmp_path, dry):
    g = load(monkeypatch, tmp_path, GUARDIAN_DRY_RUN=dry)
    assert g.DRY_RUN is False


def test_an_exception_in_compaction_forwards_the_original(monkeypatch, tmp_path):
    """Fail-open for the whole rewrite step: whatever raises inside it, the
    request goes upstream exactly as the client sent it."""
    g = load(monkeypatch, tmp_path)
    seen = wire(g)

    async def boom(client, payload):
        raise RuntimeError("compaction bug")
    monkeypatch.setattr(g, "maybe_compact", boom)
    body = _over_budget_body()
    r = post(g, body)
    assert r.status_code == 200
    assert seen["chat"] == [body]
