"""Item 6 (2026-10-14): scripts/bench_proxy.py is re-runnable and deterministic."""
import re
import subprocess
import sys
from pathlib import Path

from _child_env import child_env

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


def test_the_figures_do_not_depend_on_the_temp_directory(tmp_path):
    """The compaction note names the span file and the recall script by
    absolute path, so `tokens_after` used to move with TMPDIR -- 160341 /
    160370 / 161066 for the same --turns and --seed. A benchmark whose number
    depends on where it ran is not a benchmark."""
    short, long = tmp_path / "s", tmp_path / ("l" * 40)
    short.mkdir()
    long.mkdir()
    runs = []
    for where in (short, long):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bench_proxy.py"),
             "--turns", "12", "--seed", "4"],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            env=child_env(TMPDIR=where))
        assert out.returncode == 0, out.stderr
        runs.append(LINE.match(out.stdout.strip()).groups()[:4])
    assert runs[0] == runs[1], runs


def test_the_readme_benchmark_line_is_the_one_the_script_prints():
    """The README quotes a figure; this is what makes it a claim the repo can
    back rather than a number somebody typed."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quoted = re.search(r"^turns=40 tokens_before=(\d+) tokens_after=(\d+) compactions=(\d+)",
                       readme, re.M)
    assert quoted, "the README no longer quotes a --turns 40 benchmark line"
    turns, before, after, compactions, _, _ = _run("--turns", "40", "--seed", "1")
    assert (before, after, compactions) == quoted.groups(), (
        "README benchmark line is stale; re-run: python scripts/bench_proxy.py --turns 40 --seed 1")
