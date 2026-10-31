"""docs/ARCHIVE_FORMAT.md carries a format 2 test vector; recompute it so the doc cannot drift."""
import base64
import json
import re
from pathlib import Path

import pytest

import cg_archive

DOC = (Path(__file__).resolve().parent.parent / "docs" / "ARCHIVE_FORMAT.md").read_text(encoding="utf-8")
BODY = {"run_id": "20261031-120000", "index": 1, "at": "2026-10-31 12:00:00", "num_ctx": 1000, "message_count": 1,
        "summary": "a short summary", "messages": [{"role": "user", "content": "hello"}]}


def vector():
    out = {}
    for name in ("key", "nonce", "plaintext", "ciphertext"):
        m = re.search(r"^%s +(\S.*?)(?: +\(bytes.*\))?$" % name, DOC, re.M)
        assert m, "ARCHIVE_FORMAT.md has no %s line in its test vector" % name
        out[name] = m.group(1).strip()
    return out


def test_the_documented_vector_is_what_the_code_computes(monkeypatch):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    v = vector()
    key, nonce = base64.b64decode(v["key"]), base64.b64decode(v["nonce"])
    assert key == bytes(range(32)) and nonce == bytes(range(12))
    assert v["plaintext"] == json.dumps(BODY, ensure_ascii=False)
    assert base64.b64encode(AESGCM(key).encrypt(nonce, v["plaintext"].encode("utf-8"), None)).decode() == v["ciphertext"]
    # And through the code that writes spans, with the nonce pinned.
    monkeypatch.setattr(cg_archive.os, "urandom", lambda n: nonce[:n])
    sealed = cg_archive.Encryptor(key).seal(BODY)
    assert sealed == {"format": 2, "enc": "aes-256-gcm", "nonce": v["nonce"], "ciphertext": v["ciphertext"],
                      "run_id": "20261031-120000", "index": 1, "at": "2026-10-31 12:00:00"}


def test_an_unusable_key_is_refused_not_ignored():
    for text in ("", "not base64!!", base64.b64encode(b"x" * 16).decode()):
        env = {"GUARDIAN_ENCRYPT": "1", "GUARDIAN_ARCHIVE_KEY": text}
        with pytest.raises(ValueError):
            cg_archive.encryptor_from_env(env)
