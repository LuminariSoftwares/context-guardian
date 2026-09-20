#!/usr/bin/env python3
"""
cg_bridge.py -- private bridge between the dsh-context-guardian DSH plugin
(Node) and the Python Context Guardian in this repo.

Inside DSH, compaction itself is done in-process by the plugin (the vendored
deterministic compiler) -- no Python on the hot path. This bridge exists so the
plugin and the HTTP proxy stay ONE product: it reports the proxy's resolved
GUARDIAN_* configuration and reads the span archive the proxy has already
written, so recall/search can reach history compacted before the plugin
existed. The proxy (`context-guardian` / `context_guardian.py`) is untouched
and keeps working for anyone who prefers that path.

PROTOCOL -- one JSON object per line, both directions, UTF-8 (same framing as
dsh-tool-guardian's tg_bridge.py):
    request   {"id": <int>, "op": "<name>", ...params}
    response  {"id": <int>, "ok": true,  "result": {...}}
              {"id": <int>, "ok": false, "error": "<Type>: <message>"}

    hello      -> versions + interpreter. Imports NOTHING heavy; liveness probe.
    config     -> the proxy's resolved settings (imports context_guardian,
                  which needs its requirements.txt installed; fails loud if not)
    spans      {"limit": <int>?} -> newest span files in the archive
    shutdown   -> exits 0

    python cg_bridge.py              serve on stdio
    python cg_bridge.py --selftest   offline checks, stdlib only

Python >= 3.10 -- same floor as context_guardian.py. MIT licensed.
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

BRIDGE_VERSION = "0.1.0"

# context_guardian.py lives one level above modules/, in the repo AND in the
# published npm package. Resolved from __file__, never from the working dir.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FIELDS = ("GUARDIAN_PORT", "GUARDIAN_HOST", "UPSTREAM_URL", "NUM_CTX",
                 "COMPACT_THRESHOLD", "RESERVE_OUTPUT", "KEEP_RECENT_MESSAGES",
                 "CHARS_PER_TOKEN_ESTIMATE", "KEEP_SPANS", "KEEP_SUMMARIES")


def default_span_dir() -> Path:
    """The proxy's own rule, restated without importing it: env, else
    <package>/logs/guardian_spans."""
    return Path(os.environ.get("GUARDIAN_SPAN_DIR",
                               str(PACKAGE_ROOT / "logs" / "guardian_spans")))


def _load_proxy():
    if str(PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_ROOT))
    import context_guardian  # noqa: PLC0415  (heavy: fastapi/httpx; on demand only)
    return context_guardian


class Bridge:
    def __init__(self, load_proxy=_load_proxy, span_dir=None):
        self._load_proxy = load_proxy      # injectable: selftest passes a fake
        self._span_dir = span_dir

    def op_hello(self, _req: dict) -> dict:
        return {"bridge": BRIDGE_VERSION,
                "python": platform.python_version(),
                "executable": sys.executable,
                "package_root": str(PACKAGE_ROOT),
                "pid": os.getpid()}

    def op_config(self, _req: dict) -> dict:
        cg = self._load_proxy()
        out = {name: getattr(cg, name) for name in CONFIG_FIELDS if hasattr(cg, name)}
        out["SPAN_DIR"] = str(getattr(cg, "SPAN_DIR", default_span_dir()))
        out["version"] = getattr(cg, "__version__", "unknown")
        return out

    def op_spans(self, req: dict) -> dict:
        root = Path(self._span_dir) if self._span_dir else default_span_dir()
        limit = max(1, min(int(req.get("limit") or 50), 500))
        if not root.is_dir():
            # Absent is reported as absent, never as an empty archive.
            return {"dir": str(root), "exists": False, "spans": []}
        files = sorted((p for p in root.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return {"dir": str(root), "exists": True, "total": len(files),
                "spans": [{"name": p.name, "bytes": p.stat().st_size,
                           "mtime": p.stat().st_mtime} for p in files[:limit]]}

    def op_shutdown(self, _req: dict) -> dict:
        return {"exit": True}

    def dispatch(self, req: dict) -> dict:
        rid = req.get("id")
        op = str(req.get("op") or "")
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            return {"id": rid, "ok": False, "error": "UnknownOp: %r" % op}
        try:
            return {"id": rid, "ok": True, "result": fn(req)}
        except Exception as exc:  # noqa: BLE001
            return {"id": rid, "ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}


def serve(stdin, proto, bridge=None) -> int:
    bridge = bridge or Bridge()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("frame is not an object")
        except ValueError as exc:
            reply = {"id": None, "ok": False, "error": "BadFrame: %s" % exc}
        else:
            reply = bridge.dispatch(req)
        proto.write(json.dumps(reply, ensure_ascii=False) + "\n")
        proto.flush()
        if reply.get("ok") and (reply.get("result") or {}).get("exit"):
            return 0
    return 0


# ------------------------------------------------------------- selftest -----

class _FakeProxy:
    __version__ = "9.9.9"
    NUM_CTX = 32768
    COMPACT_THRESHOLD = 0.85
    SPAN_DIR = Path("fake-spans")


def _check_hello_needs_no_proxy_import() -> bool:
    def boom():
        raise AssertionError("hello must not import the proxy")
    r = Bridge(load_proxy=boom).dispatch({"id": 1, "op": "hello"})
    return r.get("ok") is True and r["result"]["bridge"] == BRIDGE_VERSION


def _check_config_reports_proxy_constants() -> bool:
    r = Bridge(load_proxy=lambda: _FakeProxy).dispatch({"id": 2, "op": "config"})
    res = r.get("result") or {}
    return (r.get("ok") is True and res.get("NUM_CTX") == 32768
            and res.get("COMPACT_THRESHOLD") == 0.85
            and res.get("version") == "9.9.9" and res.get("SPAN_DIR") == "fake-spans")


def _check_config_import_failure_is_loud() -> bool:
    def missing():
        raise ImportError("No module named 'fastapi'")
    r = Bridge(load_proxy=missing).dispatch({"id": 3, "op": "config"})
    return r.get("ok") is False and "fastapi" in str(r.get("error"))


def _check_missing_span_dir_is_absent_not_empty() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        r = Bridge(span_dir=str(Path(tmp) / "nope")).dispatch({"id": 4, "op": "spans"})
    res = r.get("result") or {}
    return r.get("ok") is True and res.get("exists") is False and res.get("spans") == []


def _check_spans_lists_newest_first_with_limit() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        for i, name in enumerate(("a.json", "b.json", "c.json")):
            p = Path(tmp) / name
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (1000 + i, 1000 + i))
        r = Bridge(span_dir=tmp).dispatch({"id": 5, "op": "spans", "limit": 2})
    res = r.get("result") or {}
    return (res.get("total") == 3
            and [s["name"] for s in res.get("spans", [])] == ["c.json", "b.json"])


def _check_default_span_dir_is_under_package_root() -> bool:
    saved = os.environ.pop("GUARDIAN_SPAN_DIR", None)
    try:
        d = default_span_dir()
    finally:
        if saved is not None:
            os.environ["GUARDIAN_SPAN_DIR"] = saved
    return (d == PACKAGE_ROOT / "logs" / "guardian_spans"
            and (PACKAGE_ROOT / "context_guardian.py").is_file()
            and PACKAGE_ROOT.name != "modules")


def _check_serve_roundtrip_and_shutdown_exits() -> bool:
    stdin = io.StringIO('{"id": 7, "op": "hello"}\nnot json\n{"id": 8, "op": "shutdown"}\n'
                        '{"id": 9, "op": "hello"}\n')
    proto = io.StringIO()
    code = serve(stdin, proto)
    frames = [json.loads(x) for x in proto.getvalue().splitlines()]
    return (code == 0 and len(frames) == 3
            and frames[0]["id"] == 7 and frames[0]["ok"] is True
            and "BadFrame" in frames[1]["error"]
            and frames[2]["result"]["exit"] is True)


CHECKS = [
    ("hello_needs_no_proxy_import", _check_hello_needs_no_proxy_import),
    ("config_reports_proxy_constants", _check_config_reports_proxy_constants),
    ("config_import_failure_is_loud", _check_config_import_failure_is_loud),
    ("missing_span_dir_is_absent_not_empty", _check_missing_span_dir_is_absent_not_empty),
    ("spans_lists_newest_first_with_limit", _check_spans_lists_newest_first_with_limit),
    ("default_span_dir_is_under_package_root", _check_default_span_dir_is_under_package_root),
    ("serve_roundtrip_and_shutdown_exits", _check_serve_roundtrip_and_shutdown_exits),
]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = fn() is True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
        else:
            print("  %s %s" % ("ok  " if ok else "FAIL", name))
        if ok:
            passed += 1
    total = len(CHECKS)
    print("cg_bridge selftest: %d checks, %d passed, %d failed"
          % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--version" in argv:
        print(BRIDGE_VERSION)
        return 0
    proto = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr  # nothing but `proto` may reach the real stdout
    return serve(stdin, proto)


if __name__ == "__main__":
    raise SystemExit(main())
