"""Canary: prove the async half of the suite is actually running.

If pytest-asyncio is missing or misconfigured, `async def` tests are SKIPPED
and pytest exits 0. Every guard against the 0.2.0 empty-summary data-loss bug
is an async test, so that silent skip is the difference between a suite that
protects users and a suite that only says it does.

pyproject.toml turns the skip into an error. This file is the belt to that
braces: a single async test that must PASS -- not skip, not warn -- so a
misconfiguration is visible in the summary line rather than in a warning
nobody reads.
"""
import atexit
import os
import shutil
import tempfile

import pytest

# Keep every test's writes out of the repository.
#
# context_guardian.py defaults its span archive, compaction log and version
# cache to <repo>/logs/. A test that imported it without overriding them wrote
# real spans next to the source on every run. Set here at import time, so a
# test module imported during collection already sees it, and again per test
# below, so one test cannot leak a directory into the next.
_SESSION_DIR = tempfile.mkdtemp(prefix="cg-pytest-")
atexit.register(shutil.rmtree, _SESSION_DIR, ignore_errors=True)
os.environ["GUARDIAN_SPAN_DIR"] = os.path.join(_SESSION_DIR, "guardian_spans")
os.environ["GUARDIAN_LOG_PATH"] = os.path.join(_SESSION_DIR, "context_guardian_log.json")
os.environ["GUARDIAN_VERSION_CACHE"] = os.path.join(_SESSION_DIR, "version_cache.json")


@pytest.fixture(autouse=True)
def _guardian_writes_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "guardian_spans"))
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "context_guardian_log.json"))
    monkeypatch.setenv("GUARDIAN_VERSION_CACHE", str(tmp_path / "version_cache.json"))


@pytest.mark.asyncio
async def test_the_async_suite_actually_runs():
    """If this SKIPS rather than PASSES, no async test in this repo ran."""
    assert True
