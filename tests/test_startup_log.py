"""The startup line must say which keep-recent mode is active.

Budget mode (the default: newest messages within a fraction of the usable
window) and explicit mode (GUARDIAN_KEEP_RECENT_MESSAGES=N, an exact count)
behave very differently, and the only way to tell from a running proxy was
/guardian/stats. The startup log now says keep_recent=budget 20% or
keep_recent=N messages (explicit).
"""
import importlib
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _startup_line(monkeypatch, caplog, **env):
    for k in ("GUARDIAN_KEEP_RECENT_MESSAGES", "GUARDIAN_KEEP_RECENT_FRACTION"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cg = (importlib.reload(sys.modules["context_guardian"])
          if "context_guardian" in sys.modules
          else importlib.import_module("context_guardian"))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(cg, "start_version_check", lambda: None)
    with caplog.at_level(logging.INFO, logger="context_guardian"):
        cg.main()
    lines = [r.getMessage() for r in caplog.records if "Starting Context Guardian" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def test_default_is_budget_mode(monkeypatch, caplog):
    assert "keep_recent=budget 20%" in _startup_line(monkeypatch, caplog)


def test_budget_fraction_is_reported(monkeypatch, caplog):
    line = _startup_line(monkeypatch, caplog, GUARDIAN_KEEP_RECENT_FRACTION="0.3")
    assert "keep_recent=budget 30%" in line


@pytest.mark.parametrize("n", ["6", "0"])
def test_explicit_count_is_reported(monkeypatch, caplog, n):
    line = _startup_line(monkeypatch, caplog, GUARDIAN_KEEP_RECENT_MESSAGES=n)
    assert "keep_recent=%s messages (explicit)" % n in line
