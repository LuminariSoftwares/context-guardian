"""Probe: `context-guardian launch <client>` from the behaviour description.

    python tests/probes/probe_launch.py [REPO_ROOT]

With no proxy listening, run a stand-in client through `context-guardian launch`. The client must
find a guardian answering at the base URL it was given, be able to send a request through it, and
once the launcher has returned nothing may listen on that port any more; the parent environment and
a temporary home folder must be untouched. On a tree without the feature the command starts a proxy
server instead of launching anything: the probe times out and exits 1.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
tmp = Path(tempfile.mkdtemp(prefix="cg-probe-launch-"))
client = tmp / "client.py"
client.write_text('''
import json, os, sys, urllib.request
base = os.environ.get("ANTHROPIC_BASE_URL", "")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
res = {"base": base}
try:
    res["stats_has_version"] = "version" in json.loads(op.open(base + "/guardian/stats", timeout=5).read())
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode(), headers={"content-type": "application/json"})
    try:
        op.open(req, timeout=10)
    except Exception:
        pass                      # the upstream is not there: only the proxy's own counter matters
    res["requests_seen"] = json.loads(op.open(base + "/guardian/stats", timeout=5).read()).get("requests_seen")
except Exception as exc:
    res["error"] = repr(exc)
open(sys.argv[1], "w").write(json.dumps(res))
''', encoding="utf-8")
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
home = tmp / "home"
home.mkdir()
env = {k: v for k, v in os.environ.items() if not k.startswith(("GUARDIAN_", "ANTHROPIC_", "OPENAI_"))}
env.update({"HOME": str(home), "USERPROFILE": str(home), "GUARDIAN_PORT": str(port), "GUARDIAN_HOST": "127.0.0.1",
            "GUARDIAN_DATA_DIR": str(tmp / "data"), "GUARDIAN_SPAN_DIR": str(tmp / "spans"), "GUARDIAN_LOG_PATH": str(tmp / "log.json"),
            "GUARDIAN_VERSION_CACHE": str(tmp / "v.json"), "GUARDIAN_RECEIPTS_PATH": str(tmp / "r.jsonl"), "GUARDIAN_VERSION_CHECK": "0",
            "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_OLLAMA_CONTEXT": "0", "GUARDIAN_UPSTREAM_URL": "http://127.0.0.1:9/v1"})
out = tmp / "out.json"
failed = 0


def check(name, ok):
    global failed
    print("%-5s %s" % ("ok" if ok else "FAIL", name))
    failed += 0 if ok else 1


def listening():
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


before_home = sorted(p.name for p in home.rglob("*"))
proc = subprocess.Popen([sys.executable, str(root / "context_guardian.py"), "launch", "claude-code", "--bin", str(client), "--quiet", "--", str(out)],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
try:
    code = proc.wait(timeout=90)
except subprocess.TimeoutExpired:
    code = None
    try:
        os.killpg(proc.pid, 9)
    except OSError:
        proc.kill()
    proc.wait()
check("the launcher returned (it did not just start a proxy and sit there)", code == 0)
res = json.loads(out.read_text()) if out.exists() else {}
check("the client was given this proxy's address", res.get("base") == "http://127.0.0.1:%d" % port)
check("a guardian answered at that address while the client ran", res.get("stats_has_version") is True)
check("a request the client sent went through the proxy", (res.get("requests_seen") or 0) >= 1)
time.sleep(0.5)
check("nothing listens on the port afterwards", not listening())
check("the temporary home folder is untouched", sorted(p.name for p in home.rglob("*")) == before_home)
print("probe_launch: %d failed" % failed)
sys.exit(1 if failed else 0)
