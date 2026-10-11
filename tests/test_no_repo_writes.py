"""The test suite must not write into the repository's own logs/ directory.

context_guardian.py defaults GUARDIAN_SPAN_DIR (and the log and version-cache
paths) to <repo>/logs/. Any test that imported the module without overriding
them archived its spans next to the source, so every local `pytest` run left a
logs/guardian_spans/<run_id>/ behind. conftest.py now points all of them at a
temporary directory for every test.
"""
import importlib
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))


def _inside_repo(p: Path) -> bool:
    p = Path(p).resolve()
    return p == REPO_DIR or REPO_DIR in p.parents


def test_span_dir_env_points_outside_the_repo():
    value = os.environ.get("GUARDIAN_SPAN_DIR", "")
    assert value, "conftest.py did not set GUARDIAN_SPAN_DIR"
    assert not _inside_repo(Path(value)), value


def test_a_span_written_with_no_test_override_lands_outside_the_repo():
    """A test that does not set GUARDIAN_SPAN_DIR itself (most of
    test_guardian.py) must still not write into <repo>/logs/."""
    repo_spans = REPO_DIR / "logs" / "guardian_spans"
    before = sorted(repo_spans.rglob("*")) if repo_spans.exists() else []

    mod = (importlib.reload(sys.modules["context_guardian"])
           if "context_guardian" in sys.modules
           else importlib.import_module("context_guardian"))
    path = mod.write_span([{"role": "user", "content": "x"}], "s", 1)

    assert path is not None
    assert not _inside_repo(Path(path)), path
    assert not _inside_repo(mod.LOG_PATH), mod.LOG_PATH
    assert not _inside_repo(mod.VERSION_CACHE_PATH), mod.VERSION_CACHE_PATH
    after = sorted(repo_spans.rglob("*")) if repo_spans.exists() else []
    assert after == before, "a test wrote into the repo's own span directory"
