#!/usr/bin/env python3
"""context-guardian launch <client> [options] [-- client arguments]

Start the proxy if it is not running, point one client at it for that one process, run the
client, and stop the proxy again if (and only if) this command started it. It writes no user
file: the base URL goes into a copy of the environment handed to the client, and the only
files it ever makes are temporary ones removed at the end (ADR 0010).

    context-guardian launch claude-code -- --resume
    context-guardian launch aider -- --model openai/qwen3-coder
    context-guardian launch continue --model qwen3-coder
    context-guardian launch openai -- some-openai-cli --flag
    context-guardian launch --list

Exit codes: the client's own (128+N if a signal N ended it); 2 for a problem with the launcher
itself or a client that needs a manual setting; 127 when the client's binary is not found.

Standard library only, Python 3.10+. MIT licensed.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_PORT = 8786
READY_TIMEOUT = 60.0
STOP_WAIT = 5.0
LOOPBACK = "127.0.0.1"

# name -> (binary, kind, status, note)
#   kind: "env" (variables), "continue" (a temp config), "generic" (binary is the first
#   argument after --), "steps" (a setting the user must make: print it, change nothing).
CLIENTS: Dict[str, Tuple[str, str, str, str]] = {
    "claude-code": ("claude", "env", "tested variable, launcher run against Claude Code 2.1.322 (see the PR)",
                    "ANTHROPIC_BASE_URL; the proxy's GUARDIAN_UPSTREAM_URL must serve the Anthropic Messages API"),
    "openclaude": ("openclaude", "env", "untested",
                   "CLAUDE_CODE_USE_OPENAI=1, OPENAI_BASE_URL, OPENAI_API_KEY (if unset); set OPENAI_MODEL yourself"),
    "aider": ("aider", "env", "untested",
              "OPENAI_API_BASE, OPENAI_API_KEY (if unset); pass --model openai/<model> yourself"),
    "continue": ("cn", "continue", "untested",
                 "cn --config <temporary file>; needs --model NAME"),
    "openai": ("", "generic", "untested",
               "OPENAI_BASE_URL and OPENAI_API_BASE for any CLI that reads either; the binary follows --"),
    "opencode": ("opencode", "steps", "steps only", "a provider entry in opencode.json"),
    "cline": ("", "steps", "steps only", "Settings -> API Provider in the editor"),
    "cursor": ("", "steps", "steps only (untested in the README too)", "a custom OpenAI base URL in the editor"),
}

STEPS = {
    "opencode": [
        "Add this provider to opencode.json (project folder or ~/.config/opencode/), replacing <model> with the name your backend serves:",
        '  "provider": {"guardian": {"npm": "@ai-sdk/openai-compatible", "name": "Context Guardian", '
        '"options": {"baseURL": "{url}/v1"}, "models": {"<model>": {}}}}',
        "Then start the proxy (context-guardian) and run: opencode run -m guardian/<model> \"<prompt>\"",
    ],
    "cline": [
        "In Cline: Settings -> API Provider: OpenAI Compatible",
        "  Base URL  {url}/v1",
        "  API Key   any non-empty text",
        "  Model ID  the name your backend serves",
        "Start the proxy first (context-guardian).",
    ],
    "cursor": [
        "Cursor is a GUI app and is untested against the proxy. In Settings -> Models, set a custom OpenAI base URL of",
        "  {url}/v1",
        "with any non-empty API key and a model name your backend serves. Start the proxy first (context-guardian).",
    ],
}

USAGE = """usage: context-guardian launch CLIENT [options] [-- client arguments]
       context-guardian launch --list

options:
  --port N                 proxy port (default: GUARDIAN_PORT from the environment or .env, else 8786)
  --url URL                use the proxy already at URL; never start or stop one
  --no-start               do not start a proxy; fail if none answers
  --direct-if-proxy-fails  if the proxy will not start, run the client without it (unprotected)
  --bin PATH               the client's binary (default: looked up on PATH)
  --model NAME             model name (continue)
  --timeout SECONDS        wait this long for the proxy to answer (default 60)
  --quiet                  print nothing of its own
  --list                   list clients and what has been tested
