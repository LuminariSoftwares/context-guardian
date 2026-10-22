"""The MCP server reads .env before it answers its first tool call.

get_proxy_status builds the URL from GUARDIAN_HOST / GUARDIAN_PORT. Those are
read from os.environ, and nothing in the MCP server loaded the .env that
context-guardian-configure writes -- so a user on a non-default port got
"proxy not reachable at http://127.0.0.1:8786" until a span tool happened to
run first and load .env as a side effect of cg_recall_cli.span_dir(). The
order of the client's tool calls decided whether the status tool worked.
"""
import importlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_mcp  # noqa: E402
import cg_paths  # noqa: E402

from _child_env import child_env  # noqa: E402


def test_startup_loads_the_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_PORT", raising=False)
    monkeypatch.delenv("GUARDIAN_HOST", raising=False)
    monkeypatch.setenv("GUARDIAN_DATA_DIR", str(tmp_path))
    (tmp_path / cg_paths.ENV_FILENAME).write_text(
        "GUARDIAN_PORT=9999\nGUARDIAN_HOST=127.0.0.5\n", encoding="utf-8")

    mod = importlib.reload(cg_mcp)
    mod.load_config()
    tried = {}

    def boom(url, timeout=None):
        tried["url"] = url
        raise OSError("refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    try:
        mod._get_proxy_status({})
    except mod.ToolError:
        pass
    assert tried["url"] == "http://127.0.0.5:9999/guardian/stats"


def test_a_real_environment_variable_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDIAN_DATA_DIR", str(tmp_path))
    (tmp_path / cg_paths.ENV_FILENAME).write_text("GUARDIAN_PORT=9999\n", encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_PORT", "7000")
    mod = importlib.reload(cg_mcp)
    mod.load_config()
    tried = {}

    def boom(url, timeout=None):
        tried["url"] = url
        raise OSError("refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    try:
        mod._get_proxy_status({})
    except mod.ToolError:
        pass
    assert ":7000/" in tried["url"]


def test_the_server_process_loads_it_before_the_first_tool_call(tmp_path):
    """End to end over stdio: the first thing the client calls is the status
    tool, and the error names the port from .env."""
    (tmp_path / cg_paths.ENV_FILENAME).write_text("GUARDIAN_PORT=9998\n", encoding="utf-8")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_proxy_status", "arguments": {}}},
    ]
    env = child_env(pythonpath=ROOT, GUARDIAN_DATA_DIR=tmp_path)
    r = subprocess.run([sys.executable, str(ROOT / "cg_mcp.py")],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
    text = lines[-1]["result"]["content"][0]["text"]
    assert lines[-1]["result"]["isError"] is True
    assert ":9998/guardian/stats" in text, text


def test_the_mcp_server_and_the_proxy_share_one_data_dir_resolver():
    source = (ROOT / "cg_mcp.py").read_text(encoding="utf-8")
    assert "cg_paths" in source, "the MCP server resolves its own paths"
