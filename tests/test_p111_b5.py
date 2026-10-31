"""P111 S4 (B5) acceptance -- the works-with runners, on fake clients and a fake upstream. Written by O5; DO NOT EDIT.
(tests/clients/scripted_turns.json is O5's too: the runners read it, nobody edits it.)

Why: README bullet (10) says "Client compatibility is uneven" and only Ollama and DSH are tested. B5 runs one scripted,
over-budget conversation through each real client and one real turn against each installed upstream, and the README's
two Works-with tables become those runs' lines. These tests pin the runners' LOGIC with stand-ins that read their
configuration exactly the way the real CLIs do (probed on the maintainer's Windows PC 2026-10-31: aider 0.86.2,
opencode 1.19.5, cn 1.5.47, claude 2.1.302); the live runs happen on that PC.

Contract (plan P111 decision 6 + C5; pinned by O5 2026-10-31):
- tests/clients/mock_upstream.py: a stdlib scripted model on 127.0.0.1 (`--port`, `--log`, prints `MOCK READY port=<p>`):
  OpenAI GET /v1/models + POST /v1/chat/completions (plain and stream) and Anthropic POST /v1/messages (plain and
  stream); a request whose text holds BEGIN_TRANSCRIPT gets a summary, any other gets "CG_OK <n> messages".
- tests/clients/run_clients.py [--only NAME]... [--bin NAME=PATH]... [--num-ctx N] [--turns FILE] [--keep]:
  CLIENTS aider, opencode, continue, claude-code (default binaries aider, opencode, cn, claude). A binary that is not
  found is `<name> - SKIP not installed`. A binary ending in .py runs under sys.executable.
  For each client: an isolated HOME (HOME, USERPROFILE, APPDATA, LOCALAPPDATA, XDG_*) under a temp work folder, env vars
  starting GUARDIAN_/CLAUDE/ANTHROPIC/OPENAI/OPENCODE/AIDER/CONTINUE stripped, its own context_guardian.py proxy on a
  free port (GUARDIAN_UPSTREAM_URL = the mock + "/v1", GUARDIAN_NUM_CTX = --num-ctx or scripted_turns num_ctx[name] or
  num_ctx["default"], GUARDIAN_DETECT_CONTEXT=0, GUARDIAN_KEEP_RECENT_MESSAGES = keep_recent), then the scripted
  turns in order, turn 2 continuing turn 1's conversation:
    aider        --model openai/<model> --openai-api-base <proxy>/v1 --openai-api-key mock --no-git --yes-always
                 --no-stream --no-pretty --no-check-update --no-show-model-warnings --analytics-disable
                 --chat-history-file <project>/.aider.chat.history.md --message <text>   (+ --restore-chat-history on 2)
    opencode     run [-c on 2] -m cgmock/<model> <text>, with <project>/opencode.json = {"provider": {"cgmock": {"npm":
                 "@ai-sdk/openai-compatible", "name": "cgmock", "options": {"baseURL": "<proxy>/v1"}, "models":
                 {"<model>": {}}}}, "model": "cgmock/<model>"}, cwd and PWD = the project folder
    continue     --config <project>/config.yaml -p <text> (+ --resume on 2); the yaml has one model with provider: openai,
                 model, apiBase: <proxy>/v1, apiKey: mock, roles: [chat]
    claude-code  [-c on 2] -p <text>, with ANTHROPIC_BASE_URL=<proxy> and ANTHROPIC_API_KEY set
  Checks, from the proxy's /guardian/stats after the turns: requests_seen >= 2 (else "no request reached the proxy");
  compactions_performed + oversized_spills >= 1 (else "no compaction"); the last turn's output holds answer_marker
  (else "the answer was not displayed"); a turn that exits non-zero adds "turn <n> exit <code>".
  Output: `context-guardian client check (<platform>)`, then one line per client `<name> <version> PASS`,
  `<name> <version> FAIL <reason>[; <reason>]` or `<name> - SKIP not installed` (<version> = the first word holding a
  digit in the last line of `--version`), then `<p> passed, <f> failed, <s> skipped`; exit 1 when any FAIL, else 0.
- tests/upstreams/run_upstreams.py [--only NAME]... [--url NAME=URL]... [--model NAME=MODEL]... [--keep]:
  UPSTREAMS ollama, llamacpp, vllm, lmstudio, litellm with default URLs on 127.0.0.1 (11434, 8080, 8000, 1234, 4000).
  Probe: ollama GET /api/tags (+ /api/version), the others GET /v1/models; no answer -> `<name> - SKIP not installed`.
  Model: --model, else the first listed. One proxy per upstream (GUARDIAN_UPSTREAM_URL = url + "/v1",
  GUARDIAN_DETECT_CONTEXT=1, GUARDIAN_COMPACT_THRESHOLD = upstream_threshold, GUARDIAN_KEEP_RECENT_MESSAGES=1) and ONE
  POST of scripted_turns upstream_turns. Checks: stats context_detected non-empty (else "window not detected");
  compactions_performed >= 1 (else "no compaction"); count_source in tokenize|calibrated|estimate (else
  "no count_source"). Same line and summary format, header `context-guardian upstream check (<platform>)`.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLIENTS_PY = ROOT / "tests" / "clients" / "run_clients.py"
UPSTREAMS_PY = ROOT / "tests" / "upstreams" / "run_upstreams.py"
TURNS = json.loads((ROOT / "tests" / "clients" / "scripted_turns.json").read_text(encoding="utf-8"))

# One stand-in per CLI. Each reads its configuration where the real one does, keeps its own conversation (so turn 2
# really continues turn 1), posts to the URL it was configured with, and prints the reply text. BREAK (set per test)
# makes it misbehave: "ignore_config" posts nowhere but still prints a CG_OK; "exit3_on_2" fails turn 2.
FAKE = r'''
import json, os, sys, urllib.request
NAME, BREAK = %r, %r
argv = sys.argv[1:]
if argv == ["--version"]:
    print("fake-%%s 9.8.7" %% NAME); sys.exit(0)
home = os.environ["HOME"]
with open(os.path.join(home, "seen_env.json"), "w") as fh:
    json.dump({"HOME": home, "USERPROFILE": os.environ.get("USERPROFILE"), "leak": [k for k in os.environ if k.startswith(("GUARDIAN_", "OPENAI", "ANTHROPIC_API_KEY_REAL"))]}, fh)
def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None
def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
second = False
if NAME == "aider":
    base, model, text, hist = flag("--openai-api-base"), flag("--model"), flag("--message"), flag("--chat-history-file")
    need = ["--no-git", "--yes-always", "--no-stream"]
    if not (base and model and model.startswith("openai/") and text and hist and all(n in argv for n in need)):
        sys.exit("aider: bad argv %%r" %% argv)
    second = "--restore-chat-history" in argv
    store, url, mname = hist + ".json", base + "/chat/completions", model[len("openai/"):]
elif NAME == "opencode":
    if not argv or argv[0] != "run":
        sys.exit("opencode: expected run")
    rest = argv[1:]
    second = "-c" in rest
    rest = [a for a in rest if a != "-c"]
    model, text = rest[rest.index("-m") + 1], rest[-1]
    cfg = json.load(open(os.path.join(os.environ.get("PWD") or os.getcwd(), "opencode.json")))
    prov, mname = model.split("/", 1)
    p = cfg["provider"][prov]
    if p.get("npm") != "@ai-sdk/openai-compatible" or mname not in p.get("models", {}):
        sys.exit("opencode: bad provider %%r" %% p)
    store, url = os.path.join(home, ".oc_session.json"), p["options"]["baseURL"] + "/chat/completions"
elif NAME == "continue":
    cfg, text = flag("--config"), flag("-p")
    second = "--resume" in argv
    y = open(cfg).read()
    kv = dict(l.strip().split(": ", 1) for l in y.splitlines() if ": " in l and l.strip()[0] != "-")
    if kv.get("provider") != "openai" or "roles" not in kv:
        sys.exit("continue: bad yaml")
    store, url, mname = os.path.join(home, ".cn_session.json"), kv["apiBase"] + "/chat/completions", kv["model"]
else:
    second = "-c" in argv
    text = flag("-p")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("claude: no api key")
    store, url, mname = os.path.join(home, ".claude_session.json"), os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", "claude-x"
if BREAK == "exit3_on_2" and second:
    sys.exit(3)
msgs = json.load(open(store)) if second and os.path.exists(store) else []
msgs.append({"role": "user", "content": text})
if BREAK == "ignore_config":
    reply = "CG_OK pretend"
elif NAME == "claude-code":
    reply = post(url, {"model": mname, "max_tokens": 512, "messages": msgs})["content"][0]["text"]
else:
    reply = post(url, {"model": mname, "messages": msgs, "stream": False})["choices"][0]["message"]["content"]
msgs.append({"role": "assistant", "content": reply})
json.dump(msgs, open(store, "w"))
print(reply)
'''


def fakes(tmp_path, breaks=None):
    out = []
    for name in ("aider", "opencode", "continue", "claude-code"):
        p = tmp_path / ("fake_%s.py" % name.replace("-", "_"))
        p.write_text(FAKE % (name, (breaks or {}).get(name, "")), encoding="utf-8")
        out += ["--bin", "%s=%s" % (name, p)]
    return out


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(("GUARDIAN_", "ANTHROPIC", "OPENAI"))}


def clients(*args, timeout=600, extra_env=None):
    p = subprocess.run([sys.executable, str(CLIENTS_PY), *args], capture_output=True, text=True, timeout=timeout,
                       cwd=str(ROOT), env=dict(clean_env(), **(extra_env or {})))
    return p.returncode, p.stdout.strip().splitlines(), p.stderr


def lines_for(out):
    return {ln.split()[0]: ln for ln in out[1:-1]}


# ---- the clients runner -------------------------------------------------------------------------------------------
def test_every_client_passes_through_the_proxy(tmp_path):
    code, out, err = clients(*fakes(tmp_path))
    assert code == 0, out + [err[-1500:]]
    assert out[0].startswith("context-guardian client check (")
    got = lines_for(out)
    for name in ("aider", "opencode", "continue", "claude-code"):
        assert got[name] == "%s 9.8.7 PASS" % name, got
    assert out[-1] == "4 passed, 0 failed, 0 skipped"


def test_a_missing_client_is_a_skip(tmp_path):
    code, out, err = clients("--only", "aider", "--bin", "aider=" + str(tmp_path / "nope.exe"))
    assert code == 0 and lines_for(out)["aider"] == "aider - SKIP not installed" and out[-1] == "0 passed, 0 failed, 1 skipped"


def test_a_client_that_never_reaches_the_proxy_fails(tmp_path):
    code, out, err = clients("--only", "opencode", *fakes(tmp_path, {"opencode": "ignore_config"}))
    assert code == 1
    line = lines_for(out)["opencode"]
    assert line.startswith("opencode 9.8.7 FAIL") and "no request reached the proxy" in line and "no compaction" in line


def test_no_compaction_is_a_failure(tmp_path):
    code, out, err = clients("--only", "continue", "--num-ctx", "1000000", *fakes(tmp_path))
    assert code == 1 and lines_for(out)["continue"] == "continue 9.8.7 FAIL no compaction", out


def test_a_turn_that_exits_non_zero_is_reported(tmp_path):
    code, out, err = clients("--only", "claude-code", *fakes(tmp_path, {"claude-code": "exit3_on_2"}))
    assert code == 1
    line = lines_for(out)["claude-code"]
    assert "turn 2 exit 3" in line and "the answer was not displayed" in line, line


def test_the_client_runs_in_an_isolated_home(tmp_path):
    code, out, err = clients("--only", "aider", "--keep", *fakes(tmp_path),
                             extra_env={"GUARDIAN_NUM_CTX": "999999", "OPENAI_API_BASE": "http://192.0.2.1/v1"})
    work = Path(out[-1].split("work folder: ", 1)[1]) if out[-1].startswith("work folder: ") else None
    assert code == 0 and work is not None, out
    seen = json.loads((work / "aider" / "home" / "seen_env.json").read_text(encoding="utf-8"))
    assert Path(seen["HOME"]).resolve() == (work / "aider" / "home").resolve() and seen["USERPROFILE"] == seen["HOME"]
    assert seen["leak"] == [], "GUARDIAN_*/OPENAI* variables leaked into the client"
    assert (work / "aider" / "project" / ".aider.chat.history.md.json").exists()


# ---- the upstreams runner ------------------------------------------------------------------------------------------
class FakeOllama:
    """/api/tags, /api/version, /api/show (num_ctx 4096 unless show=False) and /v1/chat/completions."""

    def __init__(self, show=True):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send({"models": [{"name": "fake:4b"}]})
                if self.path == "/api/version":
                    return self._send({"version": "0.99.1"})
                self._send({"error": "nf"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/show":
                    if not show:
                        return self._send({"error": "nf"}, 404)
                    return self._send({"parameters": "num_ctx 4096", "model_info": {"llama.context_length": 4096}})
                text = "\n".join(str(m.get("content")) for m in body.get("messages") or [])
                reply = "Summary: the release train leaves from platform 9." if "BEGIN_TRANSCRIPT" in text else "9"
                return self._send({"id": "x", "object": "chat.completion", "model": body.get("model"),
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                                "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 50, "completion_tokens": 1, "total_tokens": 51}})

        class S(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):
                import socketserver
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.srv = S(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def upstreams(*args):
    p = subprocess.run([sys.executable, str(UPSTREAMS_PY), *args], capture_output=True, text=True, timeout=600,
                       cwd=str(ROOT), env=clean_env())
    return p.returncode, p.stdout.strip().splitlines(), p.stderr


def dead_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


@pytest.fixture
def ollama():
    made = []

    def make(**kw):
        f = FakeOllama(**kw)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def test_an_upstream_that_answers_passes_and_the_rest_skip(ollama):
    f = ollama()
    args = ["--url", "ollama=" + f.url]
    for name in ("llamacpp", "vllm", "lmstudio", "litellm"):
        args += ["--url", "%s=%s" % (name, dead_url())]
    code, out, err = upstreams(*args)
    assert code == 0, out + [err[-1500:]]
    assert out[0].startswith("context-guardian upstream check (")
    got = lines_for(out)
    assert got["ollama"] == "ollama 0.99.1 PASS", got
    for name in ("llamacpp", "vllm", "lmstudio", "litellm"):
        assert got[name] == "%s - SKIP not installed" % name
    assert out[-1] == "1 passed, 0 failed, 4 skipped"


def test_an_upstream_whose_window_is_not_detected_fails(ollama):
    f = ollama(show=False)
    code, out, err = upstreams("--only", "ollama", "--url", "ollama=" + f.url, "--model", "ollama=fake:4b")
    assert code == 1
    line = lines_for(out)["ollama"]
    assert line.startswith("ollama 0.99.1 FAIL") and "window not detected" in line, line


def test_the_scripted_turns_file_shape():
    assert TURNS["version"] == 1 and len(TURNS["turns"]) == 2 and TURNS["answer_marker"] == "CG_OK"
    assert len(TURNS["turns"][0]) > TURNS["num_ctx"]["default"] * 3.5, "turn 1 alone must exceed the default window"
    assert TURNS["upstream_turns"][-1][0] == "user" and 0 < TURNS["upstream_threshold"] < 1
