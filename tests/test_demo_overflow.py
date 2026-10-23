"""The overflow demo at the top of the README is what examples/demo_overflow.py prints.

The README quotes the script's output between two markers; this runs the
script and compares, so the first thing a visitor reads cannot drift from what
the repo actually does.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "demo_overflow.py"


def _run():
    out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return out.stdout.replace("\r\n", "\n").strip("\n")


def test_the_demo_shows_a_session_the_proxy_kept_alive():
    lines = _run().splitlines()
    assert 10 <= len(lines) <= 20, len(lines)
    assert any("400 overflow" in ln for ln in lines), "the direct session never overflowed"
    assert re.search(r"proxy: (\d+)/\1 turns answered", lines[-1]), lines[-1]


def test_the_readme_quotes_the_demo_output():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"<!-- demo-overflow:start -->\n```text\n(.*?)\n```\n<!-- demo-overflow:end -->",
                  readme, re.S)
    assert m, "README has no demo-overflow block"
    assert m.group(1) == _run(), (
        "README demo block is stale; re-run: python examples/demo_overflow.py")
