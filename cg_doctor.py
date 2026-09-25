#!/usr/bin/env python3
"""cg_doctor.py -- check a context-guardian install and say how to fix what is wrong.

One line per check (OK / WARN / FAIL, each non-OK line followed by a `fix:` line), then a
count line; exit 1 when a check failed. Stdlib only, Python 3.10+, all three OSes. MIT.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil

# subprocess is used for exactly one thing: the bridge probe below, with an argv list.
import subprocess  # nosec B404
import sys
import tempfile
import urllib.request
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PKG_FALLBACK_NAME = "dsh-context-guardian"
DEFAULT_PORT = 8786
PROXY_DEFAULT_NUM_CTX = 32768
HELLO_IN = '{"id":1,"op":"hello"}\n{"id":2,"op":"shutdown"}\n'
RESERVE_FIX = "lower GUARDIAN_RESERVE_OUTPUT or raise GUARDIAN_NUM_CTX"
NUMCTX_RE = re.compile(r"^\s*numCtx\s*:\s*(\d+)\s*(#.*)?$", re.MULTILINE)
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
PYVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# (key, smallest accepted whole number) -- one table, one loop, no copy-paste
WHOLE_KEYS = (("GUARDIAN_NUM_CTX", 1), ("GUARDIAN_RESERVE_OUTPUT", 0),
              ("GUARDIAN_KEEP_RECENT_MESSAGES", 0), ("GUARDIAN_PORT", 1))

def _f(level, what, fix=""):
    return {"level": level, "what": what, "fix": fix}

def _whole(raw, minimum=0):
    """int(raw) when raw is digits only and >= minimum, else None."""
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number >= minimum else None

def read_env_file(path: Path) -> dict:
    """KEY=VALUE per line; skips blank lines, `#` comments and lines without `=`, and
    strips one pair of matching surrounding quotes. Missing/unreadable -> {}; never raises."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = (part.strip() for part in line.strip().partition("="))
        if key.startswith("#"):
            continue
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out

def find_preset_num_ctx(preset: Path | None) -> int | None:
    """The first non-commented `numCtx: <digits>` in a DSH preset, else None."""
    try:
        text = Path(preset).read_text(encoding="utf-8", errors="replace") if preset else ""
    except OSError:
        return None
    match = NUMCTX_RE.search(text)
    return int(match.group(1)) if match else None

def resolve_window(env: dict, preset_num_ctx: int | None) -> dict:
    """A positive whole-number env GUARDIAN_NUM_CTX wins, then the preset, else the host;
    a bad env value falls through here -- check 2 is what reports it."""
    value = _whole(env.get("GUARDIAN_NUM_CTX"), 1)
    if value is not None:
        return {"source": "env", "value": value}
    if preset_num_ctx is not None:
        return {"source": "preset", "value": preset_num_ctx}
    return {"source": "host", "value": None}

def find_python(root: Path, env: dict, which, os_name: str = os.name) -> str | None:
    """Same order as the JS plugin: $GUARDIAN_PYTHON, a .venv beside the package, then
    python / python3 on PATH. The env value is used as given (no existence test) so a
    typo surfaces as a bridge failure that has a fix."""
    given = (env.get("GUARDIAN_PYTHON") or "").strip()
    if given:
        return given
    if os_name == "nt":
        candidate = Path(root) / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = Path(root) / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else which("python" if os_name == "nt" else "python3")

def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """urllib GET. Raises OSError (URLError is one) on failure -- never a fake status."""
    opener = urllib.request.build_opener()
    with opener.open(url, timeout=timeout) as response:  # loopback URL built by the caller
        return int(response.getcode()), response.read().decode("utf-8", "replace")

def _run_bridge(argv: list[str], stdin_text: str, timeout: float) -> tuple[int, str]:
    """Run the bridge with an argv list, never a shell. A timeout or OSError comes back
    as (-1, "<ExceptionName>: <message>") instead of raising."""
    try:
        # argv is a list and shell is False, and the interpreter is the user's own
        # GUARDIAN_PYTHON / .venv / PATH choice -- so B603 does not apply here.
        proc = subprocess.run(argv, input=stdin_text, capture_output=True,  # nosec B603
                              text=True, timeout=timeout, shell=False, check=False)
        return proc.returncode, proc.stdout
    except (OSError, ValueError) as exc:
        return -1, f"{type(exc).__name__}: {exc}"

