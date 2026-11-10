"""D. `context-guardian launch <client>` (ADR 0010). Written before the implementation."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cg_launch as L  # noqa: E402

FAKE = str(ROOT / "tests" / "fixtures" / "fake_client.py")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")


def env_for(tmp_path):
    """A clean environment whose home, data and archive live under tmp_path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GUARDIAN_", "ANTHROPIC_", "OPENAI_", "CLAUDE"))}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "AppData"),
                "LOCALAPPDATA": str(home / "Local"), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local"), "XDG_CACHE_HOME": str(home / ".cache"),
                "GUARDIAN_DATA_DIR": str(tmp_path / "data"), "GUARDIAN_SPAN_DIR": str(tmp_path / "spans"),
                "GUARDIAN_LOG_PATH": str(tmp_path / "log.json"), "GUARDIAN_VERSION_CACHE": str(tmp_path / "v.json"),
                "GUARDIAN_RECEIPTS_PATH": str(tmp_path / "r.jsonl"), "GUARDIAN_VERSION_CHECK": "0",
                "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_OLLAMA_CONTEXT": "0", "PYTHONUNBUFFERED": "1"})
    return env


def snapshot(*roots):
    seen = {}
    for r in roots:
        for p in sorted(Path(r).rglob("*")):
            try:
                st = p.stat()
                seen[str(p)] = (st.st_size, st.st_mtime_ns) if p.is_file() else None
            except OSError:
                pass
    return seen


def run(tmp_path, args, extra=None, **kw):
    env = env_for(tmp_path)
    env.update(extra or {})
    return L.launch(args, environ=env, **kw)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def closed(port, wait=10.0):
    """Nothing accepts connections on the port (a TIME_WAIT socket can keep it un-bindable, so connect instead)."""
    deadline = time.time() + wait
    while True:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        if time.time() > deadline:
            return False
        time.sleep(0.2)


# ---- pure ---------------------------------------------------------------------------------

def test_parse_args():
    o, rest = L.parse_args(["claude-code", "--port", "9000", "--quiet", "--", "--resume", "-p", "hi"])
    assert o["client"] == "claude-code" and o["port"] == 9000 and o["quiet"] and rest == ["--resume", "-p", "hi"]
    for bad in (["x", "--port"], ["x", "--port", "abc"], ["x", "--port", "70000"], ["x", "--timeout", "-1"], ["x", "y"], ["--nope"]):
        with pytest.raises(ValueError):
            L.parse_args(bad)
    assert L.parse_args(["--list"])[0]["list"]


def test_configured_port_reads_the_env_then_dotenv(tmp_path):
    (tmp_path / ".env").write_text("GUARDIAN_PORT=9123\n", encoding="utf-8")
    assert L.configured_port({}, [tmp_path]) == 9123
    assert L.configured_port({"GUARDIAN_PORT": "9001"}, [tmp_path]) == 9001
    assert L.configured_port({"GUARDIAN_PORT": "junk"}, [tmp_path]) == 9123
    assert L.configured_port({}, []) == 8786


