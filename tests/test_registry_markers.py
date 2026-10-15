"""Official MCP Registry ownership markers: package.json `mcpName` (npm) and a
`mcp-name:` line in README.md (the README PyPI publishes). They must agree."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "io.github.LuminariSoftwares/context-guardian"


def test_both_markers_exist_and_agree():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = re.findall(r"^mcp-name: (\S+)$", readme, re.M)
    assert pkg.get("mcpName") == NAME
    assert lines == [NAME]
    assert pkg["mcpName"] == lines[0]


def test_the_readme_with_the_marker_is_what_pypi_gets():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^readme = "README\.md"$', text, re.M)
