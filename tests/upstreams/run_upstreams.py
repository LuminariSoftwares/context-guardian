"""One real turn against each installed upstream, behind its own context-guardian proxy.

    python tests/upstreams/run_upstreams.py [--only NAME]... [--url NAME=URL]... [--model NAME=MODEL]... [--keep]

Upstreams and their default URLs: ollama 127.0.0.1:11434, llamacpp :8080, vllm :8000, lmstudio :1234, litellm :4000.
Probe: ollama GET /api/tags (and /api/version for its version), the others GET /v1/models; no answer prints
`<name> - SKIP not installed`. The model is --model NAME=MODEL, else the first one the probe lists.

One proxy per upstream (GUARDIAN_UPSTREAM_URL = url + /v1, GUARDIAN_DETECT_CONTEXT=1, GUARDIAN_COMPACT_THRESHOLD =
scripted_turns.json upstream_threshold, GUARDIAN_KEEP_RECENT_MESSAGES=1), then ONE POST of the file's upstream_turns.
Checks, from /guardian/stats: context_detected non-empty ("window not detected"); compactions_performed >= 1
("no compaction"); count_source one of tokenize|calibrated|estimate ("no count_source").

Output: `context-guardian upstream check (<platform>)`, one line per upstream -- `<name> <version> PASS`,
`<name> <version> FAIL <reason>[; <reason>]` or `<name> - SKIP not installed` -- then `<p> passed, <f> failed,
<s> skipped` (and, with --keep, `work folder: <path>`). Exit 1 on any FAIL, else 0. Stdlib only.
"""
import argparse
import json
import platform
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "clients"))
import run_clients as rc  # noqa: E402  (shared proxy/process helpers)

UPSTREAMS = {"ollama": 11434, "llamacpp": 8080, "vllm": 8000, "lmstudio": 1234, "litellm": 4000}
COUNT_SOURCES = ("tokenize", "calibrated", "estimate")


def probe(name, url):
    """(model, version) of the first model the upstream lists, or None when nothing answers there."""
    try:
        if name == "ollama":
            models = [m.get("name") or m.get("model") for m in rc.http_json(url + "/api/tags", timeout=3).get("models") or []]
        else:
            models = [m.get("id") for m in rc.http_json(url + "/v1/models", timeout=3).get("data") or []]
    except (OSError, ValueError, AttributeError):
        return None
    return (models[0] if models and models[0] else ""), version_of(name, url)


def version_of(name, url):
    """Best effort: what the server says about itself, else 'unknown'."""
    try:
        if name == "ollama":
            return str(rc.http_json(url + "/api/version", timeout=3).get("version") or "unknown")
        if name == "vllm":
            return str(rc.http_json(url + "/version", timeout=3).get("version") or "unknown")
        if name == "litellm":
            return str(rc.http_json(url + "/health/readiness", timeout=3).get("litellm_version") or "unknown")
        if name == "llamacpp":
            return str((rc.http_json(url + "/props", timeout=3).get("build_info")) or "unknown").split()[0]
    except (OSError, ValueError, AttributeError, IndexError):
        pass
    return "unknown"


def check_one(name, url, model, turns, work):
    """Run one proxy and one request against an upstream; return the failure reasons."""
    d = Path(work) / name
    d.mkdir()
    proc = None
    try:
        proc, base = rc.start_proxy(d, rc.free_port(), url + "/v1",
                                    {"GUARDIAN_DETECT_CONTEXT": "1", "GUARDIAN_KEEP_RECENT_MESSAGES": "1",
                                     "GUARDIAN_COMPACT_THRESHOLD": turns["upstream_threshold"]})
    except RuntimeError as exc:
        return [str(exc)]
    try:
        body = {"model": model, "stream": False,
                "messages": [{"role": r, "content": c} for r, c in turns["upstream_turns"]]}
        # A refused or failed reply is reported, and the stats are still read: compaction happens before the forward.
        reasons = []
        try:
            rc.http_json(base + "/v1/chat/completions", timeout=rc.TURN_TIMEOUT, body=body)
        except OSError as exc:
            reasons.append("request failed: %s" % exc)
        stats = rc.http_json(base + "/guardian/stats")
        found = []
        if not stats.get("context_detected"):
            found.append("window not detected")
        if stats.get("compactions_performed", 0) < 1:
            found.append("no compaction")
        if stats.get("count_source") not in COUNT_SOURCES:
            found.append("no count_source")
        return found + reasons
    except (OSError, ValueError) as exc:
        return ["could not read /guardian/stats: %s" % exc]
    finally:
        rc.stop(proc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="one real turn against each installed upstream")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", choices=sorted(UPSTREAMS))
    ap.add_argument("--url", action="append", default=[], metavar="NAME=URL")
    ap.add_argument("--model", action="append", default=[], metavar="NAME=MODEL")
    ap.add_argument("--turns", default=str(HERE.parent / "clients" / "scripted_turns.json"))
    ap.add_argument("--keep", action="store_true", help="keep the work folder and print its path")
    args = ap.parse_args(argv)
    urls = {n: "http://127.0.0.1:%d" % p for n, p in UPSTREAMS.items()}
    models = {}
    for flag, table in (("--url", urls), ("--model", models)):
        for spec in getattr(args, flag[2:]):
            name, _, val = spec.partition("=")
            if name not in UPSTREAMS or not val:
                ap.error("%s wants NAME=VALUE with NAME one of %s" % (flag, ", ".join(sorted(UPSTREAMS))))
            table[name] = val.rstrip("/") if flag == "--url" else val
    turns = json.loads(Path(args.turns).read_text(encoding="utf-8"))
    names = [n for n in UPSTREAMS if not args.only or n in args.only]

    work = tempfile.mkdtemp(prefix="cg-upstreams-")
    lines, passed, failed, skipped = [], 0, 0, 0
    for name in names:
        found = probe(name, urls[name])
        if found is None:
            lines.append("%s - SKIP not installed" % name)
            skipped += 1
            continue
        listed, version = found
        model = models.get(name) or listed
        reasons = check_one(name, urls[name], model, turns, work) if model else ["no model listed (pass --model)"]
        if reasons:
            lines.append("%s %s FAIL %s" % (name, version, "; ".join(reasons)))
            failed += 1
        else:
            lines.append("%s %s PASS" % (name, version))
            passed += 1
    print("context-guardian upstream check (%s, python %s)" % (platform.system() + " " + platform.release(),
                                                                platform.python_version()))
    for ln in lines:
        print(ln)
    print("%d passed, %d failed, %d skipped" % (passed, failed, skipped))
    if args.keep:
        print("work folder: %s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