def test_build_env_per_client_is_a_copy():
    parent = {"PATH": "x", "NO_PROXY": "corp.example"}
    snap = dict(parent)
    e = L.build_env("claude-code", "http://127.0.0.1:8786", parent)
    assert parent == snap
    assert e["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8786" and "OPENAI_BASE_URL" not in e
    assert e["NO_PROXY"].split(",") == ["corp.example", "127.0.0.1", "localhost"]
    e = L.build_env("openclaude", "http://127.0.0.1:8786", parent)
    assert e["OPENAI_BASE_URL"] == "http://127.0.0.1:8786/v1" and e["CLAUDE_CODE_USE_OPENAI"] == "1" and e["OPENAI_API_KEY"] == "local"
    e = L.build_env("aider", "http://127.0.0.1:8786", {"OPENAI_API_KEY": "mine"})
    assert e["OPENAI_API_BASE"] == "http://127.0.0.1:8786/v1" and e["OPENAI_API_KEY"] == "mine"
    e = L.build_env("openai", "http://127.0.0.1:8786/", {})
    assert e["OPENAI_BASE_URL"] == e["OPENAI_API_BASE"] == "http://127.0.0.1:8786/v1"


def test_exit_codes_and_steps_and_list():
    assert L.exit_code_for(0) == 0 and L.exit_code_for(7) == 7 and L.exit_code_for(-15) == 143 and L.exit_code_for(None) == 2
    text = L.steps_text("opencode", "http://127.0.0.1:8786")
    assert "http://127.0.0.1:8786/v1" in text and "opencode.json" in text
    listing = L.list_text()
    for name in ("claude-code", "aider", "continue", "opencode", "openclaude"):
        assert name in listing
    assert "untested" in listing and "steps only" in listing


# ---- end to end with a real proxy and a stand-in client ---------------------------------------

def test_the_variable_reaches_only_the_client_and_the_proxy_is_stopped(tmp_path):
    port = L.free_port()
    out = tmp_path / "out.json"
    before_env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    home_before = snapshot(tmp_path / "home")
    code = run(tmp_path, ["claude-code", "--port", str(port), "--bin", FAKE, "--quiet", "--", str(out)])
    assert code == 0
    seen = read(out)
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % port
    assert seen["guardian_answers"] is True                      # the proxy was up for the client
    assert "127.0.0.1" in seen["env"]["NO_PROXY"]
    assert os.environ.get("ANTHROPIC_BASE_URL") != "http://127.0.0.1:%d" % port       # never exported here
    assert {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"} == before_env
    assert closed(port)                                          # the proxy it started is gone
    assert snapshot(tmp_path / "home") == home_before            # no file under the (temporary) home changed


def test_the_client_keeps_ctrl_c(tmp_path):
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--quiet", "--", str(out)]) == 0
    assert read(out)["sigint_ignored"] is False                  # an ignored SIGINT would be inherited


def test_an_existing_proxy_is_reused_and_left_running(tmp_path):
    env = env_for(tmp_path)
    port = L.free_port()
    proc, log = L.start_proxy(port, env)
    try:
        assert L.wait_ready("http://127.0.0.1:%d" % port, proc, 60)
        out = tmp_path / "o.json"
        assert L.launch(["claude-code", "--port", str(port), "--bin", FAKE, "--quiet", "--", str(out)], environ=env) == 0
        assert read(out)["guardian_answers"] is True
        assert proc.poll() is None and L.is_guardian("http://127.0.0.1:%d" % port)
    finally:
        L.stop_proxy(proc)
        os.unlink(log)


def test_a_port_held_by_something_else_gets_another_port(tmp_path):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        out = tmp_path / "o.json"
        assert run(tmp_path, ["claude-code", "--port", str(port), "--bin", FAKE, "--quiet", "--", str(out)]) == 0
        used = int(read(out)["env"]["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])
        assert used != port and closed(used)
    finally:
        held.close()


def test_client_exit_code_is_returned(tmp_path):
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--quiet", "--", str(out), "7"]) == 7


@posix_only
def test_a_client_killed_by_a_signal_gives_128_plus_n(tmp_path):
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--quiet", "--", str(out), "0", "selfterm"]) == 143


def test_a_missing_client_is_127(tmp_path):
    assert run(tmp_path, ["aider", "--bin", "no-such-binary-xyz", "--quiet"]) == 127


def test_settings_clients_print_steps_and_change_nothing(tmp_path, capsys):
    env_for(tmp_path)
    before = snapshot(tmp_path)
    assert run(tmp_path, ["opencode"]) == 2
    out = capsys.readouterr().out
    assert "nothing was changed" in out and "opencode.json" in out and "/v1" in out
    assert snapshot(tmp_path) == before


def test_continue_needs_a_model_and_uses_a_temporary_config(tmp_path):
    assert run(tmp_path, ["continue", "--bin", FAKE, "--quiet"]) == 2
    out = tmp_path / "o.json"
    assert run(tmp_path, ["continue", "--model", "qwen", "--port", str(L.free_port()), "--bin", FAKE, "--quiet"],
               extra={"FAKE_OUT": str(out)}) == 0
    seen = read(out)
    assert "model: qwen" in seen["config_text"] and "apiBase: http://127.0.0.1:" in seen["config_text"]
    assert not os.path.exists(seen["config_arg"])                # removed at the end


def test_openai_generic_takes_the_binary_after_the_dashes(tmp_path):
    out = tmp_path / "o.json"
    assert run(tmp_path, ["openai", "--port", str(L.free_port()), "--quiet", "--", FAKE, str(out)]) == 0
    e = read(out)["env"]
    assert e["OPENAI_BASE_URL"].endswith("/v1") and e["OPENAI_API_BASE"].endswith("/v1")
    assert run(tmp_path, ["openai", "--quiet"]) == 2


# ---- the proxy will not start ------------------------------------------------------------------

def test_a_proxy_that_will_not_start_means_no_client(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(L, "proxy_command", lambda: [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--quiet", "--timeout", "20", "--", str(out)]) == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "did not become ready" in err and "boom" in err and "To run it directly" in err


def test_direct_if_proxy_fails_runs_the_client_without_the_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "proxy_command", lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--quiet", "--timeout", "20",
                          "--direct-if-proxy-fails", "--", str(out)]) == 0
    assert read(out)["env"]["ANTHROPIC_BASE_URL"] is None


def test_no_start_and_url_never_start_a_proxy(tmp_path, capsys):
    port = L.free_port()
    assert run(tmp_path, ["claude-code", "--port", str(port), "--no-start", "--bin", FAKE, "--quiet", "--", str(tmp_path / "o")]) == 2
    assert run(tmp_path, ["claude-code", "--url", "http://127.0.0.1:%d" % port, "--bin", FAKE, "--quiet", "--", str(tmp_path / "o")]) == 2
    assert closed(port)


# ---- failure injection: the launcher's own bugs never change the client's exit code -------------------

def test_cleanup_failures_do_not_change_the_exit_code(tmp_path, monkeypatch):
    port = L.free_port()
    started = []
    real = L.start_proxy

    def spy(p, env):
        proc, log = real(p, env)
        started.append(proc)
        return proc, log
    monkeypatch.setattr(L, "start_proxy", spy)
    monkeypatch.setattr(L, "stop_proxy", lambda proc: (_ for _ in ()).throw(RuntimeError("injected")))
    monkeypatch.setattr(os, "unlink", lambda p: (_ for _ in ()).throw(OSError("injected")))
    try:
        assert run(tmp_path, ["claude-code", "--port", str(port), "--bin", FAKE, "--quiet", "--", str(tmp_path / "o.json"), "9"]) == 9
    finally:
        monkeypatch.undo()
        for p in started:
            L.stop_proxy(p)
    assert closed(port)


def test_banner_failure_changes_nothing(tmp_path, monkeypatch):
    class Broken:
        def write(self, *a):
            raise OSError("injected")
        def flush(self):
            raise OSError("injected")
    monkeypatch.setattr(sys, "stderr", Broken())
    assert run(tmp_path, ["claude-code", "--port", str(L.free_port()), "--bin", FAKE, "--", str(tmp_path / "o.json")]) == 0


def test_a_readiness_poll_that_raises_stops_the_proxy_and_skips_the_client(tmp_path, monkeypatch):
    port = L.free_port()
    started = []
    real = L.start_proxy
    monkeypatch.setattr(L, "start_proxy", lambda p, e: (lambda r: (started.append(r[0]), r)[1])(real(p, e)))
    monkeypatch.setattr(L, "wait_ready", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    out = tmp_path / "o.json"
    assert run(tmp_path, ["claude-code", "--port", str(port), "--bin", FAKE, "--quiet", "--", str(out)]) == 2
    assert not out.exists()
    deadline = time.time() + 15
    while started and started[0].poll() is None and time.time() < deadline:
        time.sleep(0.1)
    assert started and started[0].poll() is not None and closed(port)


# ---- the real CLI entry ---------------------------------------------------------------------------

def test_main_dispatches_launch_before_the_help_scan(tmp_path):
    env = env_for(tmp_path)
    p = subprocess.run([sys.executable, str(ROOT / "context_guardian.py"), "launch", "--list"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "claude-code" in p.stdout and "context-guardian -- compaction proxy" not in p.stdout
    p = subprocess.run([sys.executable, str(ROOT / "context_guardian.py"), "--help"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "context-guardian launch CLIENT" in p.stdout
    p = subprocess.run([sys.executable, str(ROOT / "context_guardian.py"), "launch", "aider", "--bin", "no-such-xyz", "--", "--help"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 127 and "compaction proxy" not in p.stdout       # --help after -- belongs to the client


@posix_only
def test_ctrl_c_goes_to_the_client_and_not_the_proxy(tmp_path):
    env = env_for(tmp_path)
    out = tmp_path / "o.json"
    port = L.free_port()
    p = subprocess.Popen([sys.executable, str(ROOT / "cg_launch.py"), "claude-code", "--port", str(port), "--bin", FAKE, "--quiet",
                          "--", str(out), "0", "sigint"], env=env, start_new_session=True)
    try:
        ready = Path(str(out) + ".ready")
        deadline = time.time() + 90
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert ready.exists()
        os.killpg(p.pid, signal.SIGINT)                          # what the terminal does on Ctrl-C
        assert p.wait(timeout=60) == 5                           # the client's own exit code; the launcher did not die
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    seen = read(out)
    assert seen["got_sigint"] is True and seen["guardian_after_sigint"] is True   # the proxy survived the SIGINT
    assert closed(port)


def test_npm_wrapper_lists_clients_and_checks_python(tmp_path):
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    env = env_for(tmp_path)
    env["GUARDIAN_PYTHON"] = sys.executable
    p = subprocess.run([node, str(ROOT / "launch.mjs"), "--list"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "claude-code" in p.stdout
    env["GUARDIAN_PYTHON"] = str(tmp_path / "no-such-python")
    env["PATH"] = str(tmp_path)                          # nothing else to find either
    p = subprocess.run([node, str(ROOT / "launch.mjs"), "--list"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "pip install context-guardian" in p.stderr and "no-such-python" in p.stderr


def test_npm_wrapper_runs_the_client_in_init_cwd_and_keeps_ctrl_c(tmp_path):
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    env = env_for(tmp_path)
    env["GUARDIAN_PYTHON"] = sys.executable
    work = tmp_path / "project"
    work.mkdir()
    env["INIT_CWD"] = str(work)
    out = work / "o.json"
    cwd_probe = work / "cwd.txt"
    script = tmp_path / "cwdclient.py"
    script.write_text("import os,sys,json\nopen(sys.argv[1],'w').write(os.getcwd())\n", encoding="utf-8")
    p = subprocess.run([node, str(ROOT / "launch.mjs"), "openai", "--port", str(L.free_port()), "--quiet", "--", str(script), str(cwd_probe)],
                       env=env, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr
    assert os.path.realpath(cwd_probe.read_text()) == os.path.realpath(str(work))
