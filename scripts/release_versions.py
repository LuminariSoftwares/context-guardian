"""Check that a release tag names the version both packages are about to publish.

    python scripts/release_versions.py --tag v0.12.0
    python scripts/release_versions.py --dry-run      # tag taken from package.json; report, never fail

The PyPI package (pyproject.toml) and the npm package (package.json) ship from one tag, so all three
must agree. npm writes a prerelease as `0.12.0-beta.1`; PEP 440 writes the same version as
`0.12.0b1`. The tag uses the npm form, pyproject.toml the PEP 440 form, and this maps one onto the
other. Anything it cannot map is a mismatch, never a guess.

Exit codes: 0 versions agree (or --dry-run), 1 mismatch, 2 bad arguments or unreadable files.
On success it prints `version=<npm version>` and `prerelease=<true|false>` for $GITHUB_OUTPUT.
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$")
PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


def pep440(npm_version):
    """`0.12.0-beta.1` -> `0.12.0b1`; `0.12.0` -> `0.12.0`; anything else -> None."""
    m = SEMVER.match(npm_version)
    if not m:
        return None
    base = ".".join(m.group(1, 2, 3))
    return base + (PEP440_PRE[m.group(4)] + m.group(5) if m.group(4) else "")


def pyproject_version(path):
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)["project"]["version"]
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def check(tag, py_version, npm_version):
    """Return a list of problems; empty means the tag can be released."""
    problems = []
    if not tag.startswith("v"):
        return [f"tag {tag!r} must look like vX.Y.Z or vX.Y.Z-beta.N"]
    wanted = tag[1:]
    if pep440(wanted) is None:
        return [f"tag {tag!r} must look like vX.Y.Z or vX.Y.Z-beta.N"]
    if npm_version != wanted:
        problems.append(f"package.json version is {npm_version}, tag wants {wanted}")
    if py_version != pep440(wanted):
        problems.append(f"pyproject.toml version is {py_version}, tag wants {pep440(wanted)}")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", help="release tag, e.g. v0.12.0")
    ap.add_argument("--dry-run", action="store_true", help="report a mismatch as a warning and exit 0")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        npm_version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
        py_version = pyproject_version(root / "pyproject.toml")
    except (OSError, KeyError, ValueError) as exc:
        print(f"cannot read versions: {exc}", file=sys.stderr)
        return 2
    tag = args.tag or ("v" + npm_version if args.dry_run else None)
    if not tag:
        print("--tag is required unless --dry-run", file=sys.stderr)
        return 2

    print(f"tag={tag} package.json={npm_version} pyproject.toml={py_version}")
    problems = check(tag, py_version, npm_version)
    for p in problems:
        # ::warning:: / ::error:: are GitHub annotations; plain text everywhere else.
        print(f"::{'warning' if args.dry_run else 'error'}::{p}")
    if problems and not args.dry_run:
        return 1
    if problems:
        print("dry run: a release from this commit would FAIL the version check")
    else:
        print("versions agree")
    print(f"version={tag[1:]}")
    print(f"prerelease={'true' if '-' in tag else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
