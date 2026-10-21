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
        assert "returns" in desc.lower(), tool["name"]
        assert "Use " in desc or "Call " in desc, tool["name"]
    by_name = {t["name"]: t["description"] for t in cg_mcp.TOOLS}
    span_tools = ("list_spans", "search_spans", "read_span")
    for name in span_tools:   # each span tool names the other two, so an agent can pick between them
        for sibling in span_tools:
            assert sibling == name or sibling in by_name[name], (name, sibling)
    for name in span_tools:   # and the status tool routes to all three
        assert name in by_name["get_proxy_status"], name


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
        assert re.fullmatch(r"(get|list|search|read)(_[a-z]+)+", name), name   # verb_noun, one verb set
        assert tool.get("title"), name
        desc = tool.get("description", "")
        for part in ("use ", "returns"):   # when to use it, what comes back
            assert part in desc.lower(), (name, part)
        assert "no network" in desc or "GET " in desc, name   # what it touches
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema.get("additionalProperties") is False, name
        assert set(schema.get("required", [])) <= set(schema["properties"]), name


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
                assert "default" in spec or "omit" in spec["description"].lower(), where
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


def test_deprecated_names_are_callable_but_not_listed():
    names = {t["name"] for t in cg_mcp.TOOLS}
    for old, new in cg_mcp.DEPRECATED_ALIASES.items():
        assert old not in names and new in names, old
    assert set(cg_mcp.HANDLERS) == names


def test_output_schema_documents_every_field_it_names():
    for tool in cg_mcp.TOOLS:
        schema = tool.get("outputSchema")
        if schema:
            assert schema["type"] == "object", tool["name"]
            for field, spec in schema["properties"].items():
                assert spec.get("description", "").strip(), (tool["name"], field)