"""


# ---- pure helpers ------------------------------------------------------------------------

def read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = (p.strip() for p in line.strip().partition("="))
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def configured_port(env: Dict[str, str], dotenv_dirs: List[Path]) -> int:
    """GUARDIAN_PORT the way the proxy reads it: the environment first, then each .env
    (load_dotenv with override=False)."""
    candidates = [env.get("GUARDIAN_PORT")]
    for d in dotenv_dirs:
        candidates.append(read_env_file(Path(d) / ".env").get("GUARDIAN_PORT"))
    for c in candidates:
        try:
            port = int(str(c).strip())
        except (TypeError, ValueError):
            continue
        if 0 < port < 65536:
            return port
    return DEFAULT_PORT


def data_dirs(environ: Optional[Dict[str, str]] = None) -> List[Path]:
    """The directories whose .env the proxy would load."""
    out: List[Path] = []
    given = (environ or {}).get("GUARDIAN_DATA_DIR", "").strip()
    try:
        import cg_paths
        out.append(Path(given) if given else Path(cg_paths.data_dir()))
        if cg_paths.PACKAGE_DIR not in out:
            out.append(Path(cg_paths.PACKAGE_DIR))
    except Exception:                                      # noqa: BLE001
        if given:
            out.append(Path(given))
    return out


def _append_no_proxy(env: Dict[str, str]) -> None:
    """Add loopback to NO_PROXY and no_proxy without losing the user's own entries: both spellings
    are merged and the same list is written to both (Windows treats them as one variable)."""
    have: List[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        for part in env.get(key, "").split(","):
            part = part.strip()
            if part and part not in have:
                have.append(part)
    for host in (LOOPBACK, "localhost"):
        if host not in have:
            have.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(have)


def build_env(client: str, base_url: str, parent: Dict[str, str]) -> Dict[str, str]:
    """A copy of `parent` with the client's variables added. Never touches `parent`."""
    env = dict(parent)
    v1 = base_url.rstrip("/") + "/v1"
    if client == "claude-code":
        env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    elif client == "openclaude":
        env["CLAUDE_CODE_USE_OPENAI"] = "1"
        env["OPENAI_BASE_URL"] = v1
    elif client == "aider":
        env["OPENAI_API_BASE"] = v1
    elif client == "openai":
        env["OPENAI_BASE_URL"] = v1
        env["OPENAI_API_BASE"] = v1
    if client in ("openclaude", "aider", "openai") and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = "local"          # a local backend ignores it; the CLI refuses to start without one
    _append_no_proxy(env)
    return env


def warnings_for(client: str, env: Dict[str, str]) -> List[str]:
    if client == "continue":
        return ["cn runs with a temporary config for this run: your own Continue models, rules and MCP servers are not loaded."]
    if client == "claude-code":
        return ["Claude Code talks the Anthropic Messages API: the proxy's GUARDIAN_UPSTREAM_URL must serve it."]
    if client == "openclaude" and not env.get("OPENAI_MODEL"):
        return ["OPENAI_MODEL is not set; OpenClaude needs the model name your backend serves."]
    if client == "aider":
        return ["aider needs --model openai/<model> (pass it after --)."]
    return []


def steps_text(client: str, base_url: str) -> str:
    return "\n".join(line.replace("{url}", base_url.rstrip("/")) for line in STEPS[client])


def continue_config(base_url: str, model: str) -> str:
    return ("name: cgproxy\nversion: 1.0.0\nschema: v1\nmodels:\n  - name: cgproxy\n    provider: openai\n"
            "    model: %s\n    apiBase: %s/v1\n    apiKey: local\n    roles: [chat]\n" % (model, base_url.rstrip("/")))


def list_text() -> str:
    rows = ["%-12s %-34s %s" % ("client", "status", "what is set")]
    for name, (binary, kind, status, note) in CLIENTS.items():
        rows.append("%-12s %-34s %s" % (name, status, note))
    return "\n".join(rows)


