"""`context-guardian mcp` starts the same stdio MCP server as `context-guardian-mcp`; the MCP Registry entry runs
`uvx context-guardian mcp`, so this is the command a registry client actually launches."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_context_guardian_mcp_subcommand_speaks_mcp(tmp_path):
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    lst = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r = subprocess.run([sys.executable, str(ROOT / "context_guardian.py"), "mcp"], input=req + "\n" + lst + "\n",
                       capture_output=True, text=True, timeout=60, cwd=tmp_path)
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
    assert lines[0]["result"]["serverInfo"]["name"] == "context-guardian"
    names = sorted(t["name"] for t in lines[1]["result"]["tools"])
    assert names == ["get_proxy_status", "list_spans", "read_span", "search_spans"]
