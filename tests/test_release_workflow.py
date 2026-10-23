"""The release workflow publishes by trusted publishing (OIDC) and must never fall back to a stored token.

These read .github/workflows/release.yml as text (no YAML dependency) and fail if a publishing job loses
`id-token: write`, or if the file gains a `password:` input, a TOKEN, or a reference to a repository secret.
They also cover scripts/release_versions.py: the tag names the PyPI version, npm publishes only a version that
is new on the registry, and a release with neither is an error. Registry lookups are mocked.
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


def _lookups(on_pypi=(), on_npm=()):
    """Mocked registries: the versions listed are already published, everything else is new."""
    return (lambda name, v: v in on_pypi), (lambda name, v: v in on_npm)


def test_tag_follows_pypi_and_ignores_npm_version():
    pypi, npm = _lookups(on_npm={"0.1.0-beta.1"})
    problems, _, out = release_versions.plan("v0.11.0", "0.11.0", "dsh-context-guardian", "0.1.0-beta.1",
                                             pypi_lookup=pypi, npm_lookup=npm)
    assert problems == []
    assert out["publish_pypi"] == "true"


def test_tag_that_is_not_the_pypi_version_fails():
    pypi, npm = _lookups()
    problems, _, _ = release_versions.plan("v0.12.0", "0.11.0", "dsh-context-guardian", "0.1.0-beta.1",
                                           pypi_lookup=pypi, npm_lookup=npm)
    assert any("must be v0.11.0" in p for p in problems), problems


def test_npm_version_already_on_registry_skips_npm():
    pypi, npm = _lookups(on_npm={"0.1.0-beta.1"})
    _, notices, out = release_versions.plan("v0.12.0", "0.12.0", "dsh-context-guardian", "0.1.0-beta.1",
                                            pypi_lookup=pypi, npm_lookup=npm)
    assert out["publish_npm"] == "false"
    assert any("already on npm" in n for n in notices), notices


def test_new_npm_version_publishes_and_prerelease_is_flagged():
    pypi, npm = _lookups(on_npm={"0.1.0-beta.1"})
    _, _, out = release_versions.plan("v0.12.0", "0.12.0", "dsh-context-guardian", "0.1.0-beta.2",
                                      pypi_lookup=pypi, npm_lookup=npm)
    assert out["publish_npm"] == "true" and out["npm_prerelease"] == "true"
    _, _, out = release_versions.plan("v0.12.0", "0.12.0", "dsh-context-guardian", "0.2.0",
                                      pypi_lookup=pypi, npm_lookup=npm)
    assert out["npm_prerelease"] == "false"


def test_pypi_only_patch_passes_and_skips_npm():
    # pyproject.toml bumped 0.11.0 -> 0.11.1, package.json unchanged (its version is already on npm).
    pypi, npm = _lookups(on_pypi={"0.11.0"}, on_npm={"0.1.0-beta.1"})
    problems, _, out = release_versions.plan("v0.11.1", "0.11.1", "dsh-context-guardian", "0.1.0-beta.1",
                                             pypi_lookup=pypi, npm_lookup=npm)
    assert problems == []
    assert out["publish_pypi"] == "true" and out["publish_npm"] == "false"


def test_neither_version_new_is_nothing_to_publish():
    pypi, npm = _lookups(on_pypi={"0.11.0"}, on_npm={"0.1.0-beta.1"})
    problems, _, out = release_versions.plan("v0.11.0", "0.11.0", "dsh-context-guardian", "0.1.0-beta.1",
                                             pypi_lookup=pypi, npm_lookup=npm)
    assert any("nothing to publish" in p for p in problems), problems
    assert out["publish_pypi"] == "false" and out["publish_npm"] == "false"


def test_cli_bad_arguments_exit_2(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.11.0"\n', encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "x", "version": "0.1.0-beta.1"}', encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr  # neither --tag nor --dry-run; no registry is asked


def test_npm_job_gates_publish_on_publish_npm():
    jobs = job_blocks(WORKFLOW.read_text(encoding="utf-8"))
    npm = "\n".join(jobs["npm"])
    assert "needs.verify.outputs.publish_npm == 'true'" in npm
    assert "--tag beta" in npm and "dist-tag add" in npm
    assert "needs.verify.outputs.publish_pypi == 'true'" in "\n".join(jobs["pypi"])
