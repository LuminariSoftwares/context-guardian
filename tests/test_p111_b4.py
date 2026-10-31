"""P111 S6 (B4) acceptance -- optional encryption of the archive at rest. Written by the O5 overseer; DO NOT EDIT.

Why: README "What this does not do" (7): "The archive is clear text on disk." B4 lets a user encrypt NEW runs end to
end with a key from the environment, keeps the seal verifiable (it hashes the bytes as written), and leaves format 1
runs readable forever. It is off by default and needs an optional extra. Plan P111 decision 5 + C4 + kill criterion
K3: no clear-text path may remain (span, digest, terms, journal, compaction-log text), or B4 closes unmerged.

Contract (pinned by O5 2026-10-31):
- Off unless GUARDIAN_ENCRYPT=1. Off = today's span files exactly (no "format" key, clear `messages`/`summary`).
- Key: GUARDIAN_ARCHIVE_KEY (base64 of exactly 32 bytes) or GUARDIAN_ARCHIVE_KEY_FILE (a file holding that base64
  text; surrounding whitespace ignored). The same key encrypts and decrypts.
- Format 2 span file: a JSON object whose keys are EXACTLY {"format": 2, "enc": "aes-256-gcm", "nonce": b64 of 12
  random bytes (fresh per file), "ciphertext": b64, "run_id", "index", "at"}. The plaintext is the UTF-8 JSON of the
  complete span body a format 1 file would have held (run_id, index, at, num_ctx, message_count, summary, messages,
  and any extra keys such as oversized/original_tokens/continues), sealed with
  cryptography's AESGCM(key).encrypt(nonce, plaintext, None).
- Every span writer encrypts: the proxy (compaction spans and the S2 oversized spans) and the Claude Code adapter.
  The adapter with GUARDIAN_ENCRYPT=1 and no usable key writes NOTHING (never a clear span) and still exits 0.
- index.jsonl lines of an encrypted run have EXACTLY the keys {"span", "ts", "messages", "chars", "est_tokens",
  "locked"} with locked true -- no digest, no terms -- also after `--reindex`, with or without the key.
- The compaction log (GUARDIAN_LOG_PATH, served by /guardian/events) carries no message or summary text for an
  encrypted run: `summary_preview` is "".
- The journal is refused: GUARDIAN_CAPTURE=journal with GUARDIAN_ENCRYPT=1 never writes a journal file.
- `cg_archive.load_span(path)` returns: a format 1 file as today; a format 2 file decrypted to the full body when the
  key (env or file) is set and correct; otherwise the clear header fields plus "locked": true and no
  `messages`/`summary`. Every reader goes through it: `context-guardian-recall <term>` finds text in an encrypted
  span with the key and prints `locked` (never the text) without it; MCP `read_span` likewise.
- `context-guardian-recall --verify` is OK on an encrypted run (the seal is unchanged).
- Starting the proxy (`python context_guardian.py`) with GUARDIAN_ENCRYPT=1 exits non-zero BEFORE serving when: no key
  (message names GUARDIAN_ARCHIVE_KEY), the key is not 32 bytes, GUARDIAN_CAPTURE=journal (message names
  GUARDIAN_CAPTURE), or `cryptography` cannot be imported (message names context-guardian[encrypt]). Importing the
  module never raises.
- Packaging: pyproject [project.optional-dependencies] encrypt = ["cryptography>=42"]; requirements-dev.txt carries
  cryptography so CI runs these tests; `cryptography` is NOT in [project] dependencies.
"""
import base64
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import cg_archive
from _child_env import child_env
from _proxy_stub import SUMMARY, load, post, wire

ROOT = Path(__file__).resolve().parent.parent
KEY_BYTES = bytes(range(32))
KEY = base64.b64encode(KEY_BYTES).decode()
OTHER_KEY = base64.b64encode(bytes(range(1, 33))).decode()
MARK = "QUARTZWHALE"
HEADER = {"format", "enc", "nonce", "ciphertext", "run_id", "index", "at"}
LOCKED_INDEX = {"span", "ts", "messages", "chars", "est_tokens", "locked"}
ENC_ENV = ("GUARDIAN_ENCRYPT", "GUARDIAN_ARCHIVE_KEY", "GUARDIAN_ARCHIVE_KEY_FILE", "GUARDIAN_CAPTURE")


def aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # required: CI installs it (requirements-dev)
    return AESGCM