def exit_code_for(returncode: Optional[int]) -> int:
    if returncode is None:
        return 2
    return 128 - returncode if returncode < 0 else returncode


def parse_args(argv: List[str]) -> Tuple[Dict, List[str]]:
    """({"client", options...}, client arguments). Raises ValueError with a message."""
    if "--" in argv:
        i = argv.index("--")
        head, rest = argv[:i], argv[i + 1:]
    else:
        head, rest = list(argv), []
    opts: Dict = {"client": None, "port": None, "url": None, "no_start": False, "direct": False, "bin": None,
                  "model": None, "timeout": READY_TIMEOUT, "quiet": False, "list": False}
    it = iter(head)
    for tok in it:
        if tok in ("--port", "--url", "--bin", "--model", "--timeout"):
            try:
                val = next(it)
            except StopIteration:
                raise ValueError("%s needs a value" % tok)
            key = tok[2:]
            if key == "port":
                if not val.isdigit() or not 0 < int(val) < 65536:
                    raise ValueError("--port needs a number from 1 to 65535")
                val = int(val)
            elif key == "timeout":
                try:
                    val = float(val)
                except ValueError:
                    raise ValueError("--timeout needs a number of seconds")
                if not val > 0:
                    raise ValueError("--timeout must be positive")
            opts[key] = val
        elif tok == "--no-start":
            opts["no_start"] = True
        elif tok == "--direct-if-proxy-fails":
            opts["direct"] = True
        elif tok == "--quiet":
            opts["quiet"] = True
        elif tok == "--list":
            opts["list"] = True
        elif tok in ("-h", "--help"):
            opts["help"] = True
        elif tok.startswith("-"):
            raise ValueError("unknown option %s" % tok)
        elif opts["client"] is None:
            opts["client"] = tok
        else:
            raise ValueError("unexpected argument %r (client arguments go after --)" % tok)
    return opts, rest


# ---- the proxy -----------------------------------------------------------------------------

def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))      # never through an HTTP proxy


def is_guardian(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with _opener().open(base_url.rstrip("/") + "/guardian/stats", timeout=timeout) as resp:
            body = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
        return resp.status == 200 and isinstance(body, dict) and "version" in body
    except Exception:                                      # noqa: BLE001
        return False


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def proxy_command() -> List[str]:
    return [sys.executable, str(Path(__file__).resolve().with_name("context_guardian.py"))]


def start_proxy(port: int, env: Dict[str, str]):
    """Start the proxy in its own session/process group. Returns (Popen, log path)."""
    child = dict(env)
    child["GUARDIAN_PORT"] = str(port)
    child["GUARDIAN_HOST"] = LOOPBACK
    handle, log_path = tempfile.mkstemp(prefix="cg-launch-proxy-", suffix=".log")
    kwargs: Dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(proxy_command(), env=child, stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, **kwargs)
    finally:
        os.close(handle)
    return proc, log_path


def stop_proxy(proc: "subprocess.Popen") -> None:
    """Stop a proxy this process started: a polite signal, a wait, then force. Never raises."""
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)              # type: ignore[attr-defined]
            except Exception:                              # noqa: BLE001
                proc.terminate()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=STOP_WAIT)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        proc.wait(timeout=STOP_WAIT)
    except BaseException as exc:                           # noqa: BLE001 -- even a second Ctrl-C must end in a kill
        try:
            proc.kill()
        except Exception:                                  # noqa: BLE001
            pass
        _warn("could not stop the proxy it started cleanly: %s" % exc)


def _warn(text: str) -> None:
    try:
        print("context-guardian: " + text, file=sys.stderr)
    except Exception:                                      # noqa: BLE001
        pass


def wait_ready(base_url: str, proc, timeout: float, ping: Callable[[str], bool] = is_guardian,
               abort: Callable[[], bool] = lambda: False) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if abort() or (proc is not None and proc.poll() is not None):
            return False
        if ping(base_url):
            return True
        time.sleep(0.2)
    return False