def _bridge_version(out: str):
    """(X.Y.Z, (X, Y)) from the bridge's first non-empty line, else (None, None)."""
    first = next((ln for ln in out.splitlines() if ln.strip()), "")
    try:
        reply = json.loads(first)
    except ValueError:
        return None, None
    result = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
    version = result.get("python") if isinstance(result, dict) else None
    parts = PYVER_RE.match(version) if isinstance(version, str) else None
    return (version, (int(parts.group(1)), int(parts.group(2)))) if parts else (None, None)

def _number_findings(work: dict) -> list[dict]:
    """Check 2's numbers: one loop over WHOLE_KEYS, the threshold, then the pair."""
    found, nums = [], {}
    for key, minimum in WHOLE_KEYS:
        raw = (work.get(key) or "").strip()
        if not raw:
            continue
        number = _whole(raw, minimum)
        if number is None:
            found.append(_f("error", f"{key}={raw} is not a whole number",
                            f"set {key} to digits only (e.g. 8192) in your .env or environment"))
        else:
            nums[key] = number
    threshold = (work.get("GUARDIAN_COMPACT_THRESHOLD") or "").strip()
    try:
        share = float(threshold) if threshold else None
    except ValueError:
        share = None
    if threshold and (share is None or not 0 < share < 1):
        found.append(_f("error", f"GUARDIAN_COMPACT_THRESHOLD={threshold} must be between 0 and 1",
                        "use a fraction such as 0.85"))
    num, reserve = nums.get("GUARDIAN_NUM_CTX"), nums.get("GUARDIAN_RESERVE_OUTPUT")
    if num is not None and reserve is not None and reserve >= num:
        found.append(_f("error", f"GUARDIAN_RESERVE_OUTPUT ({reserve}) is not below GUARDIAN_NUM_CTX ({num})",
                        RESERVE_FIX))
    return found

