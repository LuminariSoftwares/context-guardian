"""scripts/memo_drift_probe.py: its mechanics only.

The probe exists because the stub summariser returns a fixed text, so the
suite cannot measure whether an extended summary keeps early facts. These
tests do not measure that either: they check the script runs end to end,
plants and finds its facts, and stays inside its temporary directory. The
measurement is the owner's to run on a real model.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import memo_drift_probe as probe  # noqa: E402


def test_help_says_how_to_run_it():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "memo_drift_probe.py"), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "--upstream" in out.stdout and "--model" in out.stdout
    assert "python scripts/memo_drift_probe.py --upstream" in out.stdout
    assert "talks only to --upstream" in out.stdout


def test_the_upstream_must_be_a_url():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "memo_drift_probe.py"),
                          "--upstream", "file:///etc/passwd", "--model", "m"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "http" in out.stderr


def test_the_facts_are_planted_and_recognised_in_a_paraphrase():
    planted = " ".join(probe.user_turn(i) for i in range(3))
    assert sorted(probe.facts_in(planted)) == sorted(n for n, _, _ in probe.FACTS)
    paraphrase = ("Codename Blue Heron 47; deploy on 9417; Vasquez reviews; freeze March 14; "
                  "config in quill's settings.toml.")
    assert len(probe.facts_in(paraphrase)) == 5
    assert probe.facts_in("nothing relevant") == []


def test_a_stub_run_prints_a_line_per_compaction_and_the_table(monkeypatch, tmp_path, capsys):
    calls = {"summary": 0, "chat": 0, "hosts": set()}

    def handler(request):
        calls["hosts"].add(request.url.host)
        body = json.loads(request.content)
        if "BEGIN_TRANSCRIPT" in json.dumps(body):
            calls["summary"] += 1
            text = "Summary: codename BLUE HERON-47, port 9417, reviewer Okonkwo Vasquez. " * 2
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text},
                                                          "finish_reason": "stop"}]})
        calls["chat"] += 1
        reply = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Noted."},
                                         "finish_reason": "stop"}]}).encode()

        async def _stream():                                # forwarded replies are streamed through
            yield reply
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_stream())

    monkeypatch.setattr(probe, "UPSTREAM_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setitem(sys.modules, "dotenv", sys.modules.get("dotenv"))
    monkeypatch.setattr(probe.tempfile, "tempdir", str(tmp_path))
    saved = dict(os.environ)
    try:
        assert probe.main(["--upstream", "http://127.0.0.1:9/v1", "--model", "m", "--turns", "12"]) == 0
    finally:
        os.environ.clear()
        os.environ.update(saved)
    out = capsys.readouterr().out
    assert calls["hosts"] == {"127.0.0.1"}
    assert calls["chat"] == 24 and calls["summary"] >= 2
    assert "[memo on] turn" in out and "[memo off] turn" in out
    assert "facts 3/5  missing: freeze,config" in out
    assert "| memo on |" in out and "| memo off |" in out
    assert list(tmp_path.iterdir()) == []                   # the temporary directory is gone
