"""The MCP server (cg_mcp.py, console script `context-guardian-mcp`).

Driven over the real protocol: a subprocess speaking newline-delimited
JSON-RPC 2.0 on stdio, exactly as an MCP client launches it. Every tool must
carry the spec's four annotation hints as booleans plus a title, and the hints
must be true to what the tool does.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


class Client:
    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "cg_mcp.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=env)
        self.next_id = 0

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id and reply["jsonrpc"] == "2.0"
        return reply

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        assert code == 0


@pytest.fixture()
def spans(tmp_path):
    run = tmp_path / "spans" / "20261015-120000"
    run.mkdir(parents=True)
    (run / "0001.json").write_text(json.dumps({
        "run_id": "20261015-120000", "index": 1, "summary": "parser fix agreed",
        "messages": [{"role": "user", "content": "please fix the tokenizer bug"},
                     {"role": "assistant", "content": "done, see parse.py"}]}),
        encoding="utf-8")
    return tmp_path / "spans"


@pytest.fixture()
def client(spans, monkeypatch):
    import os
    env = dict(os.environ, GUARDIAN_SPAN_DIR=str(spans),
               GUARDIAN_HOST="127.0.0.1", GUARDIAN_PORT="9")
    c = Client(env)
    init = c.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "pytest", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "context-guardian"
    assert "tools" in init["result"]["capabilities"]
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield c
    c.close()


def _tools(client):
    return {t["name"]: t for t in client.call("tools/list")["result"]["tools"]}


def test_every_tool_declares_all_four_hints_and_a_title(client):
    tools = _tools(client)
    assert tools, "the server lists no tools"
    for name, tool in tools.items():
        ann = tool.get("annotations")
        assert isinstance(ann, dict), name
        for hint in HINTS:
            assert isinstance(ann.get(hint), bool), (name, hint)
        assert isinstance(ann.get("title"), str) and ann["title"], name
        assert isinstance(tool.get("title"), str) and tool["title"], name
        assert tool["inputSchema"]["type"] == "object", name


def test_a_read_only_tool_says_so(client):
    ann = _tools(client)["search_spans"]["annotations"]
    assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False


def test_the_hints_match_what_each_tool_does(client):
    tools = _tools(client)
    # Nothing in this server writes or deletes.
    for name, tool in tools.items():
        assert tool["annotations"]["readOnlyHint"] is True, name
        assert tool["annotations"]["destructiveHint"] is False, name
    # Only the status tool opens a socket (to the proxy on loopback).
    assert tools["guardian_status"]["annotations"]["openWorldHint"] is True
    assert tools["search_spans"]["annotations"]["openWorldHint"] is False
    assert tools["read_span"]["annotations"]["openWorldHint"] is False


def test_search_spans_finds_an_archived_message(client):
    res = client.call("tools/call", {"name": "search_spans",
                                     "arguments": {"term": "TOKENIZER"}})["result"]
    assert res.get("isError") is not True
    text = res["content"][0]["text"]
    assert "0001.json" in text and "msg 0 [user]" in text


def test_read_span_returns_the_span(client):
    res = client.call("tools/call", {"name": "read_span", "arguments": {
        "run": "20261015-120000", "index": 1}})["result"]
    assert res.get("isError") is not True
    assert "parser fix agreed" in res["content"][0]["text"]
    assert "please fix the tokenizer bug" in res["content"][0]["text"]


@pytest.mark.parametrize("args", [{"run": "../..", "index": 1},
                                  {"run": "20261015-120000", "index": "x"},
                                  {"run": "20261015-120000", "index": 99}])
def test_read_span_refuses_bad_or_missing_spans(client, args):
    res = client.call("tools/call", {"name": "read_span", "arguments": args})["result"]
    assert res["isError"] is True


def test_status_with_no_proxy_is_a_tool_error_not_a_crash(client):
    res = client.call("tools/call", {"name": "guardian_status", "arguments": {}})["result"]
    assert res["isError"] is True
    assert "not reachable" in res["content"][0]["text"]
    # the server is still alive afterwards
    assert _tools(client)


def test_unknown_method_and_tool(client):
    assert client.call("no/such")["error"]["code"] == -32601
    res = client.call("tools/call", {"name": "nope", "arguments": {}})
    assert "error" in res or res["result"]["isError"] is True


def test_console_script_is_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'context-guardian-mcp = "cg_mcp:main"' in text
    assert '"cg_mcp"' in text


def test_server_version_matches_the_package():
    import re
    sys.path.insert(0, str(ROOT))
    import cg_mcp
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"), re.M).group(1)
    assert cg_mcp.SERVER_VERSION == version
