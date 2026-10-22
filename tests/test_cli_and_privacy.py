"""The console entry point's flags, the archive's file modes, and one
update-check opt-out shared with the JavaScript bundle.

  --help / --version both STARTED UVICORN. `context-guardian --version` bound
  port 8786 and sat there; the only way to read the version was /guardian/stats
  from a second terminal.

  Spans are the conversation compaction removed, in clear text. The directory
  was created with the process umask (0755 on most systems) and the files 0644,
  so every local account could read them.

  The Python proxy honoured only GUARDIAN_VERSION_CHECK while the JavaScript
  bundle honoured GUARDIAN_NO_UPDATE_CHECK, NO_UPDATE_NOTIFIER and CI. One
  family, one opt-out.
"""
import importlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _child_env import child_env

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _cli(*args, timeout=30, env=None):
    # child_env, not a hand-built dict: this child imports fastapi, so without
    # SYSTEMROOT it dies in Winsock before main() runs. See tests/_child_env.py.
    full = child_env(pythonpath=ROOT, GUARDIAN_VERSION_CHECK="0", **(env or {}))
    return subprocess.run([sys.executable, str(ROOT / "context_guardian.py"), *args],
                          capture_output=True, text=True, timeout=timeout, env=full)


# --- 5. --help and --version do not start the server ----------------------

@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_prints_and_exits(flag):
    r = _cli(flag)
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"context-guardian \d+\.\d+\.\d+\S*", r.stdout.strip()), r.stdout
    assert "Starting Context Guardian" not in r.stderr


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_entry_points_and_exits(flag):
    r = _cli(flag)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for part in ("context-guardian mcp", "context-guardian-doctor",
                 "context-guardian-configure", "context-guardian-recall",
                 "context-guardian-mcp"):
        assert part in out, part
    assert "GUARDIAN_NUM_CTX" in out
    assert "Starting Context Guardian" not in r.stderr


def test_unknown_arguments_are_still_ignored(monkeypatch):
    """Documented behaviour: anything the entry point does not know about is
    ignored rather than rejected, so a wrapper can pass its own flags."""
    import context_guardian as cg
    started = []
    monkeypatch.setattr(cg, "serve", lambda: started.append(True))
    monkeypatch.setattr(sys, "argv", ["context-guardian", "--not-a-flag", "xyz"])
    assert cg.main() is None or True
    assert started == [True]


def test_the_mcp_subcommand_still_wins(monkeypatch):
    """`uvx context-guardian mcp` is what the MCP Registry runs, so the
    subcommand is checked before any flag handling."""
    import cg_mcp
    import context_guardian as cg
    calls = []
    monkeypatch.setattr(cg, "serve", lambda: calls.append("serve"))
    monkeypatch.setattr(cg_mcp, "main", lambda: calls.append("mcp") or 0)
    monkeypatch.setattr(sys, "argv", ["context-guardian", "mcp", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cg.main()
    assert exit_info.value.code == 0
    assert calls == ["mcp"]


# --- 9. the archive is private -------------------------------------------

@POSIX_ONLY
def test_span_directories_and_files_are_private(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "logs" / "log.json"))
    cg = importlib.reload(importlib.import_module("context_guardian"))
    path = cg.write_span([{"role": "user", "content": "a secret from the transcript"}],
                         "a summary of it", 1)
    assert path is not None
    span = Path(path)
    assert stat.S_IMODE(span.stat().st_mode) == 0o600, oct(span.stat().st_mode)
    assert stat.S_IMODE(span.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(span.parent.parent.stat().st_mode) == 0o700

    cg.log_event({"event": "test"})
    log = Path(cg.LOG_PATH)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600, oct(log.stat().st_mode)
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700


@POSIX_ONLY
def test_a_pre_existing_loose_log_is_tightened(monkeypatch, tmp_path):
    log = tmp_path / "log.json"
    log.write_text("", encoding="utf-8")
    log.chmod(0o644)
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(log))
    cg = importlib.reload(importlib.import_module("context_guardian"))
    cg.log_event({"event": "test"})
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_private_helpers_never_raise(monkeypatch, tmp_path):
    """Permissions are housekeeping: a filesystem that cannot do them (a
    Windows share, an exotic mount) must not cost the request."""
    cg = importlib.reload(importlib.import_module("context_guardian"))
    cg._make_private(tmp_path / "does-not-exist")
    cg._make_private_dir(tmp_path / "also-not-there")


# --- 10. one opt-out for the family --------------------------------------

@pytest.mark.parametrize("name", ["GUARDIAN_NO_UPDATE_CHECK", "NO_UPDATE_NOTIFIER", "CI"])
def test_the_javascript_opt_outs_work_on_the_python_side(monkeypatch, tmp_path, name):
    monkeypatch.delenv("GUARDIAN_VERSION_CHECK", raising=False)
    for other in ("GUARDIAN_NO_UPDATE_CHECK", "NO_UPDATE_NOTIFIER", "CI"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "1")
    cg = importlib.reload(importlib.import_module("context_guardian"))
    assert cg.VERSION_CHECK is False, name
    started = []
    monkeypatch.setattr(cg.threading, "Thread", lambda **kw: started.append(kw))
    cg.start_version_check()
    assert started == []


@pytest.mark.parametrize("value", ["0", "false"])
def test_an_opt_out_set_to_zero_does_not_opt_out(monkeypatch, tmp_path, value):
    """Same rule as update_check.js: the variable must be non-empty and not
    0/false, so `CI=0` in a shell profile does not silence a real user."""
    for other in ("GUARDIAN_VERSION_CHECK", "GUARDIAN_NO_UPDATE_CHECK",
                  "NO_UPDATE_NOTIFIER", "CI"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv("CI", value)
    cg = importlib.reload(importlib.import_module("context_guardian"))
    assert cg.VERSION_CHECK is True, value


def test_guardian_version_check_zero_still_wins(monkeypatch):
    for other in ("GUARDIAN_NO_UPDATE_CHECK", "NO_UPDATE_NOTIFIER", "CI"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv("GUARDIAN_VERSION_CHECK", "0")
    cg = importlib.reload(importlib.import_module("context_guardian"))
    assert cg.VERSION_CHECK is False


# --- 10b. a non-loopback bind says so at startup -------------------------

def test_a_non_loopback_bind_warns_at_startup(monkeypatch, caplog):
    monkeypatch.setenv("GUARDIAN_HOST", "0.0.0.0")
    cg = importlib.reload(importlib.import_module("context_guardian"))
    monkeypatch.setattr(cg, "serve", lambda: None)
    monkeypatch.setattr(sys, "argv", ["context-guardian"])
    with caplog.at_level("WARNING"):
        cg.main()
    text = caplog.text
    assert "0.0.0.0" in text and "GUARDIAN_HOST" in text
    assert "no authentication" in text.lower()


def test_a_loopback_bind_says_nothing(monkeypatch, caplog):
    monkeypatch.setenv("GUARDIAN_HOST", "127.0.0.1")
    cg = importlib.reload(importlib.import_module("context_guardian"))
    monkeypatch.setattr(cg, "serve", lambda: None)
    monkeypatch.setattr(sys, "argv", ["context-guardian"])
    with caplog.at_level("WARNING"):
        cg.main()
    assert "no authentication" not in caplog.text.lower()