def tail(path: str, lines: int = 12) -> str:
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


# ---- running the client -------------------------------------------------------------------

class SignalGuard:
    """Installed for the whole launch, cleanup included. SIGINT gets a no-op FUNCTION, never
    SIG_IGN (an ignored signal is inherited across exec and the client would lose Ctrl-C-to-cancel;
    a function handler is reset to the default in the child). SIGTERM / SIGHUP / SIGBREAK are
    forwarded to the client while it runs. A signal that arrives before the client starts is
    remembered, so a launch that is still waiting for the proxy can stop."""

    def __init__(self) -> None:
        self.client = None
        self.signalled: Optional[int] = None
        self._saved: Dict[int, object] = {}

    def _handler(self, signum, frame):                     # noqa: ARG002
        if self.client is None:
            self.signalled = signum
            return
        try:
            if signum != getattr(signal, "SIGINT", None) and self.client.poll() is None:
                self.client.send_signal(signum)            # the terminal already sent SIGINT to the client
        except Exception:                                  # noqa: BLE001
            pass

    def __enter__(self) -> "SignalGuard":
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._saved[sig] = signal.signal(sig, self._handler)
            except (ValueError, OSError):                  # not the main thread, or unsupported here
                pass
        return self

    def __exit__(self, *exc) -> None:
        for sig, old in self._saved.items():
            try:
                signal.signal(sig, old)
            except Exception:                              # noqa: BLE001
                pass


def run_client(cmd: List[str], env: Dict[str, str], guard: Optional[SignalGuard] = None) -> int:
    """Run the client with the terminal; return its exit code (128+N for a signal)."""
    proc = subprocess.Popen(cmd, env=env)
    if guard is not None:
        guard.client = proc
        if guard.signalled is not None and guard.signalled != getattr(signal, "SIGINT", None):
            try:
                proc.send_signal(guard.signalled)          # arrived between Popen and now: pass it on
            except Exception:                              # noqa: BLE001
                pass
    while True:
        try:
            return exit_code_for(proc.wait())
        except KeyboardInterrupt:
            continue


def _is_path(name: str) -> bool:
    return os.sep in name or bool(os.altsep and os.altsep in name)


def resolve_binary(name: str, override: Optional[str]) -> Optional[str]:
    if override:
        return override if (os.path.isfile(override) or shutil.which(override)) else None
    if name and _is_path(name) and os.path.isfile(name):
        return name
    return shutil.which(name) if name else None


# ---- the command ----------------------------------------------------------------------------

