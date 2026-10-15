"""Glama scores each MCP tool on how its description and parameters read. Every parameter is documented, every
description says what comes back and when to use a sibling, and the README's MCP section is complete."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_mcp  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
NAMES = [t["name"] for t in cg_mcp.TOOLS]


def test_every_parameter_is_documented_with_an_example():
    for tool in cg_mcp.TOOLS:
        for pname, spec in tool["inputSchema"]["properties"].items():
            desc = spec.get("description", "")
            assert len(desc) >= 30, (tool["name"], pname)
            assert "e.g." in desc or "for example" in desc or "such as" in desc, (tool["name"], pname)


def test_descriptions_say_what_comes_back_and_route_to_siblings():
    for tool in cg_mcp.TOOLS:
        desc = tool["description"]
        assert len(desc) >= 180, tool["name"]
        assert "Returns" in desc, tool["name"]
        assert "Use " in desc or "Call " in desc, tool["name"]
    by_name = {t["name"]: t["description"] for t in cg_mcp.TOOLS}
    assert "read_span" in by_name["search_spans"] and "search_spans" in by_name["read_span"]
    assert "guardian_status" in by_name["search_spans"]


def test_readme_mcp_section_is_complete():
    section = README[README.index("## MCP server"):]
    section = section[:section.index("\n## ", 1)]
    assert "uvx context-guardian mcp" in section
    for name in NAMES:
        assert "`%s`" % name in section, name
    blocks = re.findall(r"```json\n(.*?)```", section, re.S)
    configs = [json.loads(b)["mcpServers"]["context-guardian"] for b in blocks]
    assert any(c["command"] == "uvx" and c["args"] == ["context-guardian", "mcp"] for c in configs), configs
    assert "`server.json`" in section
    assert "mcp-name: io.github.LuminariSoftwares/context-guardian" in README.splitlines()


def test_latest_changelog_entry_says_server_json_exists():
    entries = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ")
    latest = entries[1]
    assert "`server.json`" in latest and "No `server.json`" not in latest
