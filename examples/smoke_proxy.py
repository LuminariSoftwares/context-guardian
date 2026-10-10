"""Check a running Context Guardian proxy and the backend behind it.

usage: python examples/smoke_proxy.py examples/ollama-proxy/guardian.env [--no-chat]

Standard library only. Reads the same guardian.env the proxy was started with,
then checks, through the proxy:
  1. /guardian/stats answers and reports that file's upstream and window;
  2. /v1/models is forwarded to the backend and lists at least one model;
  3. one short chat completion on the first model comes back (skip with --no-chat).
Prints one ok/FAIL line per check and a count line; exits 1 on any FAIL.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def read_env_file(path):
    out = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def call(url, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__.strip())
        return 2
    cfg = read_env_file(args[0])
    setting = lambda key, default: os.environ.get(key) or cfg.get(key) or default
    host = setting("GUARDIAN_HOST", "127.0.0.1")
    port = setting("GUARDIAN_PORT", "8786")
    upstream = setting("GUARDIAN_UPSTREAM_URL", "http://localhost:11434/v1")
    num_ctx = int(setting("GUARDIAN_NUM_CTX", "32768"))
    base = "http://%s:%s" % (host, port)
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except urllib.error.HTTPError as exc:
            ok, detail = False, "HTTP %s from %s" % (exc.code, exc.url)
        except (urllib.error.URLError, OSError) as exc:
            ok, detail = False, "%s (%s)" % (type(exc).__name__, getattr(exc, "reason", exc))
        results.append(ok)
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  -- " + str(detail)))
        return ok

    models = []

    def stats():
        _, s = call(base + "/guardian/stats")
        good = s.get("upstream") == upstream and s.get("num_ctx") == num_ctx
        return good, "proxy reports upstream=%s num_ctx=%s" % (s.get("upstream"), s.get("num_ctx"))

    def list_models():
        _, m = call(base + "/v1/models")
        models.extend(x.get("id") for x in m.get("data", []) if x.get("id"))
        return bool(models), "the backend at %s lists no models; load or pull one" % upstream

    def chat():
        _, c = call(base + "/v1/chat/completions", {
            "model": models[0], "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}, timeout=300)
        return bool(c.get("choices")), "no choices in the reply"

    if check("proxy_answers_with_this_config", stats):
        if check("backend_reachable_through_proxy", list_models) and "--no-chat" not in argv:
            check("chat_completion_through_proxy", chat)
    passed = sum(results)
    print("smoke_proxy: %d checks, %d passed, %d failed" % (len(results), passed, len(results) - passed))
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
