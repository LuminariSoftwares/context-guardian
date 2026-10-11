"""Every GUARDIAN_* environment variable either engine reads is documented.

As re-scoped 2026-09-26: the Python proxy and the DSH engine are different
programs with different jobs, so behavioural parity reduces to one promise a user relies
on -- a knob that exists in either front door is written down. This test reads the
sources, not a hand-kept list, so a new env var without a docs row fails CI.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [p for pat in ("*.py", "*.js", "*.mjs", "modules/*.py", "vendor/*.js") for p in ROOT.glob(pat)]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

# Only real environment READS count, not constants that merely share the prefix.
READ_PATTERNS = [
    r"environ(?:\.get)?\(\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"environ\[\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"getenv\(\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"env\.(GUARDIAN_[A-Z0-9_]+)",
    r"env\[\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"num\(\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"list\(\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
    r"_env_(?:int|float|bool|str)\(\s*[\"'](GUARDIAN_[A-Z0-9_]+)",
]


def env_reads():
    found = {}
    for path in SOURCES:
        if "test" in path.name or path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in READ_PATTERNS:
            for name in re.findall(pat, text):
                found.setdefault(name, set()).add(path.name)
    return found


def test_sources_were_scanned():
    names = env_reads()
    # both engines read GUARDIAN_NUM_CTX; if the scan finds nothing, the patterns broke
    assert "GUARDIAN_NUM_CTX" in names, sorted(names)
    files = set().union(*names.values())
    assert "engine.js" in files and "context_guardian.py" in files, sorted(files)


def test_every_env_var_is_documented():
    docs = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in DOCS if p.exists())
    missing = {name: sorted(files) for name, files in env_reads().items() if name not in docs}
    assert not missing, "undocumented env vars (add a row to README.md or docs/): %s" % missing
