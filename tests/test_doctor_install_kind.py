"""The doctor knows whether it is checking a pip install or an npm bundle.

`context-guardian-doctor` on a pip install exited 1 with

    FAIL package.json not found
    FAIL modules/cg_bridge.py is missing

Both are DSH-bundle checks. A pip user has no package.json and no bridge and
never will: the doctor was telling them their install was broken because it
was not a different install. Exit 1 is what a CI job and a support reply read,
so the first thing a pip user was told was wrong.

An npm layout with files missing is still an error -- that is a real broken
install, and the existing selftest covers it.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_doctor  # noqa: E402


def _pip_layout(root: Path) -> Path:
    """What `pip install context-guardian` leaves: the wheel's modules and
    nothing else. No package.json, no modules/, no index.js."""
    for name in ("context_guardian.py", "cg_doctor.py", "cg_configure.py",
                 "cg_recall_cli.py", "cg_mcp.py", "cg_paths.py"):
        (root / name).write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    return root


def _findings(root, **kw):
    kw.setdefault("env", {"GUARDIAN_PYTHON": "py"})
    kw.setdefault("http_get", lambda url, timeout: (200, '{"num_ctx": 65536}'))
    kw.setdefault("run", lambda argv, stdin, timeout: (0, '{"id":1,"ok":true,"result":{"python":"3.11.9"}}\n'))
    kw.setdefault("which", lambda name: None)
    return cg_doctor.doctor(root=Path(root), **kw)


def test_a_pip_layout_has_no_errors(tmp_path):
    findings = _findings(_pip_layout(tmp_path))
    errors = [f for f in findings if f["level"] == "error"]
    assert errors == [], errors


def test_a_pip_layout_says_the_bundle_checks_were_skipped(tmp_path):
    findings = _findings(_pip_layout(tmp_path))
    skipped = [f for f in findings if "pip install" in f["what"]]
    assert len(skipped) == 1, [f["what"] for f in findings]
    assert skipped[0]["level"] == "ok"
    assert "skipped" in skipped[0]["what"]
    text, code = cg_doctor.render(findings)
    assert code == 0
    assert "OK   pip install" in text
    assert "package.json" not in text
    assert "cg_bridge" not in text


def test_a_pip_layout_still_checks_the_env_and_the_proxy(tmp_path):
    """Skipping the bundle checks must not skip the checks a pip user needs."""
    bad = {"GUARDIAN_PYTHON": "py", "GUARDIAN_NUM_CTX": "32k"}
    errors = [f for f in _findings(_pip_layout(tmp_path), env=bad) if f["level"] == "error"]
    assert len(errors) == 1 and "GUARDIAN_NUM_CTX=32k" in errors[0]["what"]

    def down(url, timeout):
        raise OSError("refused")

    warns = [f["what"] for f in _findings(_pip_layout(tmp_path), http_get=down)
             if f["level"] == "warn"]
    assert any("proxy not reachable" in w for w in warns), warns


def test_a_broken_npm_layout_is_still_an_error(tmp_path):
    """The bridge is there, so this IS an npm install -- and its package.json
    is missing, which is a real fault."""
    root = _pip_layout(tmp_path)
    (root / "modules").mkdir()
    (root / "modules" / "cg_bridge.py").write_text("# bridge\n", encoding="utf-8")
    errors = [f["what"] for f in _findings(root) if f["level"] == "error"]
    assert any("package.json not found" in e for e in errors), errors


def test_a_complete_npm_layout_is_unchanged(tmp_path):
    root = _pip_layout(tmp_path)
    (root / "package.json").write_text('{"name": "dsh-context-guardian", "version": "1.2.3"}',
                                       encoding="utf-8")
    (root / "modules").mkdir()
    (root / "modules" / "cg_bridge.py").write_text("# bridge\n", encoding="utf-8")
    findings = _findings(root)
    assert [f["level"] for f in findings] == ["ok"] * len(findings)
    assert any("package: dsh-context-guardian 1.2.3" in f["what"] for f in findings)
    assert not any("pip install" in f["what"] for f in findings)


def test_the_cli_exits_zero_on_a_pip_layout(tmp_path):
    """End to end: the real console script against a pip-style tree."""
    root = _pip_layout(tmp_path)
    (root / "cg_doctor.py").write_text(
        (ROOT / "cg_doctor.py").read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run([sys.executable, str(root / "cg_doctor.py")],
                       capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "GUARDIAN_NUM_CTX": "32768"})
    assert "FAIL" not in r.stdout, r.stdout
    assert "pip install" in r.stdout
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_selftest_still_passes():
    r = subprocess.run([sys.executable, str(ROOT / "cg_doctor.py"), "--selftest"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 failed" in r.stdout, r.stdout
