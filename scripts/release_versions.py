"""Decide what a release publishes: the PyPI version the tag names, and the npm version if it is new.

    python scripts/release_versions.py --tag v0.12.0
    python scripts/release_versions.py --dry-run      # tag taken from pyproject.toml; report, never fail

PyPI (pyproject.toml) and npm (package.json) are independent version lines. The release tag names the
PyPI version: `vX.Y.Z` must equal pyproject.toml's version exactly as written there. package.json is not
compared with anything; its version is published to npm only when that exact version is not already on
the registry. A release where neither version is new is an error: there is nothing to publish.

Exit codes: 0 releasable (or --dry-run), 1 not releasable, 2 bad arguments, unreadable files, or a
registry lookup that failed for a reason other than "not found".
Prints `key=value` lines for $GITHUB_OUTPUT: pypi_version, npm_version, npm_prerelease, publish_pypi,
publish_npm.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
PYPI_NAME = "context-guardian"


class LookupFailed(Exception):
    """A registry answered with something other than found / not found."""


def pyproject_version(path):
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)["project"]["version"]
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def on_pypi(name, version):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise LookupFailed(f"PyPI lookup {url}: HTTP {exc.code}") from exc
    except OSError as exc:
        raise LookupFailed(f"PyPI lookup {url}: {exc}") from exc


def on_npm(name, version):
    """`npm view <pkg>@<version> version` prints the version when it exists and nothing when it does not."""
    try:
        r = subprocess.run(["npm", "view", f"{name}@{version}", "version"],
                           capture_output=True, text=True, timeout=120, shell=sys.platform == "win32")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LookupFailed(f"npm view {name}@{version}: {exc}") from exc
    if r.returncode == 0:
        return bool(r.stdout.strip())
    if "E404" in r.stderr:  # the package itself is not on the registry yet
        return False
    raise LookupFailed(f"npm view {name}@{version} exited {r.returncode}: {r.stderr.strip()[-300:]}")


def plan(tag, py_version, npm_name, npm_version, pypi_lookup=on_pypi, npm_lookup=on_npm):
    """Return (problems, notices, outputs). Empty problems means the tag can be released."""
    problems, notices = [], []
    if tag != "v" + py_version:
        problems.append(f"tag {tag} does not name the PyPI version: pyproject.toml is {py_version}, "
                        f"so the tag must be v{py_version}")
    publish_pypi = not pypi_lookup(PYPI_NAME, py_version)
    publish_npm = not npm_lookup(npm_name, npm_version)
    if not publish_pypi:
        notices.append(f"{PYPI_NAME} {py_version} is already on PyPI; skipping PyPI")
    if not publish_npm:
        notices.append(f"{npm_name}@{npm_version} is already on npm; skipping npm")
    if not publish_pypi and not publish_npm:
        problems.append("nothing to publish: neither pyproject.toml nor package.json has a version "
                        "that is not already on its registry")
    outputs = {
        "pypi_version": py_version,
        "npm_version": npm_version,
        "npm_prerelease": "true" if "-" in npm_version else "false",
        "publish_pypi": "true" if publish_pypi else "false",
        "publish_npm": "true" if publish_npm else "false",
    }
    return problems, notices, outputs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", help="release tag, e.g. v0.12.0")
    ap.add_argument("--dry-run", action="store_true", help="report a problem as a warning and exit 0")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        npm_name, npm_version = pkg["name"], pkg["version"]
        py_version = pyproject_version(root / "pyproject.toml")
    except (OSError, KeyError, ValueError) as exc:
        print(f"cannot read versions: {exc}", file=sys.stderr)
        return 2
    if not py_version:
        print("cannot read versions: no version in pyproject.toml", file=sys.stderr)
        return 2
    tag = args.tag or ("v" + py_version if args.dry_run else None)
    if not tag:
        print("--tag is required unless --dry-run", file=sys.stderr)
        return 2

    print(f"tag={tag} pyproject.toml={py_version} package.json={npm_version}")
    try:
        problems, notices, outputs = plan(tag, py_version, npm_name, npm_version)
    except LookupFailed as exc:
        print(f"::error::{exc}")
        return 2
    for n in notices:
        print(f"::notice::{n}")
    # ::warning:: / ::error:: are GitHub annotations; plain text everywhere else.
    for p in problems:
        print(f"::{'warning' if args.dry_run else 'error'}::{p}")
    if problems and not args.dry_run:
        return 1
    print("dry run: a release from this commit would FAIL" if problems else "releasable")
    for k, v in outputs.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
