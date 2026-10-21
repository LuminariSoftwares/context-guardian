"""Files outside the code that hosts and registries read: glama.json, server.json and the Dockerfile.

Each one restates something pyproject.toml already says (version, modules); these tests fail when they drift."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
VERSION = re.search(r'^version = "([^"]+)"', PYPROJECT, re.M).group(1)
PY_MODULES = re.findall(r'"([a-z_]+)"', re.search(r"^py-modules = \[(.*?)\]", PYPROJECT, re.M).group(1))


def test_glama_json_is_valid_and_names_the_maintainer():
    data = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    assert data["$schema"] == "https://glama.ai/mcp/schemas/server.json"
    assert data["maintainers"] == ["LuminariSoftwares"]


def test_server_json_version_matches_the_package():
    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert data["version"] == VERSION
    assert [p["version"] for p in data["packages"]] == [VERSION]


def test_dockerfile_copies_every_wheel_module_and_runs_the_mcp_server():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.findall(r"\b([a-z_]+)\.py\b", " ".join(
        ln for ln in dockerfile.splitlines() if ln.startswith("COPY "))))
    assert set(PY_MODULES) <= copied, set(PY_MODULES) - copied
    assert 'ENTRYPOINT ["context-guardian-mcp"]' in dockerfile
    allowed = {ln[1:] for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if ln.startswith("!")}
    assert {m + ".py" for m in PY_MODULES} | {"pyproject.toml", "README.md", "LICENSE"} <= allowed