def launch(argv: List[str], environ: Optional[Dict[str, str]] = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    try:
        opts, rest = parse_args(argv)
    except ValueError as exc:
        print("context-guardian launch: %s\n\n%s" % (exc, USAGE), file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0
    if opts["list"]:
        print(list_text())
        return 0
    client = opts["client"]
    if opts["url"] and opts["port"]:
        print("context-guardian launch: --url and --port both name the proxy; use one", file=sys.stderr)
        return 2
    if opts["model"] and client != "continue":
        print("context-guardian launch: --model is only used by `continue`; pass the model to %s after --" % client,
              file=sys.stderr)
        return 2
    if client not in CLIENTS:
        print("context-guardian launch: name a client: %s\n\n%s" % (", ".join(CLIENTS), USAGE), file=sys.stderr)
        return 2
    binary, kind, status, _note = CLIENTS[client]
    quiet = opts["quiet"]

    def say(text: str) -> None:
        if not quiet:
            _warn(text)

    # -- which proxy
    if opts["url"]:
        base_url, manage = opts["url"].rstrip("/"), False
    else:
        port = opts["port"] or configured_port(environ, data_dirs(environ))
        base_url, manage = "http://%s:%d" % (LOOPBACK, port), True

    if kind == "steps":
        print("%s needs a setting in its own configuration; nothing was changed and the client was not started.\n"
              % client)
        print(steps_text(client, base_url))
        return 2

    if kind == "generic":
        if not rest:
            print("context-guardian launch openai: put the client after --, e.g. "
                  "context-guardian launch openai -- some-cli --flag", file=sys.stderr)
            return 2
        binary, rest = rest[0], rest[1:]
    if kind == "continue" and not opts["model"]:
        print("context-guardian launch continue: --model NAME is required", file=sys.stderr)
        return 2
    exe = resolve_binary(binary, opts["bin"])
    if exe is None:
        print("context-guardian launch: %s was not found on PATH (use --bin PATH)" % (opts["bin"] or binary),
              file=sys.stderr)
        return 127

    proc, log_path, temp_files = None, None, []
    code = 2
    # The guard encloses the cleanup too: a SIGTERM/SIGHUP/second Ctrl-C while the proxy is being stopped
    # must neither replace the client's exit code nor leave the proxy running.
    with SignalGuard() as guard:
        try:
            def interrupted() -> bool:
                return guard.signalled is not None

            def signalled_code() -> int:
                return 128 + int(guard.signalled or 2)

            if is_guardian(base_url):
                say("using the proxy already running at %s" % base_url)
            elif opts["no_start"] or not manage:
                print("context-guardian launch: no proxy answers at %s%s" %
                      (base_url, "" if manage else " (--url is never started for you)"), file=sys.stderr)
                return 2
            else:
                port = int(base_url.rsplit(":", 1)[1])
                if not port_is_free(port):
                    port = free_port()
                    base_url = "http://%s:%d" % (LOOPBACK, port)
                    say("the configured port is taken by something else; using %d" % port)
                proc, log_path = start_proxy(port, environ)
                if not wait_ready(base_url, proc, opts["timeout"], abort=interrupted):
                    if interrupted():
                        return signalled_code()
                    reason = tail(log_path)
                    stop_proxy(proc)
                    proc = None
                    if opts["direct"]:
                        say("the proxy did not start; running %s WITHOUT it (--direct-if-proxy-fails)" % client)
                        return _run(exe, rest, environ, kind, opts, None, temp_files, guard)
                    print("context-guardian launch: the proxy did not become ready at %s.\n%s\n"
                          "The client was not started (it would run without the proxy). To run it directly: %s"
                          % (base_url, reason, " ".join([binary] + rest)), file=sys.stderr)
                    return 2
                say("started a proxy at %s; it stops when %s exits" % (base_url, client))
            if interrupted():
                return signalled_code()
            env = build_env(client, base_url, environ)
            for w in warnings_for(client, env):
                say(w)
            say("%s [%s]" % (client, status))
            code = _run(exe, rest, env, kind, opts, base_url, temp_files, guard)
            return code
        except BaseException as exc:                           # noqa: BLE001 -- the launcher never leaves a traceback (SystemExit becomes exit 2)
            if isinstance(exc, KeyboardInterrupt):
                return 130
            _warn("%s: %s" % (type(exc).__name__, exc))
            return 2
        finally:
            # Cleanup must never replace the client's exit code or raise past this point.
            try:
                if proc is not None:
                    stop_proxy(proc)
            except BaseException as exc:                       # noqa: BLE001
                _warn("cleanup failed: %s" % exc)
            for path in [log_path] + temp_files:
                if path:
                    try:
                        os.unlink(path)
                    except BaseException:                      # noqa: BLE001
                        pass


def _run(exe: str, rest: List[str], env: Dict[str, str], kind: str, opts: Dict, base_url: Optional[str],
         temp_files: List[str], guard: Optional[SignalGuard] = None) -> int:
    cmd = ([sys.executable, exe] if exe.lower().endswith(".py") else [exe]) + rest
    if kind == "continue" and base_url:
        handle, path = tempfile.mkstemp(prefix="cg-launch-continue-", suffix=".yaml")
        temp_files.append(path)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(continue_config(base_url, opts["model"]))
        cmd = ([sys.executable, exe] if exe.lower().endswith(".py") else [exe]) + ["--config", path] + rest
    return run_client(cmd, env, guard)


def main(argv: Optional[List[str]] = None) -> int:
    return launch(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
