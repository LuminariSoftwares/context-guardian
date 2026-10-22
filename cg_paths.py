#!/usr/bin/env python3
"""cg_paths.py -- where Context Guardian keeps its data.

Every default used to be derived from the directory holding the source file:

    REPO_DIR = Path(__file__).resolve().parent

which is the checkout when you cloned the repo, and **site-packages** when you
`pip install context-guardian`. So an installed proxy archived its spans, wrote
its compaction log and its version cache, and `context-guardian-configure`
wrote its `.env`, inside site-packages. Three things follow:

  * `pip install -U` replaces that directory. The archive the README calls
    "lossless on disk" went with it.
  * Nothing in the user's home directory ever mentioned Context Guardian, so
    the data was somewhere no one would think to look -- or to delete.
  * `uvx context-guardian mcp` (the command the MCP Registry runs) builds its
    OWN environment, so the MCP server read a different, empty archive from
    the one the proxy had been writing.

So: a source tree keeps using itself, exactly as before. An installed package
uses a per-user directory -- `~/.context-guardian`, or
`%LOCALAPPDATA%\\context-guardian` on Windows. `GUARDIAN_DATA_DIR` overrides
the choice, and the specific `GUARDIAN_SPAN_DIR` / `GUARDIAN_LOG_PATH` /
`GUARDIAN_VERSION_CACHE` variables still win over everything.

Migration is conservative: when the old site-packages location already holds
spans and the new one does not, the old one keeps being used and the caller is
handed one notice naming the new path. Nothing is moved or deleted by this
module; a user who upgrades does not silently lose sight of their archive.

Stdlib only, Python 3.10+. MIT licensed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Optional

PACKAGE_DIR = Path(__file__).resolve().parent

#: Files that only exist in a source tree, never in an installed wheel.
SOURCE_MARKERS = (".git", "pyproject.toml", "package.json")

#: Relative to the data directory. Kept here so the proxy, the recall CLI, the
#: MCP server and the configure wizard cannot drift apart.
SPANS_SUBDIR = Path("logs") / "guardian_spans"
LOG_SUBPATH = Path("logs") / "context_guardian_log.json"
VERSION_CACHE_SUBPATH = Path("logs") / ".version_check_cache.json"
ENV_FILENAME = ".env"

MIGRATION_NOTICE = (
    "Context Guardian is installed as a package but its span archive is still "
    "inside the install directory ({old}). Keeping it there so nothing goes "
    "missing. New installs use {new} instead; to move, copy {old}/logs into "
    "{new}/logs, or set GUARDIAN_SPAN_DIR (and GUARDIAN_LOG_PATH) to the "
    "directory you want."
)


def is_source_tree(package_dir: Path) -> bool:
    """True when this file sits in a checkout rather than in an installed
    package. A checkout is where its owner deliberately put it, and the README
    has always documented its logs/ as the archive, so it does not move."""
    return any((package_dir / marker).exists() for marker in SOURCE_MARKERS)


def user_data_dir(env: Dict[str, str], os_name: str = os.name) -> Path:
    """The per-user directory for an installed package."""
    if os_name == "nt":
        local = (env.get("LOCALAPPDATA") or "").strip()
        if local:
            return Path(local) / "context-guardian"
    home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    base = Path(home) if home else Path(os.path.expanduser("~"))
    return base / ".context-guardian"


def has_spans(root: Path) -> bool:
    """Whether a data directory already holds an archived span. Cheap and
    never raises: an unreadable directory counts as empty."""
    try:
        return next((root / SPANS_SUBDIR).glob("*/*.json"), None) is not None
    except OSError:
        return False


def data_dir(package_dir: Path = PACKAGE_DIR, env: Optional[Dict[str, str]] = None,
             os_name: str = os.name,
             notify: Optional[Callable[[str], None]] = None) -> Path:
    """The directory spans, logs, the version cache and `.env` live under.

    Pure apart from reading the filesystem: `notify` is called at most once,
    with the migration notice, and the caller decides whether to log it.
    """
    env = dict(os.environ) if env is None else env
    chosen = (env.get("GUARDIAN_DATA_DIR") or "").strip()
    if chosen:
        return Path(chosen)
    package_dir = Path(package_dir)
    if is_source_tree(package_dir):
        return package_dir
    new = user_data_dir(env, os_name)
    if has_spans(package_dir) and not has_spans(new):
        if notify is not None:
            notify(MIGRATION_NOTICE.format(old=package_dir, new=new))
        return package_dir
    return new


def env_file(package_dir: Path = PACKAGE_DIR,
             env: Optional[Dict[str, str]] = None) -> Path:
    """The `.env` the proxy loads and the wizard writes. A checkout resolves to
    the file beside the source, which is where it has always been."""
    return data_dir(package_dir=package_dir, env=env) / ENV_FILENAME


if __name__ == "__main__":  # a one-line answer for a bug report
    print(data_dir(notify=lambda msg: print("notice:", msg)))
