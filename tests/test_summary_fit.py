"""Acceptance for the summary-fit fix (O5, 2026-11-01). DO NOT EDIT -- written before the change, from the contract.

Found on a guardian-leaderboard calibration run (RTX 3060 12 GB, 2026-11-01): at num_ctx 8192, qwen3-coder:30b logged one
`compaction_refused` / `empty_or_short_summary` (29 chars < 40) in every seed, at 28,654-33,428 estimated tokens, and
the request then went upstream at about four times the window. The summarisation call sends the WHOLE evicted range
in one prompt, sized by nothing, so once that range outgrows the window the backend truncates the prompt (the
instructions first) and the "summary" comes back empty.

Contract (C-SummaryFit):
  * summary_input_budget(num_ctx, reserve=None) -> int: the most tokens one summarisation PROMPT may estimate at.
    reserve defaults to RESERVE_OUTPUT; a reserve >= num_ctx is clamped to num_ctx // 2 (the same rule as
    effective_threshold); result = max(1, num_ctx - reserve).
  * summarize_older_messages(client, model, older, headers=None, budget_tokens=None):
      - budget_tokens None, or the whole prompt fits: exactly ONE call with exactly today's body (no change).
      - otherwise: consecutive groups of messages, each call's prompt estimated (estimate_tokens of the one user
        message) <= budget_tokens. The first call condenses group 1; every later call condenses
        [{"role": EARLIER_SUMMARY_ROLE, "content": <previous summary>}] + the next group (the memo "extend" shape),
        so the result is ONE rolling summary: the last call's text. Every message appears in exactly one call.
      - a single message too big for any call on its own is cut to fit, keeping its start and its end with
        "[... <N> chars cut for the summary call ...]" between them (the full message is archived in the span as now).
      - any call failing -> None (fail open, exactly as today).
  * maybe_compact and maybe_compact_anthropic summarise with budget_tokens = summary_input_budget(num_ctx), so the
    proxy never sends a summarisation prompt larger than the window it just measured.
"""
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def g(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDIAN_NUM_CTX", "1000")
    monkeypatch.setenv("GUARDIAN_COMPACT_THRESHOLD", "0.5")
    monkeypatch.setenv("GUARDIAN_KEEP_RECENT_MESSAGES", "2")
    monkeypatch.setenv("GUARDIAN_MIN_SUMMARY_CHARS", "40")
    monkeypatch.delenv("GUARDIAN_RESERVE_OUTPUT", raising=False)
    monkeypatch.setenv("GUARDIAN_LOG_PATH", str(tmp_path / "guardian_log.json"))
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", str(tmp_path / "spans"))
    if "context_guardian" in sys.modules:
        return importlib.reload(sys.modules["context_guardian"])
    return importlib.import_module("context_guardian")


class _Resp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Client:
    """Records every summarisation call; anything else (window detection, token counting) is refused."""

    def __init__(self, fail_on=None, anthropic=False):
        self.prompts, self.fail_on, self.anthropic = [], fail_on, anthropic

    async def post(self, url, json=None, timeout=None, headers=None):
        if not (url.endswith("/chat/completions") or url.endswith("/messages")):
            raise RuntimeError("not a summarisation call: %s" % url)
        self.prompts.append(json["messages"][0]["content"])
        n = len(self.prompts)
        text = "" if n == self.fail_on else "ROLLING-SUMMARY-%d keeps what mattered so far, briefly." % n
        if url.endswith("/messages"):
            return _Resp({"content": [{"type": "text", "text": text}]})
        return _Resp({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})

    async def get(self, *a, **k):
        raise RuntimeError("no detection in this test")


def _toks(g, prompt):
    return g.estimate_tokens([{"role": "user", "content": prompt}])


def _inside(prompt):
    return prompt[prompt.rindex("BEGIN_TRANSCRIPT"):prompt.rindex("END_TRANSCRIPT")]


def _msgs(n, size):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": ("MSG-%03d " % i) + ("w%d " % i) * (size // 4)} for i in range(n)]


def test_budget_arithmetic(g):
    assert g.summary_input_budget(32768, reserve=8192) == 24576
    assert g.summary_input_budget(8192, reserve=8192) == 4096     # reserve >= window: clamped to half
    assert g.summary_input_budget(1000, reserve=100) == 900
    assert g.summary_input_budget(1000) == 500                     # default RESERVE_OUTPUT 8192 > 1000


@pytest.mark.asyncio
async def test_no_budget_is_byte_for_byte_today(g):
    older = _msgs(30, 400)
    c = _Client()
    out = await g.summarize_older_messages(c, "m", older)
    transcript = "\n\n".join(g._fence_safe(g.render_for_summary(m)) for m in older)
    assert len(c.prompts) == 1 and out.startswith("ROLLING-SUMMARY-1")
    assert c.prompts[0] == g.CONDENSE_PROMPT + transcript + g.FENCE_SUFFIX


@pytest.mark.asyncio
async def test_a_prompt_that_fits_is_one_unchanged_call(g):
    older = _msgs(3, 200)
    a, b = _Client(), _Client()
    await g.summarize_older_messages(a, "m", older)
    await g.summarize_older_messages(b, "m", older, budget_tokens=100000)
    assert len(b.prompts) == 1 and b.prompts == a.prompts


@pytest.mark.asyncio
async def test_oversized_range_is_summarised_in_calls_that_each_fit(g):
    older = _msgs(40, 400)
    budget = 1500
    c = _Client()
    out = await g.summarize_older_messages(c, "m", older, budget_tokens=budget)
    assert len(c.prompts) >= 2
    assert all(_toks(g, p) <= budget for p in c.prompts), [_toks(g, p) for p in c.prompts]
    assert out.startswith("ROLLING-SUMMARY-%d" % len(c.prompts)), "the result is the LAST call's rolling summary"
    for i in range(1, len(c.prompts)):
        assert "ROLLING-SUMMARY-%d" % i in _inside(c.prompts[i]), "call %d does not carry the summary so far" % (i + 1)
    seen = [re.findall(r"MSG-\d{3}", _inside(p)) for p in c.prompts]
    flat = [m for s in seen for m in s]
    assert sorted(flat) == ["MSG-%03d" % i for i in range(40)], "a message was dropped or sent twice"


@pytest.mark.asyncio
async def test_a_single_giant_message_is_cut_to_fit_keeping_both_ends(g):
    older = [{"role": "user", "content": "HEAD-MARK " + "z" * 20000 + " TAIL-MARK"}]
    budget = 500
    c = _Client()
    out = await g.summarize_older_messages(c, "m", older, budget_tokens=budget)
    assert out is not None
    assert all(_toks(g, p) <= budget for p in c.prompts), [_toks(g, p) for p in c.prompts]
    body = "".join(_inside(p) for p in c.prompts)
    assert "HEAD-MARK" in body and "TAIL-MARK" in body
    assert re.search(r"\[\.\.\. \d+ chars cut for the summary call \.\.\.\]", body)


@pytest.mark.asyncio
async def test_any_failed_call_fails_open(g):
    c = _Client(fail_on=2)
    out = await g.summarize_older_messages(c, "m", _msgs(40, 400), budget_tokens=1500)
    assert out is None


@pytest.mark.asyncio
async def test_maybe_compact_never_sends_a_prompt_larger_than_the_window(g):
    c = _Client()
    payload = {"model": "test-model", "messages": _msgs(6, 600)}
    result = await g.maybe_compact(client=c, payload=payload)
    budget = g.summary_input_budget(1000)
    assert len(c.prompts) >= 2, "the evicted range does not fit one call at this window, so it must be split"
    assert all(_toks(g, p) <= budget for p in c.prompts), [_toks(g, p) for p in c.prompts]
    assert g._state["compactions_performed"] == 1
    assert "ROLLING-SUMMARY-%d" % len(c.prompts) in json.dumps(result["messages"])


@pytest.mark.asyncio
async def test_anthropic_route_fits_too(g):
    c = _Client(anthropic=True)
    payload = {"model": "test-model", "max_tokens": 64, "messages": _msgs(6, 600)}
    await g.maybe_compact_anthropic(c, payload, {})
    budget = g.summary_input_budget(1000)
    assert len(c.prompts) >= 2
    assert all(_toks(g, p) <= budget for p in c.prompts), [_toks(g, p) for p in c.prompts]