def doctor(root: Path = PACKAGE_ROOT, env: dict | None = None, preset: Path | None = None,
           http_get=None, run=None, which=shutil.which) -> list[dict]:
    """Run every check and return the findings, in check order."""
    root = Path(root)
    if env is None:
        env = dict(os.environ)
    get, run_it = (_http_get if http_get is None else http_get), (_run_bridge if run is None else run)
    # the process environment WINS over .env, as the proxy's load_dotenv does.
    work = read_env_file(root / ".env")
    work.update(env)
    out = []

    # 1. package + proxy version
    name, data = PKG_FALLBACK_NAME, None
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except OSError:
        data = None
    except ValueError:
        data = "bad"
    if isinstance(data, dict) and isinstance(data.get("version"), str) and data["version"]:
        name = str(data.get("name") or PKG_FALLBACK_NAME)
        out.append(_f("ok", "package: {} {}".format(name, data["version"])))
    else:
        bad = "package.json is not valid JSON" if data == "bad" else "package.json not found"
        out.append(_f("error", bad, "run the doctor from the installed package folder (npm ls dsh-context-guardian shows where)"))
    try:
        match = VERSION_RE.search((root / "context_guardian.py").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        match = None
    if match:
        out.append(_f("ok", f"proxy: context_guardian.py {match.group(1)}"))
    else:
        out.append(_f("warn", "proxy version not found", f"reinstall the package: npm i {name}@latest"))

    # 2. .env and the numbers in it
    envfile = root / ".env"
    out.append(_f("ok", f"using .env: {envfile}" if envfile.is_file() else "no .env (optional)"))
    out.extend(_number_findings(work))

    # 3. the window both halves will use
    w = resolve_window(work, find_preset_num_ctx(preset))
    if w["source"] == "env":
        out.append(_f("ok", f"window: {w['value']} tokens from GUARDIAN_NUM_CTX (engine and proxy)"))
    elif w["source"] == "preset":
        out.append(_f("warn", f"window: pinned to {w['value']} by numCtx in {preset}",
                      "delete the numCtx line so the engine uses the window DSH reports for your model; "
                      f"keep it only if {w['value']} really is your model's window"))
    else:
        out.append(_f("ok", "window: the engine uses the window DSH reports for the session; "
                            f"the proxy uses its default {PROXY_DEFAULT_NUM_CTX}"))

    # 4. the proxy
    host = (work.get("GUARDIAN_HOST") or "").strip() or "127.0.0.1"
    port = _whole(work.get("GUARDIAN_PORT"), 1) or DEFAULT_PORT
    url = f"http://{host}:{port}/guardian/stats"
    stats = None
    try:
        status, body = get(url, 2.0)
        candidate = json.loads(body) if status == 200 else None
        stats = candidate if isinstance(candidate, dict) else None
    except Exception:  # noqa: BLE001 -- any transport or parse failure means "not reachable"
        stats = None
    if stats is None:
        out.append(_f("warn", f"proxy not reachable at {url}", "optional: the DSH bundle works without it. To run it: python context_guardian.py"))
    else:
        running = stats.get("num_ctx")
        whole = isinstance(running, int) and not isinstance(running, bool)
        shown = f" (num_ctx {running})" if whole else ""
        out.append(_f("ok", f"proxy reachable at {url}{shown}"))
        if w["source"] == "env" and whole and running != w["value"]:
            out.append(_f("warn", f"proxy is running with num_ctx {running} but GUARDIAN_NUM_CTX is {w['value']}",
                          "restart the proxy so it re-reads GUARDIAN_NUM_CTX"))

    # 5. the Python bridge
    bridge = root / "modules" / "cg_bridge.py"
    if not bridge.is_file():
        out.append(_f("error", "modules/cg_bridge.py is missing", f"reinstall the package: npm i {PKG_FALLBACK_NAME}@latest"))
    else:
        py = find_python(root, work, which)
        if not py:
            out.append(_f("error", "no Python found for the bridge", "install Python 3.10+ or set GUARDIAN_PYTHON to its full path"))
        else:
            try:
                code, said = run_it([py, str(bridge)], HELLO_IN, 20.0)
            except Exception as exc:  # noqa: BLE001 -- an injected runner may raise
                code, said = -1, f"{type(exc).__name__}: {exc}"
            version, series = _bridge_version(said or "")
            if series is not None and series < (3, 10):
                out.append(_f("error", f"python bridge runs Python {version}; 3.10+ is required",
                              "set GUARDIAN_PYTHON to a Python 3.10+ interpreter"))
            elif series is not None:
                out.append(_f("ok", f"python bridge works: {py} (Python {version})"))
            else:
                detail = (said or "").strip()[-200:] or (f"exit code {code}")
                out.append(_f("error", f"python bridge failed: {detail}",
                              f"run: {py} modules/cg_bridge.py --selftest  and check GUARDIAN_PYTHON"))
    return out

def render(findings) -> tuple[str, int]:
    """The text report and its exit code: 1 when any finding is an error."""
    lines, counts = [], {"ok": 0, "warn": 0, "error": 0}
    for finding in findings:
        level = finding["level"]
        counts[level] = counts.get(level, 0) + 1
        lines.append("{} {}".format({"ok": "OK  ", "warn": "WARN", "error": "FAIL"}[level], finding["what"]))
        if level != "ok" and finding["fix"]:
            lines.append("     fix: {}".format(finding["fix"]))
    lines.append(f"doctor: {counts['ok']} ok, {counts['warn']} warnings, {counts['error']} errors")
    return "\n".join(lines), (1 if counts["error"] else 0)

def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        return selftest()
    parser = argparse.ArgumentParser(prog="cg_doctor.py", description="Check a context-guardian install and say how to fix it.")
    parser.add_argument("--preset", type=Path, default=None, help="a DSH preset / cordis.patch.yml to check for a pinned numCtx")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    ns = parser.parse_args(args)
    findings = doctor(preset=ns.preset)
    if ns.json:
        print(json.dumps(findings, indent=2))
        return 1 if any(f["level"] == "error" for f in findings) else 0
    text, code = render(findings)
    print(text)
    return code

# --- selftest: real comparisons over temp-dir fixtures; http_get/run/which are fakes.
def _plant(root, with_pkg=True, proxy="9.9.9"):
    root = Path(root)
    if with_pkg:
        (root / "package.json").write_text(f'{{"name": "{PKG_FALLBACK_NAME}", "version": "1.2.3"}}', encoding="utf-8")
    if proxy is not None:
        (root / "context_guardian.py").write_text(f'__version__ = "{proxy}"\n', encoding="utf-8")
    (root / "modules").mkdir(parents=True, exist_ok=True)
    (root / "modules" / "cg_bridge.py").write_text("# bridge\n", encoding="utf-8")
    return root

def _up(url, timeout):
    return 200, '{"num_ctx": 65536}'

def _good_run(argv, stdin_text, timeout):
    return 0, '{"id":1,"ok":true,"result":{"python":"3.11.9"}}\n'

def _dr(root, env=None, preset=None, http_get=None, run=None):
    """doctor() over a planted root with every door faked."""
    return doctor(root=root, env=env if env is not None else {"GUARDIAN_PYTHON": "py"}, preset=preset,
                  http_get=http_get or _up, run=run or _good_run, which=lambda name: None)

def _levels(findings, level):
    return [f for f in findings if f["level"] == level]

def _check_env_file_parses_quotes_and_comments():
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / ".env"
        path.write_text('A="x"\n# B=1\nC = 3\njunk\n', encoding="utf-8")
        return read_env_file(path) == {"A": "x", "C": "3"}

def _check_env_file_missing_is_empty():
    with tempfile.TemporaryDirectory() as t:
        return read_env_file(Path(t) / "nope.env") == {}

def _check_preset_numctx_found_and_comment_ignored():
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "cordis.patch.yml"
        path.write_text("  config:\n    # numCtx: 1\n    numCtx: 65536\n", encoding="utf-8")
        return (find_preset_num_ctx(path) == 65536 and find_preset_num_ctx(Path(t) / "gone.yml") is None
                and find_preset_num_ctx(None) is None)

def _check_window_env_beats_preset():
    return resolve_window({"GUARDIAN_NUM_CTX": "65536"}, 4096) == {"source": "env", "value": 65536}

def _check_window_preset_when_no_env():
    return resolve_window({}, 4096) == {"source": "preset", "value": 4096}

def _check_window_host_when_neither():
    return resolve_window({}, None) == {"source": "host", "value": None}

def _check_window_bad_env_value_falls_through():
    return resolve_window({"GUARDIAN_NUM_CTX": "abc"}, 4096) == {"source": "preset", "value": 4096}

def _check_find_python_prefers_env_then_venv_then_path():
    with tempfile.TemporaryDirectory() as t:
        root, asked = Path(t), []

        def which(name):
            asked.append(name)
            return "PATH-" + name

        venv = root / ".venv" / "Scripts" / "python.exe"
        venv.parent.mkdir(parents=True)
        venv.write_text("x", encoding="utf-8")
        from_venv = find_python(root, {}, which, os_name="nt")
        venv.unlink()
        from_nt, from_path = find_python(root, {}, which, os_name="nt"), find_python(root, {}, which, "posix")
        from_env = find_python(root, {"GUARDIAN_PYTHON": "  C:/py/python.exe  "}, which, os_name="nt")
        return (from_venv == str(venv) and from_nt == "PATH-python" and from_path == "PATH-python3"
                and from_env == "C:/py/python.exe" and asked == ["python", "python3"])

def _check_default_root_is_the_file_folder():
    return (PACKAGE_ROOT == Path(__file__).resolve().parent and PACKAGE_ROOT.name != "modules"
            and PACKAGE_ROOT.is_dir())

def _check_all_good_has_no_errors():
    with tempfile.TemporaryDirectory() as t:
        findings = _dr(_plant(t), env={"GUARDIAN_NUM_CTX": "65536", "GUARDIAN_PYTHON": "py"})
        return len(findings) == 6 and all(f["level"] == "ok" for f in findings)

def _check_missing_package_json_is_error():
    with tempfile.TemporaryDirectory() as t:
        return any("package.json not found" in f["what"] and f["fix"]
                   for f in _levels(_dr(_plant(t, with_pkg=False)), "error"))

def _check_bad_numeric_is_error_with_fix():
    with tempfile.TemporaryDirectory() as t:
        errors = _levels(_dr(_plant(t), env={"GUARDIAN_PYTHON": "py", "GUARDIAN_NUM_CTX": "32k"}), "error")
        return len(errors) == 1 and "GUARDIAN_NUM_CTX=32k" in errors[0]["what"] and bool(errors[0]["fix"])

def _check_reserve_not_below_window_is_error():
    with tempfile.TemporaryDirectory() as t:
        env = {"GUARDIAN_PYTHON": "py", "GUARDIAN_NUM_CTX": "8192", "GUARDIAN_RESERVE_OUTPUT": "9999"}
        return any("GUARDIAN_RESERVE_OUTPUT (9999) is not below GUARDIAN_NUM_CTX (8192)" in f["what"]
                   and RESERVE_FIX in f["fix"] for f in _levels(_dr(_plant(t), env=env), "error"))

def _check_preset_pin_is_warn():
    with tempfile.TemporaryDirectory() as t:
        root = _plant(t)
        preset = root / "cordis.patch.yml"
        preset.write_text("config:\n  numCtx: 32768\n", encoding="utf-8")
        return any("pinned to 32768" in f["what"] and f["fix"] for f in _levels(_dr(root, preset=preset), "warn"))

def _check_proxy_down_is_warn_not_error():
    def down(url, timeout):
        raise OSError("refused")

    with tempfile.TemporaryDirectory() as t:
        findings = _dr(_plant(t), http_get=down)
        wanted = f"proxy not reachable at http://127.0.0.1:{DEFAULT_PORT}/guardian/stats"
        return any(wanted in f["what"] and f["fix"] for f in _levels(findings, "warn")) \
            and not _levels(findings, "error")

def _check_proxy_window_mismatch_is_warn():
    def stale(url, timeout):
        return 200, '{"num_ctx": 32768}'

    with tempfile.TemporaryDirectory() as t:
        env = {"GUARDIAN_PYTHON": "py", "GUARDIAN_NUM_CTX": "65536"}
        warns = _levels(_dr(_plant(t), env=env, http_get=stale), "warn")
        return any("num_ctx 32768 but GUARDIAN_NUM_CTX is 65536" in f["what"]
                   and "restart the proxy" in f["fix"] for f in warns)

def _check_bridge_failure_is_error():
    def crashed(argv, stdin_text, timeout):
        return 1, "Traceback (most recent call last): boom\n"

    with tempfile.TemporaryDirectory() as t:
        return any("python bridge failed" in f["what"] and "boom" in f["what"] and f["fix"]
                   for f in _levels(_dr(_plant(t), run=crashed), "error"))

def _check_bridge_old_python_is_error():
    def old(argv, stdin_text, timeout):
        return 0, '{"id":1,"ok":true,"result":{"python":"3.8.10"}}\n'

    with tempfile.TemporaryDirectory() as t:
        return any("3.10+ is required" in f["what"] and "3.10+" in f["fix"]
                   for f in _levels(_dr(_plant(t), run=old), "error"))

def _check_cli_exit_codes():
    text, code = render([{"level": "error", "what": "x", "fix": "do y"}, {"level": "ok", "what": "z", "fix": ""}])
    ok_text, ok_code = render([{"level": "ok", "what": "z", "fix": ""}])
    last = text.strip().splitlines()[-1]
    return (code == 1 and ok_code == 0 and "FAIL x" in text and "fix: do y" in text
            and re.fullmatch(r"doctor: 1 ok, 0 warnings, 1 errors", last) is not None
            and ok_text.strip().splitlines()[-1] == "doctor: 1 ok, 0 warnings, 0 errors")

CHECKS = [("env_file_parses_quotes_and_comments", _check_env_file_parses_quotes_and_comments), ("env_file_missing_is_empty", _check_env_file_missing_is_empty), ("preset_numctx_found_and_comment_ignored", _check_preset_numctx_found_and_comment_ignored), ("window_env_beats_preset", _check_window_env_beats_preset), ("window_preset_when_no_env", _check_window_preset_when_no_env), ("window_host_when_neither", _check_window_host_when_neither), ("window_bad_env_value_falls_through", _check_window_bad_env_value_falls_through), ("find_python_prefers_env_then_venv_then_path", _check_find_python_prefers_env_then_venv_then_path), ("default_root_is_the_file_folder", _check_default_root_is_the_file_folder), ("all_good_has_no_errors", _check_all_good_has_no_errors), ("missing_package_json_is_error", _check_missing_package_json_is_error), ("bad_numeric_is_error_with_fix", _check_bad_numeric_is_error_with_fix), ("reserve_not_below_window_is_error", _check_reserve_not_below_window_is_error), ("preset_pin_is_warn", _check_preset_pin_is_warn), ("proxy_down_is_warn_not_error", _check_proxy_down_is_warn_not_error), ("proxy_window_mismatch_is_warn", _check_proxy_window_mismatch_is_warn), ("bridge_failure_is_error", _check_bridge_failure_is_error), ("bridge_old_python_is_error", _check_bridge_old_python_is_error), ("cli_exit_codes", _check_cli_exit_codes)]

def selftest() -> int:
    total, passed = 0, 0
    for name, function in CHECKS:
        detail = ""
        try:
            ok = bool(function())
        except Exception as exc:  # noqa: BLE001 -- a crash is a failed check
            ok, detail = False, f" ({type(exc).__name__}: {exc})"
        if ok:
            passed += 1
        print("  {} {}{}".format("ok  " if ok else "FAIL", name, detail))
        total += 1
    failed = total - passed
    print(f"cg_doctor selftest: {total} checks, {passed} passed, {failed} failed")
    return 1 if failed or total == 0 else 0

if __name__ == "__main__":
    sys.exit(main())
