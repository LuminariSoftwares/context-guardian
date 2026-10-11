"""README promises that a reader can act on without opening the source."""
import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
README = (REPO_DIR / "README.md").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    m = re.search(r"^## %s\s*$(.*?)(?=^## )" % re.escape(heading), README, re.M | re.S)
    assert m, "README has no '## %s' section" % heading
    return m.group(1)


def test_quick_start_names_the_doctor():
    assert "context-guardian-doctor" in _section("Quick start")


def test_client_config_table_covers_the_four_clients():
    body = _section("Point your client at the proxy")
    for client in ("Claude Code", "Cline", "Continue", "OpenCode"):
        row = [ln for ln in body.splitlines() if ln.startswith("| **%s**" % client)]
        assert row, "no table row for %s" % client
    assert body.count("http://localhost:8786") >= 4


def test_handoff_files_are_explained():
    assert "handoff_latest.json" in README and "handoff_<session>" in README


def test_recall_command_is_documented():
    assert "context-guardian-recall" in README


def test_span_test_docstring_matches_the_code():
    """write_span claims NNNN.json with an exclusive create; there is no .part
    file and no os.replace any more."""
    text = (REPO_DIR / "tests" / "test_spans.py").read_text(encoding="utf-8")
    assert ".part then os.replace" not in text
