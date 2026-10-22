"""`tools/list` is sent to the model on every single request, so its size is a
contract, not a detail.

Measured the way the 2026-10 audit measured it: start the real server over
stdio, speak MCP to it, take json.dumps(result["tools"]) with json's default
separators and divide the character count by 3.5 -- the same characters-per-
token estimate the proxy itself uses.

0.10.0 shipped at about 445 tokens. Adding list_spans and an outputSchema for
get_proxy_status took it to about 2,116, nearly five times the payload on every
request, to describe four read-only tools. The budget below is the ceiling;
descriptions are not allowed to grow into it to raise a directory score.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Hard ceiling on the serialised tools array, in estimated tokens.
BUDGET = 1100
CHARS_PER_TOKEN = 3.5
PROTOCOL_VERSION = "2025-06-18"


def tools_over_stdio(module="cg_mcp.py", *args):
    """The tools array the real server answers tools/list with."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "budget", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.run(
        [sys.executable, str(ROOT / module), *args],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {r.get("id"): r for r in replies}
    assert by_id[1]["result"]["protocolVersion"] == PROTOCOL_VERSION
    return by_id[2]["result"]["tools"]


def measure(tools) -> int:
    return round(len(json.dumps(tools)) / CHARS_PER_TOKEN)


def test_tools_list_fits_the_budget():
    tools = tools_over_stdio()
    tokens = measure(tools)
    per_tool = ", ".join("%s=%d" % (t["name"], measure(t)) for t in tools)
    assert tokens <= BUDGET, (
        "tools/list is %d estimated tokens, over the %d-token budget (%s). "
        "This payload is sent on every request: shorten the descriptions and "
        "the outputSchema property descriptions rather than raising the budget."
        % (tokens, BUDGET, per_tool))


def test_the_budget_covers_the_whole_surface():
    """A budget that measured only some of the tools would not be a budget."""
    tools = tools_over_stdio()
    assert sorted(t["name"] for t in tools) == [
        "get_proxy_status", "list_spans", "read_span", "search_spans"]


def test_the_console_script_entry_point_measures_the_same():
    """`context-guardian mcp` is what the MCP Registry runs."""
    assert tools_over_stdio("context_guardian.py", "mcp") == tools_over_stdio()
