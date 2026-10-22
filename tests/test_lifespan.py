"""Item 1 (2026-10-14): startup/shutdown run from a lifespan, not @app.on_event.

FastAPI deprecated on_event; every import of the proxy emitted two
DeprecationWarnings, which buried any real warning in the pytest summary.
"""
import importlib
import sys
import warnings
from pathlib import Path

from _proxy_stub import LOOPBACK, load


def test_importing_the_proxy_emits_no_on_event_deprecation(monkeypatch, tmp_path):
    load(monkeypatch, tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(sys.modules["context_guardian"])
    on_event = [w for w in caught if "on_event" in str(w.message)]
    assert on_event == [], [str(w.message) for w in on_event]


def test_the_source_no_longer_uses_on_event(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    assert "@app.on_event" not in Path(g.__file__).read_text(encoding="utf-8")


def test_the_lifespan_client_carries_a_request_and_closes(monkeypatch, tmp_path):
    """Fail-open path under the lifespan: the client the lifespan built is the
    one requests use, an upstream that refuses the connection is still the
    readable 502 (never a 503 'not ready' or a 500), and shutdown closes it."""
    from fastapi.testclient import TestClient

    g = load(monkeypatch, tmp_path, GUARDIAN_UPSTREAM_URL="http://127.0.0.1:9/v1")
    g._http_client = None
    with TestClient(g.app, base_url=LOOPBACK) as client:
        assert g._http_client is not None
        r = client.get("/v1/models")
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "upstream_unreachable"
    assert g._http_client.is_closed
