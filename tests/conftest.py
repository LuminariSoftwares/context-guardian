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
import pytest


@pytest.mark.asyncio
async def test_the_async_suite_actually_runs():
    """If this SKIPS rather than PASSES, no async test in this repo ran."""
    assert True
