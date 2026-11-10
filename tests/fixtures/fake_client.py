"""A stand-in client for the launcher tests. argv: OUT.json [EXIT_CODE] [MODE ...].

Writes what it saw (environment variables, its SIGINT disposition, its arguments, whether a
guardian answers at its base URL) to OUT.json, then exits with EXIT_CODE (default 0).
MODE `sigint`: wait for SIGINT, then ask the proxy again and exit 5. MODE `selfterm`: kill
itself with SIGTERM.
"""
import json
import os
import signal
import sys
import time
import urllib.request

args = [a for a in sys.argv[1:]]
if "--config" in args:                      # `cn --config FILE`: not ours to interpret
    i = args.index("--config")
    del args[i:i + 2]
out = os.environ.get("FAKE_OUT") or args[0]
rest = args[1:] if not os.environ.get("FAKE_OUT") else args
code = int(rest[0]) if rest and rest[0].lstrip("-").isdigit() else 0
modes = rest[1:] if rest and rest[0].lstrip("-").isdigit() else rest
keys = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY", "CLAUDE_CODE_USE_OPENAI",
        "NO_PROXY", "no_proxy")
base = os.environ.get("ANTHROPIC_BASE_URL") or (os.environ.get("OPENAI_BASE_URL") or "").rsplit("/v1", 1)[0]


def alive():
    if not base:
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/guardian/stats", timeout=3) as r:
            return "version" in json.loads(r.read().decode())
    except Exception:
        return False


seen = {"env": {k: os.environ.get(k) for k in keys}, "argv": sys.argv[1:], "guardian_answers": alive(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN, "config_arg": None}
if "--config" in sys.argv:
    path = sys.argv[sys.argv.index("--config") + 1]
    seen["config_arg"] = path
    try:
        seen["config_text"] = open(path, encoding="utf-8").read()
    except OSError:
        seen["config_text"] = None
if "sigint" in modes:
    got = []
    signal.signal(signal.SIGINT, lambda s, f: got.append(1))
    open(out + ".ready", "w").close()
    for _ in range(100):
        if got:
            break
        time.sleep(0.1)
    seen["got_sigint"] = bool(got)
    seen["guardian_after_sigint"] = alive()
    code = 5
with open(out, "w", encoding="utf-8") as fh:
    json.dump(seen, fh)
if "selfterm" in modes:
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
sys.exit(code)
