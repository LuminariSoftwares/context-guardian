"""Run the real Claude Code through `context-guardian launch claude-code` against the scripted mock model.

    python tests/clients/run_launch_check.py [--bin PATH-TO-claude]

Isolated home folder, a stripped environment, the mock upstream of run_clients.py, then the two scripted
turns (the second continuing the first), each as its own `launch` so the proxy is started and stopped
twice. Passes when both turns print the mock's answer marker, the mock saw requests, and nothing listens
on the proxy port afterwards. Output: `context-guardian launch check (<platform>)`, then
`claude-code <version> PASS|FAIL <reason>` or `claude-code - SKIP not installed`. Exit 1 on FAIL.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import run_clients as rc  # noqa: E402


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default="claude")
    args = ap.parse_args(argv)
    exe = shutil.which(args.bin) or (args.bin if os.path.isfile(args.bin) else None)
    print("context-guardian launch check (%s, python %s)" % (platform.system() + " " + platform.release(), platform.python_version()))
    if not exe:
        print("claude-code - SKIP not installed")
        return 0
    turns = json.loads((HERE / "scripted_turns.json").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix="cg-launch-check-"))
    mock = None
    reasons = []
    try:
        mock, mock_port = rc.start_mock(work)
        port = rc.free_port()
        env = rc.home_env(work / "home")
        env.update({"GUARDIAN_PORT": str(port), "GUARDIAN_UPSTREAM_URL": "http://127.0.0.1:%d/v1" % mock_port,
                    "GUARDIAN_NUM_CTX": str(turns["num_ctx"].get("claude-code", turns["num_ctx"]["default"])),
                    "GUARDIAN_DETECT_CONTEXT": "0", "GUARDIAN_KEEP_RECENT_MESSAGES": str(turns["keep_recent"]),
                    "GUARDIAN_DATA_DIR": str(work / "guardian"), "GUARDIAN_VERSION_CHECK": "0",
                    "ANTHROPIC_API_KEY": "mock"})
        project = work / "project"
        project.mkdir()
        version = "unknown"
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, env=env, cwd=str(project),
                               stdin=subprocess.DEVNULL)
            version = rc.first_version_word((p.stdout or "") + "\n" + (p.stderr or ""))
        except (OSError, subprocess.SubprocessError):
            pass
        output = ""
        for n, text in enumerate(turns["turns"], 1):
            cmd = [sys.executable, str(ROOT / "context_guardian.py"), "launch", "claude-code", "--bin", exe, "--quiet", "--"]
            cmd += (["-c"] if n > 1 else []) + ["-p", text]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=rc.TURN_TIMEOUT, env=env,
                                   cwd=str(project), stdin=subprocess.DEVNULL)
                output, code = (p.stdout or "") + "\n" + (p.stderr or ""), p.returncode
            except subprocess.TimeoutExpired:
                output, code = "", "timeout"
            if code != 0:
                reasons.append("turn %d exit %s" % (n, code))
            if listening(port):
                reasons.append("the proxy was still listening after turn %d" % n)
        if turns["answer_marker"] not in output:
            reasons.append("the answer was not displayed")
        log = work / "mock.jsonl"
        if not log.exists() or not log.read_text(encoding="utf-8").strip():
            reasons.append("the mock upstream saw no request")
        print("claude-code %s %s" % (version, "FAIL " + "; ".join(reasons) if reasons else "PASS"))
    finally:
        rc.stop(mock)
        shutil.rmtree(work, ignore_errors=True)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
