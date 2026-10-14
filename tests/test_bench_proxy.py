"""Item 6 (2026-10-14): scripts/bench_proxy.py is re-runnable and deterministic."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^turns=(\d+) tokens_before=(\d+) tokens_after=(\d+) "
                  r"compactions=(\d+) p50_added_ms=([\d.]+) p95_added_ms=([\d.]+)$")


def _run(*args):
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_proxy.py"), *args],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert len(lines) == 1, out.stdout
    m = LINE.match(lines[0])
    assert m, lines[0]
    return m.groups()


def test_bench_prints_one_line_and_compacts():
    turns, before, after, compactions, p50, p95 = _run("--turns", "30", "--seed", "3")
    assert turns == "30"
    assert int(compactions) >= 1
    assert int(after) < int(before)
    assert float(p50) <= float(p95)


def test_same_seed_same_tokens():
    a = _run("--turns", "20", "--seed", "5")
    b = _run("--turns", "20", "--seed", "5")
    assert a[:4] == b[:4]


def test_bench_leaves_no_files_in_the_repo():
    before = set(p for p in ROOT.rglob("*") if ".git" not in p.parts
                 and "node_modules" not in p.parts and "__pycache__" not in p.parts)
    _run("--turns", "10", "--seed", "1")
    after = set(p for p in ROOT.rglob("*") if ".git" not in p.parts
                and "node_modules" not in p.parts and "__pycache__" not in p.parts)
    assert after == before
