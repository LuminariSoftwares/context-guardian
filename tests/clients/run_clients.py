"""Run one scripted, over-budget conversation through each real client, behind its own context-guardian proxy.

    python tests/clients/run_clients.py [--only NAME]... [--bin NAME=PATH]... [--num-ctx N] [--turns FILE] [--keep]

Clients (default binary): aider (aider), opencode (opencode), continue (cn), claude-code (claude). A binary that is
not found prints `<name> - SKIP not installed`; one ending in .py runs under this interpreter.

Per client: an isolated HOME (HOME, USERPROFILE, APPDATA, LOCALAPPDATA, XDG_*) in a temp work folder; every
GUARDIAN_*/CLAUDE*/ANTHROPIC*/OPENAI*/OPENCODE*/AIDER*/CONTINUE* variable of this shell stripped; its own
context_guardian.py proxy on a free port (upstream = the shared mock + /v1, GUARDIAN_NUM_CTX = --num-ctx, else
scripted_turns.json num_ctx[name], else num_ctx["default"], GUARDIAN_DETECT_CONTEXT=0, GUARDIAN_KEEP_RECENT_MESSAGES
= keep_recent); then the scripted turns in order, turn 2 continuing turn 1's conversation. The argv and config
per client (probed on the real CLIs, 2026-10-31: aider 0.86.2, opencode 1.19.5, cn 1.5.47, claude 2.1.302):

  aider        --model openai/<model> --openai-api-base <proxy>/v1 --openai-api-key mock --no-git --yes-always
               --no-stream --no-pretty --no-check-update --no-show-model-warnings --analytics-disable
               --chat-history-file <project>/.aider.chat.history.md --message <text>  (+ --restore-chat-history, turn 2)
  opencode     run [-c, turn 2] -m cgmock/<model> <text>; <project>/opencode.json holds a cgmock provider
               (npm @ai-sdk/openai-compatible, options.baseURL <proxy>/v1, models {<model>: {}}); cwd and PWD = <project>
  continue     --config <project>/config.yaml -p <text> [--resume, turn 2]; the yaml has one openai model, apiBase
               <proxy>/v1, apiKey mock, roles [chat]
  claude-code  [-c, turn 2] -p <text>, with ANTHROPIC_BASE_URL=<proxy> and ANTHROPIC_API_KEY set

Checks, from the proxy's /guardian/stats after the turns: requests_seen >= 2 ("no request reached the proxy");
compactions_performed + oversized_spills >= 1 ("no compaction"); the last turn's output holds the answer marker
("the answer was not displayed"); a turn that exits non-zero adds "turn <n> exit <code>".

Output: `context-guardian client check (<platform>)`, one line per client -- `<name> <version> PASS`,
`<name> <version> FAIL <reason>[; <reason>]` or `<name> - SKIP not installed` -- then `<p> passed, <f> failed,
<s> skipped` (and, with --keep, `work folder: <path>`). Exit 1 on any FAIL, else 0. Stdlib only.
"""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CLIENTS = {"aider": "aider", "opencode": "opencode", "continue": "cn", "claude-code": "claude"}
STRIP_PREFIXES = ("GUARDIAN_", "CLAUDE", "ANTHROPIC", "OPENAI", "OPENCODE", "AIDER", "CONTINUE")
TURN_TIMEOUT = 600
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, timeout=5, body=None):
    """GET (or POST `body`) a JSON document, never through an HTTP proxy from the environment."""
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with _NO_PROXY.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env():
    """This shell's environment minus everything a client or the proxy would read as configuration."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES) and not k.startswith("XDG_")}
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    return env


def home_env(home):
    """clean_env() with every home-ish variable pointed inside `home`."""
    env = clean_env()
    home = Path(home)
    dirs = {"APPDATA": home / "AppData" / "Roaming", "LOCALAPPDATA": home / "AppData" / "Local",
            "XDG_CONFIG_HOME": home / ".config", "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_CACHE_HOME": home / ".cache", "XDG_STATE_HOME": home / ".local" / "state"}
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    env.update({k: str(v) for k, v in dirs.items()})
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


def start_proxy(workdir, port, upstream, extra, log_name="proxy.log"):
    """Launch context_guardian.py on `port`; return (process, base url) once /guardian/stats answers, else raise."""
    workdir = Path(workdir)
    env = home_env(workdir / "proxy_home")
    env.update({"GUARDIAN_PORT": str(port), "GUARDIAN_HOST": "127.0.0.1", "GUARDIAN_UPSTREAM_URL": upstream,
                "GUARDIAN_DATA_DIR": str(workdir / "guardian"), "GUARDIAN_SPAN_DIR": str(workdir / "guardian" / "spans"),
                "GUARDIAN_LOG_PATH": str(workdir / "guardian" / "log.json"),
                "GUARDIAN_RECEIPTS_PATH": str(workdir / "guardian" / "receipts.jsonl"),
                "GUARDIAN_VERSION_CACHE": str(workdir / "guardian" / "version_cache.json"),
                "GUARDIAN_VERSION_CHECK": "0"})
    env.update({k: str(v) for k, v in extra.items()})
    logfh = open(workdir / log_name, "wb")
    proc = subprocess.Popen([sys.executable, str(ROOT / "context_guardian.py")], env=env, cwd=str(workdir),
                            stdin=subprocess.DEVNULL, stdout=logfh, stderr=subprocess.STDOUT)
    logfh.close()
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("proxy exited with %s (see %s)" % (proc.returncode, workdir / log_name))
        try:
            http_json(base + "/guardian/stats", timeout=2)
            return proc, base
        except OSError:
            time.sleep(0.2)
    stop(proc)
    raise RuntimeError("proxy did not answer within 60s (see %s)" % (workdir / log_name))


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def start_mock(workdir):
    """Start mock_upstream.py on a free port; return (process, port)."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(HERE / "mock_upstream.py"), "--port", str(port),
                             "--log", str(Path(workdir) / "mock.jsonl")],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    if "MOCK READY" not in line:
        stop(proc)
        raise RuntimeError("mock upstream did not start: %r" % line)
    return proc, port


