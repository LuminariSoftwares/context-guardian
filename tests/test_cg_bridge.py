"""The DSH bridge (modules/cg_bridge.py), run the way the plugin runs it: as a
child process, from a working directory that is NOT the repo."""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "modules" / "cg_bridge.py"


def test_selftest_count_line_from_foreign_cwd():
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([sys.executable, str(BRIDGE), "--selftest"], cwd=cwd,
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    m = re.search(r"cg_bridge selftest: (\d+) checks, (\d+) passed, (\d+) failed", r.stdout)
    assert m, r.stdout
    total, passed, failed = (int(x) for x in m.groups())
    assert total >= 7 and passed == total and failed == 0


def test_stdio_roundtrip_reports_the_real_proxy_config():
    frames = ('{"id": 1, "op": "hello"}\n{"id": 2, "op": "config"}\n'
              '{"id": 3, "op": "nope"}\n{"id": 4, "op": "shutdown"}\n')
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([sys.executable, str(BRIDGE)], cwd=cwd, input=frames.encode("utf-8"),
                           capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = [json.loads(line) for line in r.stdout.decode("utf-8").splitlines()]
    assert [f["id"] for f in out] == [1, 2, 3, 4]          # every stdout line is a frame
    assert out[0]["ok"] and Path(out[0]["result"]["package_root"]).name != "modules"
    # the REAL context_guardian import, with logging going to stderr, not stdout
    assert out[1]["ok"], out[1]
    assert out[1]["result"]["NUM_CTX"] > 0 and 0 < out[1]["result"]["COMPACT_THRESHOLD"] < 1
    assert out[2]["ok"] is False and "UnknownOp" in out[2]["error"]
    assert out[3]["result"]["exit"] is True
