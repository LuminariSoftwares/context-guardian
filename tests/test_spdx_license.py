"""The wheel must carry the SPDX licence form (PEP 639), not the TOML-table form.

setuptools stops building `license = { text = ... }` on 2027-02-18, so this builds a real wheel and reads
the METADATA it produced. Needs the `build` package (installed by CI and by the dev requirements).
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_wheel_metadata_has_spdx_license_expression(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(ROOT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata = wheel.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode("utf-8")
    lines = metadata.splitlines()
    assert "License-Expression: MIT" in lines, metadata[:600]
    assert not any(ln.startswith("Classifier: License ::") for ln in lines), "licence classifier must go with SPDX"
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names), names
