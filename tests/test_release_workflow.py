"""The release workflow publishes by trusted publishing (OIDC) and must never fall back to a stored token.

These read .github/workflows/release.yml as text (no YAML dependency) and fail if a publishing job loses
`id-token: write`, or if the file gains a `password:` input, a TOKEN, or a reference to a repository secret.
They also cover scripts/release_versions.py, which decides whether a tag may be released.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SCRIPT = ROOT / "scripts" / "release_versions.py"

sys.path.insert(0, str(SCRIPT.parent))
import release_versions  # noqa: E402


def job_blocks(text):
    """{job name: its lines}, for the two-space-indented keys under `jobs:`."""
    body = text.split("\njobs:\n", 1)[1]
    blocks, name = {}, None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1)
            blocks[name] = []
        elif name:
            blocks[name].append(line)
    return blocks


def credential_problems(text):
    """Lines that would hand a long-lived credential to a publish step."""
    problems = []
    for n, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if re.search(r"password\s*:", code, re.I):
            problems.append(f"{n}: password input")
        if "TOKEN" in code:
            problems.append(f"{n}: TOKEN")
        if "secrets." in code:
            problems.append(f"{n}: repository secret")
    return problems


def has_id_token_write(lines):
    return any(re.match(r"^\s+id-token:\s*write\s*$", ln) for ln in lines)


def test_publish_jobs_use_oidc():
    jobs = job_blocks(WORKFLOW.read_text(encoding="utf-8"))
    for job in ("pypi", "npm"):
        assert job in jobs, f"release.yml has no `{job}` job"
        assert has_id_token_write(jobs[job]), f"`{job}` lost `id-token: write`"
    assert any("environment:" in ln for ln in jobs["pypi"]), "PyPI trusted publisher is bound to environment pypi"
    assert any("name: pypi" in ln for ln in jobs["pypi"])


def test_no_stored_credential():
    assert credential_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_actions_pinned_by_sha():
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        m = re.search(r"uses:\s*(\S+)", line)
        if m:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", m.group(1)), line
            assert re.search(r"#\s*v\d", line), f"no version comment: {line}"


def test_guards_catch_regressions():
    # The checks above are only worth having if they go red on the mutation they exist for.
    text = WORKFLOW.read_text(encoding="utf-8")
    lost = text.replace("      id-token: write\n", "", 1)
    assert lost != text
    jobs = job_blocks(lost)
    assert not all(has_id_token_write(jobs[j]) for j in ("pypi", "npm"))
    assert credential_problems(text + "        with:\n          password: x\n")
    assert credential_problems(text + "        env:\n          NODE_AUTH_TOKEN: x\n")
    assert credential_problems(text + "          x: ${{ secrets.X }}\n")


@pytest.mark.parametrize("npm, pep", [
    ("0.12.0", "0.12.0"),
    ("0.12.0-beta.1", "0.12.0b1"),
    ("1.0.0-alpha.2", "1.0.0a2"),
    ("1.0.0-rc.3", "1.0.0rc3"),
    ("1.0", None),
    ("1.0.0-dev.1", None),
])
def test_pep440_mapping(npm, pep):
    assert release_versions.pep440(npm) == pep


def test_check_tag_against_both_files():
    assert release_versions.check("v0.12.0", "0.12.0", "0.12.0") == []
    assert release_versions.check("v0.12.0-beta.1", "0.12.0b1", "0.12.0-beta.1") == []
    assert release_versions.check("v0.12.0", "0.12.0", "0.1.0-beta.1")  # npm side differs
    assert release_versions.check("v0.12.0", "0.11.0", "0.12.0")  # PyPI side differs
    assert release_versions.check("0.12.0", "0.12.0", "0.12.0")  # no leading v
    assert release_versions.check("v0.12.0-beta.1", "0.12.0", "0.12.0-beta.1")  # PyPI not marked pre


def _run(tmp_path, py, npm, *args):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{py}"\n', encoding="utf-8")
    (tmp_path / "package.json").write_text(f'{{"name": "x", "version": "{npm}"}}', encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), *args],
                          capture_output=True, text=True)


def test_cli_exit_codes(tmp_path):
    ok = _run(tmp_path, "0.12.0b1", "0.12.0-beta.1", "--tag", "v0.12.0-beta.1")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "version=0.12.0-beta.1" in ok.stdout and "prerelease=true" in ok.stdout
    bad = _run(tmp_path, "0.11.0", "0.1.0-beta.1", "--tag", "v0.11.0")
    assert bad.returncode == 1 and "::error::" in bad.stdout
    dry = _run(tmp_path, "0.11.0", "0.1.0-beta.1", "--dry-run")
    assert dry.returncode == 0 and "::warning::" in dry.stdout and "would FAIL" in dry.stdout
    assert _run(tmp_path, "0.12.0", "0.12.0").returncode == 2  # neither --tag nor --dry-run
