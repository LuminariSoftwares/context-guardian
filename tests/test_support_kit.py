"""The support kit stays in place: issue forms parse, blank issues stay off.

Docs-only guard. The issue forms are what a stranger sees first, so a YAML typo
or a quiet `blank_issues_enabled: true` should fail CI, not ship.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = ["bug_report.yml", "feature_request.yml"]


def load(name):
    return yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FORMS)
def test_issue_form_schema(name):
    form = load(name)
    for key in ("name", "description", "body"):
        assert form.get(key), "%s: missing %s" % (name, key)
    ids = []
    for item in form["body"]:
        assert item.get("type") in {"markdown", "input", "textarea", "dropdown", "checkboxes"}
        if item["type"] != "markdown":
            assert item.get("id"), "%s: a %s field has no id" % (name, item["type"])
            assert item["attributes"].get("label"), "%s: %s has no label" % (name, item["id"])
            ids.append(item["id"])
    assert len(ids) == len(set(ids)), "%s: duplicate field ids" % name


def test_bug_form_asks_for_the_reproduction():
    form = load("bug_report.yml")
    assert "needs-info" in form["labels"]
    fields = {i["id"]: i for i in form["body"] if "id" in i}
    for needed in ("version", "os", "python_version", "command", "output", "expected"):
        assert fields[needed].get("validations", {}).get("required") is True, needed
    boxes = fields["quick_start"]["attributes"]["options"]
    assert any(
        b["label"] == "I ran the quick start as written" and b.get("required") is True
        for b in boxes
    )


def test_feature_form_promises_no_timeline():
    text = (TEMPLATES / "feature_request.yml").read_text(encoding="utf-8").lower()
    assert "not a promise of a timeline" in text


def test_blank_issues_stay_disabled():
    config = load("config.yml")
    assert config["blank_issues_enabled"] is False
    links = {c["name"]: c["url"] for c in config["contact_links"]}
    assert links["Security issue"] == (
        "https://github.com/LuminariSoftwares/context-guardian/security/advisories/new"
    )
    assert "Question" in links


def test_support_md_exists_and_is_linked():
    support = ROOT / "SUPPORT.md"
    assert support.is_file(), "SUPPORT.md is gone"
    text = support.read_text(encoding="utf-8")
    assert "within a day" in text
    assert "Please do not open a public issue" in text
    assert "SUPPORT.md" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
