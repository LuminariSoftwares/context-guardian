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


# --- Every tool and every parameter is fully documented ------------------------------------------------------
# Fails the moment a tool or a parameter lands without the parts an MCP client's model needs to pick and call it.

def test_every_tool_has_a_complete_definition():
    for tool in cg_mcp.TOOLS:
        name = tool["name"]
        assert re.fullmatch(r"[a-z]+(_[a-z]+)+", name), name   # snake_case, at least two words
        assert tool.get("title"), name
        desc = tool.get("description", "")
        for part in ("Use ", "Behaviour: read-only", "Returns"):
            assert part in desc, (name, part)
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema.get("additionalProperties") is False, name
        assert set(schema.get("required", [])) <= set(schema["properties"]), name
        if not schema["properties"]:
            assert "Takes no arguments" in desc, name


def test_every_parameter_has_type_description_example_and_default_or_required():
    for tool in cg_mcp.TOOLS:
        required = set(tool["inputSchema"].get("required", []))
        for pname, spec in tool["inputSchema"]["properties"].items():
            where = (tool["name"], pname)
            assert spec.get("type") in ("string", "integer", "number", "boolean", "array", "object"), where
            assert spec.get("description", "").strip(), where
            assert spec.get("examples"), where
            if pname in required:
                assert "Required" in spec["description"], where
            else:
                assert "default" in spec or "Omit" in spec["description"], where
            if spec["type"] == "integer":
                assert "minimum" in spec, where
            for example in spec["examples"]:
                if spec["type"] == "integer":
                    assert isinstance(example, int) and spec["minimum"] <= example <= spec.get("maximum", example), where
                elif "pattern" in spec:
                    assert re.fullmatch(spec["pattern"], example), where


def test_advertised_run_pattern_is_the_one_the_server_enforces():
    for tool in cg_mcp.TOOLS:
        run = tool["inputSchema"]["properties"].get("run")
        if run:
            assert run["pattern"] == cg_mcp.RUN_ID_RE.pattern, tool["name"]


def test_readme_tool_reference_matches_the_schemas():
    sys.path.insert(0, str(ROOT / "tools"))
    import gen_mcp_reference
    assert gen_mcp_reference.readme_block(README) == gen_mcp_reference.render(), (
        "run: python tools/gen_mcp_reference.py --write")
