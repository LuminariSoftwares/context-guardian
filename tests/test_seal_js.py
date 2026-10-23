"""Spans the DSH engine writes verify with the Python verifier (Phase 1).

Two writers, one format: cg_seal.js writes the chain from Node, cg_archive.py
checks it. Skipped only where no Node is installed; every CI runner has one.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import cg_recall_cli

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

WRITER = """
import { pathToFileURL } from 'node:url'
const seal = await import(pathToFileURL(process.argv[1]).href)
const spanDir = process.argv[2]
for (const i of [1, 2, 3, 4]) {
  seal.writeSealedSpan({ spanDir, runId: 'dsh-20261023120000-1', startIndex: i,
    payload: index => ({ run_id: 'dsh-20261023120000-1', index, summary: 's' + index,
                         messages: [{ role: 'user', content: 'caf\\u00e9 ' + index }] }) })
}
seal.pruneSealed({ spanDir, keepSpans: 3 })
"""


def _write(spans):
    proc = subprocess.run([NODE, "--input-type=module", "-e", WRITER, str(ROOT / "cg_seal.js"), str(spans)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_js_written_spans_verify_ok(tmp_path, monkeypatch, capsys):
    spans = tmp_path / "spans"
    _write(spans)
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(spans))
    code = cg_recall_cli.main(["--verify", "--all"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "pruned" in out and "0001.json" in out


def test_tampering_with_a_js_span_is_caught(tmp_path, monkeypatch, capsys):
    spans = tmp_path / "spans"
    _write(spans)
    target = spans / "dsh-20261023120000-1" / "0003.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["summary"] = "rewritten"
    target.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(spans))
    code = cg_recall_cli.main(["--verify", "--all"])
    out = capsys.readouterr().out
    assert code != 0 and "content-changed" in out and "0003.json" in out, out
