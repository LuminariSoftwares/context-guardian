"""cg_mask: key-shaped values are masked in what recall shows; the archive is untouched."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cg_mask  # noqa: E402

# Built at runtime so no key-shaped literal sits in the repo.
KEY = "sk-live-" + "ABCD" * 4 + "1234"
AKID = "AKIA" + "Q7XZ" * 4
AWS = "wJalr" + "XUtnFEMI" * 4 + "EXAMPLE1"
TOKEN = "tok" + "9f8e7d6c" * 4
GHP = "ghp_" + "aB3dE5" * 6
SLACK = "xoxb-" + "1234567890" + "-" + "abcdefABCDEF"

# Shapes both code-guardian and cg_mask must detect: (secret as built, text it sits in).
SHARED_SHAPES = [
    ("provider sk", KEY, "key " + KEY),
    ("aws access key id", AKID, "id " + AKID),
    ("aws secret pair", AWS, "aws_secret_access_key=" + AWS),
    ("github token", GHP, "tok " + GHP),
]


@pytest.fixture(autouse=True)
def _mask_on(monkeypatch):
    monkeypatch.delenv("MASK_OFF", raising=False)


def test_contract_example():
    assert cg_mask.mask(KEY) == "sk-****1234"


@pytest.mark.parametrize("name,secret,text", SHARED_SHAPES, ids=[s[0] for s in SHARED_SHAPES])
def test_shared_shapes_are_masked(name, secret, text):
    out = cg_mask.mask(text)
    assert secret not in out and "****" in out


def test_assignment_keeps_the_name_and_masks_the_value():
    assert cg_mask.mask("token=" + TOKEN) == "token=" + TOKEN[:3] + "****" + TOKEN[-4:]
    assert cg_mask.mask('{"api_key": "' + TOKEN + '"}') == '{"api_key": "' + TOKEN[:3] + "****" + TOKEN[-4:] + '"}'
    assert "password=hunter2xyz" not in cg_mask.mask("password=hunter2xyz")
    assert cg_mask.mask("password=hunter2xyz").startswith("password=hun****")


def test_long_run_after_secret_named_key():
    run = "a1" * 32
    assert run not in cg_mask.mask("client_secret: " + run)


def test_slack_token_masked():
    assert SLACK not in cg_mask.mask(SLACK)


def test_empty_and_idempotent():
    assert cg_mask.mask("") == ""
    s = "a %s b token=%s c %s" % (KEY, TOKEN, AKID)
    once = cg_mask.mask(s)
    assert cg_mask.mask(once) == once


def test_prose_and_urls_unchanged():
    for s in ("The exporter writes CSV rows; see https://example.org/docs?page=2 for the format.",
              "password: required for login", "the token bucket refills", "skip-this-step-please-now is fine",
              "Set the secret in your vault."):
        assert cg_mask.mask(s) == s


def test_mask_off_is_read_at_call_time(monkeypatch):
    assert cg_mask.mask(KEY) != KEY
    monkeypatch.setenv("MASK_OFF", "1")
    assert cg_mask.mask(KEY) == KEY
    monkeypatch.setenv("MASK_OFF", "0")
    assert cg_mask.mask(KEY) != KEY


def test_fail_open_and_logs_once(monkeypatch, capsys):
    class Boom:
        def sub(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(cg_mask, "_PROVIDER", Boom())
    monkeypatch.setattr(cg_mask, "_logged", False)
    assert cg_mask.mask("x " + KEY) == "x " + KEY
    assert cg_mask.mask("y " + KEY) == "y " + KEY
    assert capsys.readouterr().err.count("cg_mask: masking failed") == 1


def _env(tmp_path, **extra):
    env = dict(os.environ, PYTHONPATH=str(ROOT), GUARDIAN_SPAN_DIR=str(tmp_path / "spans"))
    env.pop("MASK_OFF", None)
    env.update(extra)
    return env


def _capture(tmp_path):
    t = tmp_path / "t.jsonl"
    line = {"type": "user", "message": {"role": "user", "content": "EXAMPLE-ONLY my key is %s ok" % KEY}}
    t.write_text(json.dumps(line) + "\n", encoding="utf-8")
    hook = {"session_id": "mask-1", "transcript_path": str(t), "cwd": str(tmp_path), "hook_event_name": "PreCompact"}
    subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), "capture"], input=json.dumps(hook),
                   capture_output=True, encoding="utf-8", timeout=120, env=_env(tmp_path), cwd=str(tmp_path))
    return tmp_path / "spans" / "cc-mask-1"


def _mcp(tmp_path, name, args, **extra):
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); import cg_mcp;"
            "r=cg_mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':sys.argv[2],"
            "'arguments':json.loads(sys.argv[3])}}); print(json.dumps(r))")
    r = subprocess.run([sys.executable, "-c", code, str(ROOT), name, json.dumps(args)], capture_output=True,
                       encoding="utf-8", timeout=120, env=_env(tmp_path, **extra), cwd=str(tmp_path),
                       stdin=subprocess.DEVNULL)
    return json.loads(r.stdout.strip().splitlines()[-1])["result"]["content"][0]["text"]


def test_recall_surfaces_mask_and_archive_is_untouched(tmp_path):
    run = _capture(tmp_path)
    assert KEY in "".join(p.read_text(encoding="utf-8") for p in run.glob("0*.json"))
    read = _mcp(tmp_path, "read_span", {"run": "cc-mask-1", "index": 1})
    assert "sk-****1234" in read and KEY not in read
    found = _mcp(tmp_path, "search_spans", {"term": "my key"})
    assert KEY not in found and "my key" in found
    cli = subprocess.run([sys.executable, str(ROOT / "cg_recall_cli.py"), "my key"], capture_output=True,
                         encoding="utf-8", timeout=120, env=_env(tmp_path), cwd=str(tmp_path),
                         stdin=subprocess.DEVNULL)
    assert KEY not in cli.stdout and "my key" in cli.stdout
    assert KEY in _mcp(tmp_path, "read_span", {"run": "cc-mask-1", "index": 1}, MASK_OFF="1")
    assert "sk-****" not in _mcp(tmp_path, "list_spans", {"run": "cc-mask-1"})
