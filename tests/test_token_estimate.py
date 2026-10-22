"""The token estimate is content-blind, and the README says so.

estimate_tokens is `characters / GUARDIAN_CHARS_PER_TOKEN`, and nothing about
what those characters ARE enters the sum. The README used to claim the
estimate "triggers a little early rather than late", which is true only for
the content whose real ratio is above 3.5 characters per token. English prose
is; minified JSON and CJK text are well below it, so on that traffic the
estimate reads low and compaction fires LATE -- the direction that lets the
backend overflow first, which is the failure this proxy exists to prevent.

This file does not claim a multiplier: a real ratio needs a real tokenizer and
this package has no dependency on one. It pins the mechanism, which is what
makes the README's statement a fact about the code.
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROSE = "The quick brown fox jumps over the lazy dog and then considers it."
JSON_ISH = '{"a":1,"b":[2,3],"c":{"d":"e"},"f":true,"g":null,"h":"i","j":4.5}'
CJK = "日本語のテキストは一文字あたりのトークン数が多く、推定が低く出ます"


def _cg():
    return (importlib.reload(sys.modules["context_guardian"])
            if "context_guardian" in sys.modules
            else importlib.import_module("context_guardian"))


def test_the_same_length_estimates_the_same_tokens_whatever_the_content():
    cg = _cg()
    pad = max(len(PROSE), len(JSON_ISH), len(CJK))
    estimates = {name: cg.estimate_tokens([{"role": "user", "content": text.ljust(pad, text[0])[:pad]}])
                 for name, text in (("prose", PROSE), ("json", JSON_ISH), ("cjk", CJK))}
    assert len(set(estimates.values())) == 1, estimates


def test_the_estimate_is_characters_over_the_configured_ratio(monkeypatch):
    monkeypatch.setenv("GUARDIAN_CHARS_PER_TOKEN", "3.5")
    cg = _cg()
    text = "x" * 350
    assert cg.estimate_tokens([{"role": "user", "content": text}]) >= 100


def test_the_readme_does_not_claim_the_estimate_errs_early():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "triggers a little early rather than late" not in readme
    assert "content-blind" in readme or "nothing about the *content*" in readme
    # and it names the direction that actually matters
    assert "fires **late**" in readme