def convo(n=60):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": "%s turn %02d about the %s-%02d plan with ordinary words" % ("q" if i % 2 == 0 else "a", i, MARK, i)}
            for i in range(n)]


def proxy(monkeypatch, tmp_path, encrypt=True, **env):
    for k in ENC_ENV:
        monkeypatch.delenv(k, raising=False)
    base = {"GUARDIAN_NUM_CTX": "1000", "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_KEEP_RECENT_MESSAGES": "2",
            "GUARDIAN_CALIBRATE": "0"}
    if encrypt:
        base.update(GUARDIAN_ENCRYPT="1", GUARDIAN_ARCHIVE_KEY=KEY)
    base.update(env)
    g = load(monkeypatch, tmp_path, **base)
    seen = wire(g)
    return g, seen


def send(g, messages):
    r = post(g, json.dumps({"model": "m", "messages": messages}).encode())
    assert r.status_code == 200, r.text[:300]
    return r


def run_dir(g):
    return Path(g.SPAN_DIR) / g.RUN_ID


def span_files(g):
    return sorted(run_dir(g).glob("[0-9][0-9][0-9][0-9].json"))


def raw(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def decrypt(obj, key=KEY_BYTES):
    pt = aesgcm()(key).decrypt(base64.b64decode(obj["nonce"]), base64.b64decode(obj["ciphertext"]), None)
    return json.loads(pt.decode("utf-8"))


def every_file_text(*roots):
    out = []
    for root in roots:
        root = Path(root)
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        out += [p.read_text(encoding="utf-8", errors="replace") for p in files]
    return "\n".join(out)


def encrypted_run(monkeypatch, tmp_path, **env):
    g, seen = proxy(monkeypatch, tmp_path, **env)
    send(g, convo())
    files = span_files(g)
    assert files, "the fixture must compact and write a span"
    return g, seen, files


def cli_env(tmp_path, key=True, **extra):
    env = child_env(pythonpath=ROOT, GUARDIAN_SPAN_DIR=tmp_path / "spans", GUARDIAN_DATA_DIR=tmp_path / "data",
                    GUARDIAN_LOG_PATH=tmp_path / "guardian_log.json", GUARDIAN_RECEIPTS_PATH=tmp_path / "receipts.jsonl",
                    **extra)
    if key:
        env["GUARDIAN_ARCHIVE_KEY"] = KEY
    return env


def recall(env, *args):
    return subprocess.run([sys.executable, str(ROOT / "cg_recall_cli.py"), *args], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(ROOT), encoding="utf-8")


# ---- packaging and the default ------------------------------------------------------------------------------
def test_cryptography_is_an_optional_extra_and_a_dev_requirement():
    aesgcm()
    toml = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    head, _, rest = toml.partition("[project.optional-dependencies]")
    assert rest, "pyproject has no [project.optional-dependencies]"
    section = rest.split("\n[", 1)[0]
    assert any(line.replace(" ", "").startswith('encrypt=["cryptography>=42"') for line in section.splitlines()), section
    deps = head.split("[project]", 1)[1].split("\n[", 1)[0]
    assert "cryptography" not in deps, "cryptography must not be a required dependency"
    assert "cryptography" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


def test_off_by_default_spans_are_format_1(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path, encrypt=False)
    send(g, convo())
    span = raw(span_files(g)[0])
    assert "format" not in span and "ciphertext" not in span and MARK in json.dumps(span["messages"])


# ---- the format 2 file ----------------------------------------------------------------------------------------
def test_an_encrypted_span_has_only_the_header_in_clear(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    obj = raw(files[0])
    assert set(obj) == HEADER, sorted(obj)
    assert obj["format"] == 2 and obj["enc"] == "aes-256-gcm" and obj["run_id"] == g.RUN_ID
    assert len(base64.b64decode(obj["nonce"])) == 12
    body = decrypt(obj)
    assert body["run_id"] == g.RUN_ID and body["index"] == obj["index"] and body["summary"] == SUMMARY
    assert MARK in json.dumps(body["messages"]) and body["message_count"] == len(body["messages"])


def test_nonces_are_fresh_per_file(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    send(g, convo())
    send(g, convo() + convo(40))
    objs = [raw(p) for p in span_files(g)]
    assert len(objs) >= 2
    assert len({o["nonce"] for o in objs}) == len(objs) and len({o["ciphertext"] for o in objs}) == len(objs)


def test_no_clear_text_anywhere_on_disk(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    disk = every_file_text(tmp_path / "spans", tmp_path / "guardian_log.json")
    assert MARK not in disk, "message text leaked to disk"
    assert SUMMARY not in disk, "summary text leaked to disk"
    events = [json.loads(x) for x in (tmp_path / "guardian_log.json").read_text(encoding="utf-8").splitlines() if x.strip()]
    comp = [e for e in events if e.get("event") == "compaction"]
    assert comp and all(e.get("summary_preview", "") == "" for e in comp)
    lines = [json.loads(x) for x in (run_dir(g) / "index.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines and all(set(x) == LOCKED_INDEX and x["locked"] is True for x in lines), lines
    assert all(x["messages"] > 0 and x["chars"] > 0 for x in lines), "the locked index still counts"


def test_the_model_still_gets_the_summary(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    fwd = json.dumps(json.loads(seen["chat"][-1])["messages"])
    assert SUMMARY in fwd, "encryption is about the disk; the forwarded request is unchanged"


def test_an_oversized_span_is_encrypted_too(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path)
    paste = "\n".join("%s%04d payload line with some ordinary words in it" % (MARK, i) for i in range(400))
    send(g, convo(12) + [{"role": "user", "content": paste}])
    objs = [raw(p) for p in span_files(g)]
    assert objs and all(set(o) == HEADER for o in objs)
    assert any(decrypt(o).get("oversized") is True for o in objs)
    assert MARK + "0000" in json.loads(seen["chat"][-1])["messages"][-1]["content"], "the head still reaches the model"
    assert MARK not in every_file_text(tmp_path / "spans", tmp_path / "guardian_log.json")


def test_the_journal_is_refused(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path, GUARDIAN_CAPTURE="journal")
    send(g, convo())
    assert not list((tmp_path / "spans").rglob("journal*.jsonl"))


# ---- reading -----------------------------------------------------------------------------------------------
def test_load_span_decrypts_with_the_key_and_locks_without(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    monkeypatch.setenv("GUARDIAN_ARCHIVE_KEY", KEY)
    full = cg_archive.load_span(files[0])
    assert full["summary"] == SUMMARY and MARK in json.dumps(full["messages"]) and not full.get("locked")
    for env in ({"GUARDIAN_ARCHIVE_KEY": OTHER_KEY}, {}):
        monkeypatch.delenv("GUARDIAN_ARCHIVE_KEY", raising=False)
        monkeypatch.delenv("GUARDIAN_ARCHIVE_KEY_FILE", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        locked = cg_archive.load_span(files[0])
        assert locked["locked"] is True and "messages" not in locked and "summary" not in locked, env
        assert locked["run_id"] == g.RUN_ID and locked["index"] == raw(files[0])["index"]
    kf = tmp_path / "archive.key"
    kf.write_text("  " + KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_ARCHIVE_KEY_FILE", str(kf))
    assert cg_archive.load_span(files[0])["summary"] == SUMMARY, "the key file works"


def test_format_1_stays_readable_with_a_key_set(monkeypatch, tmp_path):
    g, seen = proxy(monkeypatch, tmp_path, encrypt=False)
    send(g, convo())
    path = span_files(g)[0]
    before = raw(path)
    monkeypatch.setenv("GUARDIAN_ARCHIVE_KEY", KEY)
    assert cg_archive.load_span(path) == before


def test_recall_search_with_and_without_the_key(monkeypatch, tmp_path):
    encrypted_run(monkeypatch, tmp_path)
    r = recall(cli_env(tmp_path), MARK)
    assert r.returncode == 0 and MARK in r.stdout, r.stdout[-600:] + r.stderr[-600:]
    r = recall(cli_env(tmp_path, key=False), MARK)
    assert r.returncode == 0 and MARK not in r.stdout and "locked" in r.stdout, r.stdout[-600:]


def test_verify_is_ok_on_an_encrypted_run(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    for key in (True, False):
        r = recall(cli_env(tmp_path, key=key), "--verify", "--run", g.RUN_ID)
        assert r.returncode == 0, r.stdout[-600:] + r.stderr[-600:]


def test_reindex_keeps_the_index_locked(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    (run_dir(g) / "index.jsonl").unlink()
    r = recall(cli_env(tmp_path), "--reindex", "--run", g.RUN_ID)
    assert r.returncode == 0, r.stderr[-600:]
    lines = [json.loads(x) for x in (run_dir(g) / "index.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines and all(set(x) == LOCKED_INDEX for x in lines)
    assert MARK not in (run_dir(g) / "index.jsonl").read_text(encoding="utf-8")


def mcp_read_span(env, run, index):
    p = subprocess.Popen([sys.executable, str(ROOT / "cg_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_span", "arguments": {"run": run, "index": index}}}]
    out, err = p.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=60)
    reply = [json.loads(x) for x in out.splitlines() if x.strip() and json.loads(x).get("id") == 2][0]
    return json.dumps(reply)


def test_mcp_read_span_with_and_without_the_key(monkeypatch, tmp_path):
    g, seen, files = encrypted_run(monkeypatch, tmp_path)
    index = raw(files[0])["index"]
    assert MARK in mcp_read_span(cli_env(tmp_path), g.RUN_ID, index)
    locked = mcp_read_span(cli_env(tmp_path, key=False), g.RUN_ID, index)
    assert MARK not in locked and SUMMARY not in locked and "locked" in locked


# ---- the Claude Code adapter -------------------------------------------------------------------------------
SID = "0f8e7d6c-1111-2222-3333-444455556666"


def adapter(tmp_path, env):
    tr = tmp_path / "transcript.jsonl"
    lines = []
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(json.dumps({"type": role, "uuid": "u%d" % i, "sessionId": SID,
                                 "message": {"role": role, "content": "turn %d about %s" % (i, MARK)}}) + "\n")
    tr.write_text("".join(lines), encoding="utf-8")
    hook = json.dumps({"session_id": SID, "transcript_path": str(tr), "cwd": str(tmp_path),
                       "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": None})
    r = subprocess.run([sys.executable, str(ROOT / "cg_claude_code.py"), "capture"], input=hook, capture_output=True,
                       text=True, timeout=60, env=env, encoding="utf-8")
    return r, sorted((tmp_path / "spans" / ("cc-" + SID)).glob("[0-9]*.json"))


def test_the_claude_code_adapter_encrypts(tmp_path):
    r, files = adapter(tmp_path, cli_env(tmp_path, GUARDIAN_ENCRYPT="1"))
    assert r.returncode == 0 and files, r.stderr[-600:]
    assert all(set(raw(p)) == HEADER for p in files)
    assert MARK not in every_file_text(tmp_path / "spans")


def test_the_adapter_without_a_key_writes_nothing(tmp_path):
    r, files = adapter(tmp_path, cli_env(tmp_path, key=False, GUARDIAN_ENCRYPT="1"))
    assert r.returncode == 0 and files == [], "never a clear span when encryption was asked for"


# ---- start-up refusals -----------------------------------------------------------------------------------------
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(tmp_path, block_crypto=False, **env):
    e = cli_env(tmp_path, key=False, GUARDIAN_PORT=free_port(), GUARDIAN_ENCRYPT="1", **env)
    code = ("import sys, runpy; sys.modules['cryptography'] = None; "
            "sys.argv = ['context_guardian.py']; runpy.run_path(%r, run_name='__main__')" % str(ROOT / "context_guardian.py"))
    cmd = [sys.executable, "-c", code] if block_crypto else [sys.executable, str(ROOT / "context_guardian.py")]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=e, cwd=str(tmp_path),
                              encoding="utf-8")
    except subprocess.TimeoutExpired:
        pytest.fail("the proxy started serving instead of refusing")


@pytest.mark.parametrize("env,block,needle", [({}, False, "GUARDIAN_ARCHIVE_KEY"),
                                              ({"GUARDIAN_ARCHIVE_KEY": base64.b64encode(b"x" * 16).decode()}, False,
                                               "32"),
                                              ({"GUARDIAN_ARCHIVE_KEY": KEY, "GUARDIAN_CAPTURE": "journal"}, False,
                                               "GUARDIAN_CAPTURE"),
                                              ({"GUARDIAN_ARCHIVE_KEY": KEY}, True, "context-guardian[encrypt]")])
def test_start_refuses_loudly(tmp_path, env, block, needle):
    r = start(tmp_path, block_crypto=block, **env)
    assert r.returncode != 0, (r.stdout + r.stderr)[-800:]
    assert needle in r.stdout + r.stderr, (r.stdout + r.stderr)[-800:]
