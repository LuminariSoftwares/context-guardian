"""A loopback-bound proxy answers only loopback names, and no web page at all.

The proxy binds 127.0.0.1 and fronts the backend with no authentication, which
is only safe while "on this machine" and "reachable" are the same thing. Two
ways they came apart:

  DNS rebinding -- a name an attacker controls resolves to 127.0.0.1, the
  browser (or any client) sends `Host: evil.example`, and the request arrives
  on loopback. `curl -H 'Host: evil.example' http://127.0.0.1:8786/guardian/stats`
  answered 200.

  A drive-by POST -- a page on any origin can send a `text/plain` body to
  http://127.0.0.1:8786/v1/chat/completions with no preflight, so visiting a
  web page spent the local GPU and reached the local model.

Both are rejected with 403 when the bind address is loopback. A non-loopback
bind is a deliberate choice (GUARDIAN_HOST), and keeps its old behaviour.
"""
import json

import pytest
from fastapi.testclient import TestClient

from _proxy_stub import load, wire


def _client(g):
    # base_url, not the default http://testserver: the Host header is what the
    # guard reads, so a test client that calls itself "testserver" would be
    # refused before it reached the behaviour under test.
    return TestClient(g.app, base_url="http://127.0.0.1:8786")


def _body():
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()


# --- the Host header ------------------------------------------------------

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.1:8786",
                                  "localhost:8786", "[::1]", "[::1]:8786",
                                  "127.0.0.2:8786"])
def test_loopback_hosts_are_accepted(monkeypatch, tmp_path, host):
    g = load(monkeypatch, tmp_path)
    r = _client(g).get("/guardian/stats", headers={"host": host})
    assert r.status_code == 200, host


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8786",
                                  "guardian.attacker.test", "0.0.0.0:8786",
                                  "192.168.1.9:8786"])
def test_non_loopback_hosts_are_refused(monkeypatch, tmp_path, host):
    g = load(monkeypatch, tmp_path)
    r = _client(g).get("/guardian/stats", headers={"host": host})
    assert r.status_code == 403, host
    assert r.json()["error"]["type"] == "host_not_allowed"
    assert len(r.text.splitlines()) == 1


def test_a_chat_request_with_a_foreign_host_never_reaches_the_upstream(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    r = _client(g).post("/v1/chat/completions", content=_body(),
                        headers={"content-type": "application/json",
                                 "host": "evil.example"})
    assert r.status_code == 403
    assert seen["chat"] == []


# --- the Origin header ---------------------------------------------------

def test_no_origin_header_is_the_normal_client(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    r = _client(g).post("/v1/chat/completions", content=_body(),
                        headers={"content-type": "application/json"})
    assert r.status_code == 200
    assert len(seen["chat"]) == 1


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:5173",
                                    "http://[::1]:8080", "https://localhost"])
def test_loopback_origins_are_accepted(monkeypatch, tmp_path, origin):
    g = load(monkeypatch, tmp_path)
    r = _client(g).get("/guardian/stats", headers={"origin": origin})
    assert r.status_code == 200, origin


@pytest.mark.parametrize("origin", ["https://evil.example", "http://attacker.test:8080",
                                    "null", "http://192.168.1.9"])
def test_cross_origin_requests_are_refused(monkeypatch, tmp_path, origin):
    g = load(monkeypatch, tmp_path)
    seen = wire(g)
    r = _client(g).post("/v1/chat/completions", content=_body(),
                        headers={"content-type": "text/plain", "origin": origin})
    assert r.status_code == 403, origin
    assert r.json()["error"]["type"] == "origin_not_allowed"
    assert seen["chat"] == []


def test_an_extra_origin_can_be_allowed(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path,
             GUARDIAN_ALLOWED_ORIGINS="https://ui.example, http://other.test:9000")
    c = _client(g)
    assert c.get("/guardian/stats", headers={"origin": "https://ui.example"}).status_code == 200
    assert c.get("/guardian/stats", headers={"origin": "http://other.test:9000"}).status_code == 200
    assert c.get("/guardian/stats", headers={"origin": "https://nope.example"}).status_code == 403


# --- a deliberate non-loopback bind keeps its old behaviour ---------------

def test_a_non_loopback_bind_checks_nothing(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_HOST="0.0.0.0")
    c = _client(g)
    assert c.get("/guardian/stats", headers={"host": "guardian.lan"}).status_code == 200
    assert c.get("/guardian/stats", headers={"origin": "https://evil.example"}).status_code == 200


def test_the_health_page_is_guarded_too(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path)
    assert _client(g).get("/guardian/health",
                          headers={"host": "evil.example"}).status_code == 403
