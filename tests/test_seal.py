"""Phase 1 acceptance: the span archive is sealed, so "lossless on disk" can be checked.

Every span write appends one line to <span_dir>/<run_id>/chain.jsonl carrying
the SHA-256 of the bytes on disk and the hash of the previous line, and
`context-guardian-recall --verify` walks that chain. These tests are the spec:
written before the code, seen failing, then passing.

Tamper-EVIDENT, not tamper-proof: anyone who can write the directory can
rehash a whole chain, which is why the anchor file (test_g) exists.
"""
import json
import threading
from pathlib import Path

import pytest

from _proxy_stub import SUMMARY, load, post, wire

import cg_recall_cli


def _spans(g, n, start=1):
    paths = []
    for i in range(start, start + n):
        p = g.write_span([{"role": "user", "content": "message %d" % i}], "summary %d" % i, i)
        assert p is not None
        paths.append(Path(p))
    return paths


def _verify(capsys, *args):
    code = cg_recall_cli.main(["--verify", *args])
    return code, capsys.readouterr().out


@pytest.fixture()
def g(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_SEAL_ANCHOR_PATH", raising=False)
    monkeypatch.delenv("GUARDIAN_SEAL", raising=False)
    return load(monkeypatch, tmp_path)


def _chain(g):
    path = Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# (a) ------------------------------------------------------------------------
def test_a_three_sealed_spans_verify_ok(g, capsys):
    _spans(g, 3)
    chain = _chain(g)
    assert [e["seq"] for e in chain] == [1, 2, 3]
    assert [e["file"] for e in chain] == ["0001.json", "0002.json", "0003.json"]
    assert chain[0]["prev"] == "genesis:%s" % g.RUN_ID
    assert chain[1]["prev"] == chain[0]["entry_hash"]
    assert chain[2]["prev"] == chain[1]["entry_hash"]
    code, out = _verify(capsys, "--all")
    assert code == 0, out
    assert g.RUN_ID in out and "OK" in out


def test_a_the_sealed_hash_is_of_the_bytes_on_disk(g):
    import hashlib
    paths = _spans(g, 1)
    assert _chain(g)[0]["sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()


# (b) ------------------------------------------------------------------------
def test_b_one_flipped_byte_in_span_2_is_reported_as_exactly_that(g, capsys):
    paths = _spans(g, 3)
    data = bytearray(paths[1].read_bytes())
    data[len(data) // 2] ^= 0x01
    paths[1].write_bytes(bytes(data))
    code, out = _verify(capsys, "--all")
    assert code != 0, out
    assert "content-changed" in out and "0002.json" in out, out
    assert "0001.json" not in out and "0003.json" not in out, out


# (c) ------------------------------------------------------------------------
def test_c_a_span_deleted_without_a_prune_record_is_missing(g, capsys):
    paths = _spans(g, 3)
    paths[0].unlink()
    code, out = _verify(capsys, "--all")
    assert code != 0, out
    assert "file-missing" in out and "0001.json" in out, out


# (d) ------------------------------------------------------------------------
def test_d_retention_pruning_is_expected_not_a_break(g, monkeypatch, capsys):
    monkeypatch.setattr(g, "KEEP_SPANS", 2)
    _spans(g, 3)
    run = Path(g.SPAN_DIR) / g.RUN_ID
    assert not (run / "0001.json").exists()
    pruned = [e for e in _chain(g) if e.get("type") == "pruned"]
    assert [e["file"] for e in pruned] == ["0001.json"]
    code, out = _verify(capsys, "--all")
    assert code == 0, out
    assert "pruned" in out and "0001.json" in out, out


# (e) ------------------------------------------------------------------------
def test_e_concurrent_writes_make_one_unforked_chain(g, capsys):
    barrier = threading.Barrier(8)
    results = []

    def worker(i):
        barrier.wait()
        results.append(g.write_span([{"role": "user", "content": "t%d" % i}], "s", 1))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results) and len(set(results)) == 8
    chain = _chain(g)
    assert [e["seq"] for e in chain] == list(range(1, 9))
    for before, after in zip(chain, chain[1:]):
        assert after["prev"] == before["entry_hash"]
    assert sorted(e["file"] for e in chain) == sorted(Path(p).name for p in results)
    code, out = _verify(capsys, "--all")
    assert code == 0, out


# (f) ------------------------------------------------------------------------
def test_f_a_chain_that_cannot_be_written_never_costs_the_request(monkeypatch, tmp_path, caplog):
    g = load(monkeypatch, tmp_path, GUARDIAN_KEEP_RECENT_MESSAGES="2")
    seen = wire(g)
    # A directory where the chain file should be: open() fails on every OS and
    # for every user, root included.
    (Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl").mkdir(parents=True)
    body = json.dumps({"model": "m", "messages": [
        {"role": "user", "content": "x" * 1500} for _ in range(5)]}).encode()
    with caplog.at_level("WARNING", logger="context_guardian"):
        resp = post(g, body)
    assert resp.status_code == 200
    assert g._state["compactions_performed"] == 1
    forwarded = json.loads(seen["chat"][-1])
    assert SUMMARY in forwarded["messages"][0]["content"]
    assert any("seal" in r.getMessage().lower() for r in caplog.records), caplog.text


# (g) ------------------------------------------------------------------------
def test_g_a_rehashed_chain_is_caught_by_the_anchor(monkeypatch, tmp_path, capsys):
    import cg_archive
    anchor = tmp_path / "elsewhere" / "anchor.jsonl"
    g = load(monkeypatch, tmp_path, GUARDIAN_SEAL_ANCHOR_PATH=str(anchor))
    paths = _spans(g, 3)
    assert anchor.exists()
    # Somebody with write access edits span 2 and rehashes the whole chain to
    # match: the chain alone cannot tell.
    paths[1].write_text(paths[1].read_text(encoding="utf-8").replace("message 2", "message X"),
                        encoding="utf-8")
    run = Path(g.SPAN_DIR) / g.RUN_ID
    old = _chain(g)
    (run / "chain.jsonl").unlink()
    for e in old:
        fields = {k: v for k, v in e.items() if k not in ("seq", "prev", "entry_hash", "ts")}
        if fields["file"] == "0002.json":
            fields["sha256"] = cg_archive.sha256_file(paths[1])
        cg_archive.append_entry(run, g.RUN_ID, fields)          # no anchor: the forger has none
    monkeypatch.delenv("GUARDIAN_SEAL_ANCHOR_PATH")
    code, out = _verify(capsys, "--all")
    assert code == 0, "without the anchor the forgery is invisible -- that is the point: " + out
    monkeypatch.setenv("GUARDIAN_SEAL_ANCHOR_PATH", str(anchor))
    code, out = _verify(capsys, "--all")
    assert code != 0, out
    assert "anchor-mismatch" in out, out


# --- the rest of the contract ---------------------------------------------------

def test_an_unsealed_legacy_run_is_reported_then_backfilled(monkeypatch, tmp_path, capsys):
    g = load(monkeypatch, tmp_path, GUARDIAN_SEAL="0")
    _spans(g, 2)
    assert not (Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl").exists()
    code, out = _verify(capsys, "--all")
    assert code != 0 and "unsealed" in out.lower(), out
    code, out = _verify(capsys, "--seal-existing")
    assert code == 0, out
    chain = _chain(g)
    assert [e["file"] for e in chain] == ["0001.json", "0002.json"]
    assert all(e.get("backfilled") is True for e in chain)
    code, out = _verify(capsys, "--all")
    assert code == 0 and "backfilled" in out, out


def test_heads_are_printed_and_in_stats(g, capsys):
    from fastapi.testclient import TestClient
    from _proxy_stub import LOOPBACK
    _spans(g, 2)
    head = _chain(g)[-1]["entry_hash"]
    code, out = _verify(capsys, "--heads")
    assert code == 0 and head in out, out
    seal = TestClient(g.app, base_url=LOOPBACK).get("/guardian/stats").json()["seal"]
    assert seal["enabled"] is True and seal["head"] == head and seal["seq"] == 2


def test_a_reordered_chain_is_reported(g, capsys):
    _spans(g, 3)
    path = Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = _verify(capsys, "--all")
    assert code != 0 and "reordered" in out, out


def test_an_edited_entry_is_reported(g, capsys):
    _spans(g, 2)
    path = Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["ts"] = "1999-01-01T00:00:00+00:00"
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = _verify(capsys, "--all")
    assert code != 0 and "entry-edited" in out, out


def test_a_forked_chain_is_reported(g, capsys):
    import cg_archive
    _spans(g, 2)
    path = Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    # A second writer that did not see entry 2 also chains onto entry 1.
    twin = {"seq": 2, "type": "span", "file": "0009.json", "sha256": "0" * 64,
            "prev": first["entry_hash"], "ts": first["ts"]}
    twin["entry_hash"] = cg_archive.entry_hash(twin)
    path.write_text("\n".join(lines + [json.dumps(twin)]) + "\n", encoding="utf-8")
    code, out = _verify(capsys, "--all")
    assert code != 0 and "forked" in out, out


def test_seal_off_switch_writes_no_chain(monkeypatch, tmp_path):
    g = load(monkeypatch, tmp_path, GUARDIAN_SEAL="0")
    _spans(g, 1)
    assert not (Path(g.SPAN_DIR) / g.RUN_ID / "chain.jsonl").exists()


def test_verify_without_an_archive_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "nope"))
    code, _ = _verify(capsys, "--all")
    assert code == 2


def test_the_canonical_form_is_pinned():
    """The JS writer (cg_seal.js) pins the same value in tests/seal_smoke.mjs:
    one golden hash, two implementations."""
    import cg_archive
    entry = {"seq": 1, "type": "span", "file": "0001.json", "sha256": "ab" * 32,
             "prev": "genesis:20261023-120000", "ts": "2026-10-23T12:00:00-05:00"}
    assert cg_archive.canonical(entry) == (
        b'{"file":"0001.json","prev":"genesis:20261023-120000","seq":1,'
        b'"sha256":"' + b"ab" * 32 + b'","ts":"2026-10-23T12:00:00-05:00","type":"span"}')
    assert cg_archive.entry_hash(entry) == GOLDEN_ENTRY_HASH


GOLDEN_ENTRY_HASH = "46fc20f69dc23fcda35e4c1688428bd5f1a3e8ac5a44f3ea19d5227de02f418d"
