"""P111 S5 (B6) acceptance -- counted savings and llms.txt. Written by the O5 overseer; DO NOT EDIT.

Why: "what does it actually save?" is an outside user's first question. `--receipts` already counts compactions,
tokens saved, summaries and recall reads. What it does not show is the before -> after token totals from the compaction
log (how much smaller the forwarded requests were), the oversized pastes cut by S2, and a machine-readable form. B6 adds
exactly that as `--savings`; it is counted from the log and the receipts, never estimated. Plus llms.txt + the
one-prompt install line, the same shape as tool-guardian's (P110 S4).

Contract (plan P111 decision 7 + C6; scoped by O5 2026-10-31 so it does not repeat --receipts):
- `context-guardian-recall --savings [--since AGE] [--json]` (AGE as --receipts: 30m, 24h, 7d, 2w; a bad AGE exits 2).
- Reads the compaction log (GUARDIAN_LOG_PATH, default <data dir>/logs/context_guardian_log.json, one JSON object per
  line, written by the proxy) and the receipts (as --receipts does). Log lines that are not JSON objects are skipped.
  With --since, a log event whose "timestamp" (local time, YYYY-MM-DDTHH:MM:SS) is older than now - AGE is left out.
- `--json` prints ONE object with exactly these keys:
    window            "all time", or "last <AGE>"
    compactions       number of "compaction" events (never "dry_run")
    tokens_before     sum of their estimated_tokens_before
    tokens_after      sum of their estimated_tokens_after
    tokens_kept_out   sum of their tokens_saved
    pct_smaller       round(100 * (1 - tokens_after / tokens_before), 1); 0.0 when tokens_before is 0
    oversized_spills  number of "oversized_spill" events + the sum of compaction events' oversized_spilled
    summaries_reused  compaction events whose summary_source is "reused"
    recall_calls      recall receipts (tool cli.* or mcp.*) in the window
    chars_returned    sum of their chars_returned
- The text form starts with `Context Guardian savings (<window>)` and shows every number above. With no compaction and
  no oversized spill in the window it prints `no compactions in window` and exits 0.
- `llms.txt` at the repo root: first line `# context-guardian`, a `> ` summary line within the next five lines, then H2
  sections in this order: `## Install`, `## Configure`, `## Recall`, `## Limits`; under 6,000 characters; every link a
  relative path to an existing file (optionally `README.md#<real heading slug>`) or a
  https://github.com/LuminariSoftwares/context-guardian URL to an existing file; `## Limits` links to the README
  "What this does *not* do" heading. llms.txt ships in the npm package ("files").
- The README carries the raw llms.txt URL
  https://raw.githubusercontent.com/LuminariSoftwares/context-guardian/main/llms.txt OUTSIDE the guardian-family block,
  and documents `--savings` (the word `--savings` appears outside that block).
"""
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLMS = ROOT / "llms.txt"
RAW = "https://raw.githubusercontent.com/LuminariSoftwares/context-guardian/main/llms.txt"
REPO = "https://github.com/LuminariSoftwares/context-guardian"
KEYS = {"window", "compactions", "tokens_before", "tokens_after", "tokens_kept_out", "pct_smaller",
        "oversized_spills", "summaries_reused", "recall_calls", "chars_returned"}


