"""Where an installed package keeps its data.

Every default was derived from REPO_DIR = Path(__file__).parent, which is the
checkout when you cloned and site-packages when you pip-installed. So a pip
install wrote spans, the compaction log, the version cache and the .env from
context-guardian-configure INTO site-packages: lost on upgrade, invisible to
the user, and not the same directory `uvx context-guardian mcp` reads, because
uvx builds its own environment. An installed package now defaults to a
per-user directory.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_paths  # noqa: E402


def test_a_checkout_keeps_using_the_checkout(tmp_path):
    """A git checkout is where the user put it, and its logs/ is where the
    README has always said they are. Nothing moves."""
    (tmp_path / ".git").mkdir()
    assert cg_paths.data_dir(package_dir=tmp_path, env={}) == tmp_path


@pytest.mark.parametrize("marker", ["pyproject.toml", "package.json"])
def test_a_source_tree_without_git_still_counts_as_a_checkout(tmp_path, marker):
    (tmp_path / marker).write_text("x", encoding="utf-8")
    assert cg_paths.data_dir(package_dir=tmp_path, env={}) == tmp_path


def test_a_fresh_install_gets_a_per_user_directory(tmp_path, monkeypatch):
    pkg = tmp_path / "site-packages"
    pkg.mkdir()
    home = tmp_path / "home"
    got = cg_paths.data_dir(package_dir=pkg, env={"HOME": str(home)},
                            os_name="posix")
    assert got == home / ".context-guardian"


def test_windows_uses_localappdata(tmp_path):
    pkg = tmp_path / "site-packages"
    pkg.mkdir()
    local = tmp_path / "AppData" / "Local"
    got = cg_paths.data_dir(package_dir=pkg, env={"LOCALAPPDATA": str(local)},
                            os_name="nt")
    assert got == local / "context-guardian"


def test_an_existing_old_archive_keeps_being_used(tmp_path, capsys):
    """Migration: a user who already has spans in site-packages must not
    silently start reading an empty new directory."""
    pkg = tmp_path / "site-packages"
    (pkg / "logs" / "guardian_spans" / "20261020-120000").mkdir(parents=True)
    (pkg / "logs" / "guardian_spans" / "20261020-120000" / "0001.json").write_text(
        "{}", encoding="utf-8")
    home = tmp_path / "home"
    notes = []
    got = cg_paths.data_dir(package_dir=pkg, env={"HOME": str(home)},
                            os_name="posix", notify=notes.append)
    assert got == pkg
    assert len(notes) == 1
    note = notes[0]
    assert str(home / ".context-guardian") in note
    assert "GUARDIAN_SPAN_DIR" in note


def test_the_new_directory_wins_once_it_has_spans(tmp_path):
    pkg = tmp_path / "site-packages"
    (pkg / "logs" / "guardian_spans" / "20261020-120000").mkdir(parents=True)
    (pkg / "logs" / "guardian_spans" / "20261020-120000" / "0001.json").write_text(
        "{}", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".context-guardian" / "logs" / "guardian_spans" / "20261021-120000").mkdir(parents=True)
    (home / ".context-guardian" / "logs" / "guardian_spans" / "20261021-120000" / "0001.json"
     ).write_text("{}", encoding="utf-8")
    assert cg_paths.data_dir(package_dir=pkg, env={"HOME": str(home)},
                             os_name="posix") == home / ".context-guardian"


def test_an_explicit_data_dir_wins(tmp_path):
    pkg = tmp_path / "site-packages"
    pkg.mkdir()
    chosen = tmp_path / "elsewhere"
    got = cg_paths.data_dir(package_dir=pkg,
                            env={"GUARDIAN_DATA_DIR": str(chosen), "HOME": str(tmp_path)},
                            os_name="posix")
    assert got == chosen


def test_notice_is_printed_once_per_process(tmp_path):
    pkg = tmp_path / "site-packages"
    (pkg / "logs" / "guardian_spans" / "r").mkdir(parents=True)
    (pkg / "logs" / "guardian_spans" / "r" / "0001.json").write_text("{}", encoding="utf-8")
    notes = []
    for _ in range(3):
        cg_paths.data_dir(package_dir=pkg, env={"HOME": str(tmp_path / "home")},
                          os_name="posix", notify=notes.append)
    assert len(notes) == 3, "data_dir itself is pure; the caller decides when to say it"


# --- what the two front doors actually resolve to -------------------------

def test_the_proxy_and_the_mcp_server_resolve_the_same_span_dir(monkeypatch, tmp_path):
    import importlib
    monkeypatch.delenv("GUARDIAN_SPAN_DIR", raising=False)
    monkeypatch.setenv("GUARDIAN_DATA_DIR", str(tmp_path))
    import cg_recall_cli
    proxy = importlib.reload(importlib.import_module("context_guardian"))
    importlib.reload(cg_recall_cli)
    assert proxy.SPAN_DIR == tmp_path / "logs" / "guardian_spans"
    assert cg_recall_cli.span_dir() == proxy.SPAN_DIR


def test_the_env_var_still_wins_over_everything(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("GUARDIAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "chosen"))
    proxy = importlib.reload(importlib.import_module("context_guardian"))
    assert proxy.SPAN_DIR == tmp_path / "chosen"