def resolve(binary):
    """argv prefix for a binary spec, or None when it is not there. A .py runs under this interpreter."""
    if binary.lower().endswith(".py"):
        return [sys.executable, binary] if os.path.isfile(binary) else None
    found = shutil.which(binary)
    return [found] if found else None


def first_version_word(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for word in (lines[-1].split() if lines else []):
        if any(c.isdigit() for c in word):
            return word.strip("(),;")
    return "unknown"


class Case:
    """One client: its folders, proxy port, environment and command lines."""

    def __init__(self, name, prefix, work, turns, num_ctx, proxy_port):
        self.name, self.prefix, self.turns = name, prefix, turns
        self.dir = Path(work) / name
        self.home, self.project = self.dir / "home", self.dir / "project"
        self.home.mkdir(parents=True)
        self.project.mkdir()
        self.model = turns["model"]
        self.num_ctx = num_ctx if num_ctx else turns.get("num_ctx", {}).get(name, turns["num_ctx"]["default"])
        self.port = proxy_port
        self.base = "http://127.0.0.1:%d" % proxy_port
        self.env = home_env(self.home)
        self.env["PWD"] = str(self.project)
        if name == "claude-code":
            self.env.update({"ANTHROPIC_BASE_URL": self.base, "ANTHROPIC_API_KEY": "mock"})
        elif name == "opencode":
            (self.project / "opencode.json").write_text(json.dumps({
                "provider": {"cgmock": {"npm": "@ai-sdk/openai-compatible", "name": "cgmock",
                                        "options": {"baseURL": self.base + "/v1"}, "models": {self.model: {}}}},
                "model": "cgmock/" + self.model}, indent=1), encoding="utf-8")
        elif name == "continue":
            (self.project / "config.yaml").write_text(
                "name: cgmock\nversion: 1.0.0\nschema: v1\nmodels:\n  - name: cgmock\n    provider: openai\n"
                "    model: %s\n    apiBase: %s/v1\n    apiKey: mock\n    roles: [chat]\n" % (self.model, self.base),
                encoding="utf-8")

    def version(self):
        try:
            p = subprocess.run(self.prefix + ["--version"], capture_output=True, text=True, timeout=60,
                               env=self.env, cwd=str(self.project), stdin=subprocess.DEVNULL)
            return first_version_word((p.stdout or "") + "\n" + (p.stderr or ""))
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def command(self, n, text):
        second = n > 1
        if self.name == "aider":
            cmd = ["--model", "openai/" + self.model, "--openai-api-base", self.base + "/v1", "--openai-api-key", "mock",
                   "--no-git", "--yes-always", "--no-stream", "--no-pretty", "--no-check-update",
                   "--no-show-model-warnings", "--analytics-disable",
                   "--chat-history-file", str(self.project / ".aider.chat.history.md"), "--message", text]
            if second:
                cmd.append("--restore-chat-history")
        elif self.name == "opencode":
            cmd = ["run"] + (["-c"] if second else []) + ["-m", "cgmock/" + self.model, text]
        elif self.name == "continue":
            cmd = ["--config", str(self.project / "config.yaml"), "-p", text] + (["--resume"] if second else [])
        else:
            cmd = (["-c"] if second else []) + ["-p", text]
        return self.prefix + cmd


def check_one(case, mock_port):
    """Run the proxy and the turns for one client; return (version, [failure reasons])."""
    version = case.version()
    proc = None
    try:
        proc, _ = start_proxy(case.dir, case.port, "http://127.0.0.1:%d/v1" % mock_port,
                              {"GUARDIAN_NUM_CTX": case.num_ctx, "GUARDIAN_DETECT_CONTEXT": "0",
                               "GUARDIAN_KEEP_RECENT_MESSAGES": case.turns["keep_recent"]})
    except RuntimeError as exc:
        return version, [str(exc)]
    reasons, output = [], ""
    try:
        for n, text in enumerate(case.turns["turns"], 1):
            try:
                p = subprocess.run(case.command(n, text), capture_output=True, text=True, errors="replace",
                                   timeout=TURN_TIMEOUT, env=case.env, cwd=str(case.project), stdin=subprocess.DEVNULL)
                output, code = (p.stdout or "") + "\n" + (p.stderr or ""), p.returncode
            except subprocess.TimeoutExpired:
                output, code = "", "timeout"
            except OSError as exc:
                output, code = "", "launch error: %s" % exc
            (case.dir / ("turn%d.out" % n)).write_text(output, encoding="utf-8")
            if code != 0:
                reasons.append("turn %d exit %s" % (n, code))
        try:
            stats = http_json(case.base + "/guardian/stats")
        except OSError as exc:
            return version, ["could not read /guardian/stats: %s" % exc]
        found = []
        if stats.get("requests_seen", 0) < 2:
            found.append("no request reached the proxy")
        if stats.get("compactions_performed", 0) + stats.get("oversized_spills", 0) < 1:
            found.append("no compaction")
        if case.turns["answer_marker"] not in output:
            found.append("the answer was not displayed")
        return version, found + reasons
    finally:
        stop(proc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="run the scripted over-budget conversation through each real client")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", choices=sorted(CLIENTS))
    ap.add_argument("--bin", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--num-ctx", default="", help="GUARDIAN_NUM_CTX for every client (default: scripted_turns.json)")
    ap.add_argument("--turns", default=str(HERE / "scripted_turns.json"))
    ap.add_argument("--keep", action="store_true", help="keep the work folder and print its path")
    args = ap.parse_args(argv)
    bins = dict(CLIENTS)
    for spec in args.bin:
        name, _, path = spec.partition("=")
        if name not in CLIENTS or not path:
            ap.error("--bin wants NAME=PATH with NAME one of %s" % ", ".join(sorted(CLIENTS)))
        bins[name] = path
    turns = json.loads(Path(args.turns).read_text(encoding="utf-8"))
    names = [n for n in CLIENTS if not args.only or n in args.only]

    work = tempfile.mkdtemp(prefix="cg-clients-")
    lines, passed, failed, skipped = [], 0, 0, 0
    mock = None
    try:
        for name in names:
            prefix = resolve(bins[name])
            if prefix is None:
                lines.append("%s - SKIP not installed" % name)
                skipped += 1
                continue
            if mock is None:
                try:
                    mock, mock_port = start_mock(work)
                except RuntimeError as exc:
                    print(exc, file=sys.stderr)
                    return 2
            case = Case(name, prefix, work, turns, args.num_ctx, free_port())
            version, reasons = check_one(case, mock_port)
            if reasons:
                lines.append("%s %s FAIL %s" % (name, version, "; ".join(reasons)))
                failed += 1
            else:
                lines.append("%s %s PASS" % (name, version))
                passed += 1
    finally:
        stop(mock)
    print("context-guardian client check (%s, python %s)" % (platform.system() + " " + platform.release(),
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