def ts(days_ago=0.0):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def iso(days_ago=0.0):
    return (datetime.datetime.now().astimezone() - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


def comp(before, after, saved, source="generated", spilled=0, days_ago=0.0, event="compaction"):
    return {"event": event, "run_id": "r1", "estimated_tokens_before": before, "estimated_tokens_after": after,
            "tokens_saved": saved, "summary_source": source, "oversized_spilled": spilled, "timestamp": ts(days_ago)}


def home(tmp_path, events, receipts=()):
    log = tmp_path / "log.json"
    log.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events), encoding="utf-8")
    rec = tmp_path / "receipts.jsonl"
    rec.write_text("".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    env = dict(os.environ, GUARDIAN_LOG_PATH=str(log), GUARDIAN_RECEIPTS_PATH=str(rec),
               GUARDIAN_SPAN_DIR=str(tmp_path / "spans"), GUARDIAN_DATA_DIR=str(tmp_path / "data"))
    return env


def savings(env, *extra):
    return subprocess.run([sys.executable, str(ROOT / "cg_recall_cli.py"), "--savings", *extra], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(ROOT))


EVENTS = [
    comp(10000, 4000, 6000),
    comp(8000, 3000, 5000, source="reused", spilled=1),
    comp(9000, 1000, 8000, event="dry_run"),
    {"event": "oversized_spill", "run_id": "r1", "oversized_spilled": 1, "timestamp": ts()},
    {"event": "tool_budget", "tool_tokens": 900, "timestamp": ts()},
    "not json at all",
    comp(5000, 2500, 2500, days_ago=10),
]
RECEIPTS = [
    {"ts": iso(), "tool": "mcp.read_span", "spans_opened": 1, "chars_returned": 1200},
    {"ts": iso(), "tool": "cli.search", "spans_matched": 2, "chars_returned": 300},
    {"ts": iso(), "tool": "proxy.compaction", "tokens_saved": 6000},
    {"ts": iso(10), "tool": "mcp.search_spans", "chars_returned": 50},
]


def test_json_all_time(tmp_path):
    r = savings(home(tmp_path, EVENTS, RECEIPTS), "--json")
    assert r.returncode == 0, r.stderr[-600:]
    got = json.loads(r.stdout)
    assert set(got) == KEYS
    assert got == {"window": "all time", "compactions": 3, "tokens_before": 23000, "tokens_after": 9500,
                   "tokens_kept_out": 13500, "pct_smaller": 58.7, "oversized_spills": 2, "summaries_reused": 1,
                   "recall_calls": 3, "chars_returned": 1550}


def test_since_window(tmp_path):
    got = json.loads(savings(home(tmp_path, EVENTS, RECEIPTS), "--since", "7d", "--json").stdout)
    assert got["window"] == "last 7d" and got["compactions"] == 2 and got["tokens_before"] == 18000
    assert got["tokens_after"] == 7000 and got["pct_smaller"] == 61.1 and got["recall_calls"] == 2
    assert got["chars_returned"] == 1500 and got["oversized_spills"] == 2


def test_text_form_shows_every_number(tmp_path):
    r = savings(home(tmp_path, EVENTS, RECEIPTS), "--since", "7d")
    assert r.returncode == 0 and r.stdout.splitlines()[0] == "Context Guardian savings (last 7d)"
    flat = r.stdout.replace(",", "")
    for n in ("18000", "7000", "11000", "61.1", "2"):
        assert n in flat, (n, r.stdout)


def test_empty_window(tmp_path):
    r = savings(home(tmp_path, [comp(5000, 2500, 2500, days_ago=10)]), "--since", "24h")
    assert r.returncode == 0 and "no compactions in window" in r.stdout
    r = savings(home(tmp_path, []))
    assert r.returncode == 0 and "no compactions in window" in r.stdout


def test_bad_age_exits_2(tmp_path):
    r = savings(home(tmp_path, EVENTS), "--since", "soon")
    assert r.returncode == 2 and "unrecognized arguments" not in r.stderr, r.stderr[-300:]


def test_receipts_unchanged(tmp_path):
    env = home(tmp_path, EVENTS, RECEIPTS)
    r = subprocess.run([sys.executable, str(ROOT / "cg_recall_cli.py"), "--receipts"], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(ROOT))
    assert r.returncode == 0 and r.stdout.startswith("Context Guardian receipts (all time)")


# ---- llms.txt ---------------------------------------------------------------------------------------------------
def slug(heading):
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def outside_family(text):
    a, b = text.index("<!-- guardian-family:start -->"), text.index("<!-- guardian-family:end -->")
    return text[:a] + text[b:]


def test_llms_structure():
    assert LLMS.is_file(), "llms.txt is missing"
    t = LLMS.read_text(encoding="utf-8")
    lines = t.splitlines()
    assert lines[0].strip() == "# context-guardian"
    assert any(line.startswith("> ") for line in lines[1:6])
    want = ["## Install", "## Configure", "## Recall", "## Limits"]
    assert [h.strip() for h in lines if h.strip() in want] == want
    assert len(t) < 6000


def test_llms_links_resolve():
    t = LLMS.read_text(encoding="utf-8")
    slugs = {slug(line.lstrip("#")) for line in readme().splitlines() if line.startswith("#")}
    links = re.findall(r"\]\(([^)\s]+)\)", t)
    assert links
    for link in links:
        if link.startswith("http"):
            assert link.startswith(REPO + "/blob/main/") or link == REPO, "foreign host: " + link
            rel, _, anchor = link[len(REPO + "/blob/main/"):].partition("#") if link != REPO else ("", "", "")
        else:
            rel, _, anchor = link.partition("#")
        if rel:
            assert (ROOT / rel).exists(), "no such file: " + rel
        if anchor:
            assert rel == "README.md" and anchor in slugs, "no such README heading: #" + anchor
    limits = t.split("## Limits", 1)[1].split("\n## ", 1)[0]
    assert "#" + slug("What this does *not* do") in limits


def test_readme_and_package():
    rest = outside_family(readme())
    assert RAW in rest and "--savings" in rest
    assert "llms.txt" in json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]
