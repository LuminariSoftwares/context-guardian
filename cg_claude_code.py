#!/usr/bin/env python3
"""cg_claude_code.py -- archive, seal and recall Claude Code sessions.

    context-guardian-claude-code capture      the hook entry point (reads the hook JSON on stdin)
    context-guardian-claude-code install      print the hooks JSON and the `claude mcp add` command;
                                              --apply merges the hooks into your settings file
    context-guardian-claude-code uninstall    the reverse (--apply to edit)
    context-guardian-claude-code status       hooks installed? sessions captured, last capture, seal

Claude Code compacts its own window and by default talks to Anthropic's API
directly. It can be routed through the proxy (ANTHROPIC_BASE_URL, the
/v1/messages route), but most sessions are not, and an archive should not
depend on how the client connects. Claude Code runs hook commands at
documented moments and hands them
the session id and the path of the session's transcript, a JSONL file it keeps
appending to (https://code.claude.com/docs/en/hooks,
https://code.claude.com/docs/en/sessions). `capture` copies the transcript
lines it has not copied before into the span archive, under a run named
cc-<session_id>: sealed into the chain (cg_archive.py), indexed for
list_digests, and searchable with context-guardian-recall and the MCP tools.

It installs on PreCompact (before Claude Code's own compaction drops detail
from the window) and SessionEnd (so the end of a session is never missed).

`capture` must never block, slow or break Claude Code: it always exits 0,
writes a line to its log instead of failing, and kills itself after a hard
timeout. PreCompact treats exit 2 as "block compaction", which is exactly the
kind of side effect an archiver must never have. The transcript's line format
is internal to Claude Code and changes between versions, so lines are copied
verbatim and never interpreted beyond finding their text for search.

Stdlib only, Python 3.10+, Windows and POSIX. MIT licensed.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cg_archive
import cg_paths
import cg_recall_cli

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RUN_PREFIX = "cc-"
STATE_FILE = "cc_state.json"
CAPTURE_LOCK = "cc_capture.lock"
EVENTS = ("PreCompact", "SessionEnd")
# Seconds Claude Code may give the hook. SessionEnd's own default budget is
# 1.5 s; a per-hook timeout raises it (up to 60 s), so the hook asks for 30.
HOOK_TIMEOUT_SECONDS = 30
# The adapter's own limit, under the hook's: it exits 0 on its own before
# Claude Code has to kill it.
DEFAULT_CAPTURE_TIMEOUT = 20.0
# Spans are cut at this size: a first capture of a long session is split
# rather than written as one huge file, and the offset advances per span, so a
# timeout part-way through loses nothing.
MAX_SPAN_BYTES = 2_000_000
MAX_SPAN_LINES = 2_000
MAX_STDIN_BYTES = 1_000_000


# --- logging -----------------------------------------------------------------------

def log_path() -> Path:
    """claude_code_capture.log beside the proxy's compaction log."""
    chosen = (os.environ.get("GUARDIAN_LOG_PATH") or "").strip()
    base = Path(chosen).parent if chosen else cg_paths.data_dir() / cg_paths.LOG_SUBPATH.parent
    return base / "claude_code_capture.log"


def log(message: str) -> None:
    """One line to stderr (Claude Code's debug log) and to the capture log.
    Never raises."""
    line = "context-guardian-claude-code: " + message
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:                                       # noqa: BLE001
        pass
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if new_file:
                # It names transcript files and run ids: private, like the archive.
                cg_archive._make_private(path, cg_archive.PRIVATE_FILE_MODE)
            fh.write("%s %s\n" % (cg_archive.now_iso(), line))
    except Exception:                                       # noqa: BLE001
        pass


# --- capture -----------------------------------------------------------------------

def _load_state(run_dir: Path) -> Dict[str, Any]:
    try:
        data = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _newest_span_end(run_dir: Path, transcript: str) -> Tuple[int, int, int]:
    """(byte offset, line count, highest index) recorded by the newest span of
    this transcript. The state file is a cache; the spans are the record, so a
    capture killed between writing a span and saving the state does not copy
    those lines twice."""
    names = sorted(n for n in cg_archive.archived_files(run_dir) if cg_archive.SPAN_NAME_RE.match(n))
    top = int(names[-1][:-5]) if names else 0
    for name in reversed(names):
        span = cg_archive.load_span(run_dir / name)
        if span and span.get("transcript") == transcript and isinstance(span.get("bytes"), list):
            return int(span["bytes"][1]), int(span.get("lines", [0, 0])[1]), top
    return 0, 0, top


def _parse_line(raw: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"))
        if isinstance(obj, dict):
            return obj
    except (ValueError, UnicodeDecodeError):
        pass
    return {"type": "raw", "raw": raw.decode("utf-8", "replace")}


def capture(hook: Dict[str, Any], span_root: Path, deadline: float) -> int:
    """Copy new transcript lines into the archive. Returns spans written.
    Raises ValueError for a hook payload it will not act on."""
    sid = hook.get("session_id")
    if not isinstance(sid, str) or not SESSION_RE.match(sid):
        raise ValueError("session_id missing or not a plain id")
    transcript = hook.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        raise ValueError("transcript_path missing")
    tpath = Path(transcript)
    if not tpath.exists() or tpath.is_dir():
        raise ValueError("transcript %s does not exist" % transcript)
    event = hook.get("hook_event_name") if isinstance(hook.get("hook_event_name"), str) else ""
    run_id = RUN_PREFIX + sid
    run_dir = Path(span_root) / run_id
    missing = [p for p in (run_dir, *run_dir.parents) if not p.exists()]
    run_dir.mkdir(parents=True, exist_ok=True)
    for made in missing:
        cg_archive._make_private(made, cg_archive.PRIVATE_DIR_MODE)
    seal = os.environ.get("GUARDIAN_SEAL", "1").strip().lower() not in ("0", "false", "no", "off")
    index = os.environ.get("GUARDIAN_INDEX", "1").strip().lower() not in ("0", "false", "no", "off")
    anchor = (os.environ.get("GUARDIAN_SEAL_ANCHOR_PATH") or "").strip() or None

    written = 0
    # One capture per session at a time: PreCompact and SessionEnd can fire
    # back to back, and two readers of the same offset would copy lines twice.
    with cg_archive.ChainLock(run_dir, timeout=5.0, name=CAPTURE_LOCK):
        state = _load_state(run_dir)
        offset, line_no, top = _newest_span_end(run_dir, str(tpath))
        if state.get("transcript") == str(tpath):
            offset = max(offset, int(state.get("offset", 0)))
            line_no = max(line_no, int(state.get("line", 0)))
        with open(tpath, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size < offset:
                log("transcript %s is shorter than the %d bytes already copied; copying it "
                    "again from the start" % (tpath.name, offset))
                offset, line_no = 0, 0
            fh.seek(offset)
            data = fh.read()
        # Only complete lines: Claude Code may be mid-write, and the rest of a
        # half-written line arrives with the next capture.
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        lines = data[:end + 1].split(b"\n")[:-1]
        pos = 0
        while pos < len(lines):
            chunk, nbytes = [], 0
            while pos < len(lines) and len(chunk) < MAX_SPAN_LINES and (not chunk or nbytes < MAX_SPAN_BYTES):
                chunk.append(lines[pos])
                nbytes += len(lines[pos]) + 1
                pos += 1
            start_byte, start_line = offset, line_no
            offset += nbytes
            line_no += len(chunk)
            messages = [_parse_line(raw) for raw in chunk if raw.strip()]

            def payload(claimed, messages=messages, start_byte=start_byte, start_line=start_line,
                        end_byte=offset, end_line=line_no):
                return {"run_id": run_id, "index": claimed,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "num_ctx": 0,
                        "message_count": len(messages), "summary": "", "messages": messages,
                        "source": "claude-code", "session_id": sid, "hook_event": event,
                        "transcript": str(tpath), "bytes": [start_byte, end_byte],
                        "lines": [start_line, end_line]}

            if messages:
                result = cg_archive.write_span_file(run_dir, top + 1, payload)
                if result is None:
                    raise OSError("no free span index after %d" % top)
                path, top, body = result
                if seal:
                    try:
                        cg_archive.append_entry(run_dir, run_id, {"type": "span", "file": path.name,
                                                                  "sha256": cg_archive.sha256_file(path)},
                                                anchor_path=anchor)
                    except Exception as exc:               # noqa: BLE001
                        log("could not seal %s/%s (%s); it is archived, --verify will report it "
                            "unsealed" % (run_id, path.name, exc))
                if index:
                    try:
                        cg_archive.append_index(run_dir, cg_archive.index_entry(body, path.name))
                    except Exception as exc:               # noqa: BLE001
                        log("could not index %s/%s (%s); --reindex rebuilds it" % (run_id, path.name, exc))
                written += 1
            cg_archive.atomic_write_bytes(run_dir / STATE_FILE, json.dumps(
                {"transcript": str(tpath), "offset": offset, "line": line_no,
                 "updated": cg_archive.now_iso()}).encode("utf-8"))
            if time.monotonic() > deadline:
                log("stopping early to stay inside the hook's time; the next capture continues "
                    "from byte %d" % offset)
                break
    if written:
        log("%s: archived %d span(s) from %s up to line %d" % (event or "capture", written, run_id, line_no))
    return written


def _arm_timeout(seconds: float) -> None:
    """Exit 0 from a watchdog thread when capture overruns, whatever the main
    thread is blocked in (a read of a pipe, a network filesystem). Files are
    written atomically and the offset only advances after a span is written,
    so being cut off loses nothing; the next capture continues."""
    def fire():
        log("capture timed out after %gs; exiting 0 so Claude Code carries on" % seconds)
        os._exit(0)
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()


def cmd_capture(ns) -> int:
    _arm_timeout(ns.timeout)
    deadline = time.monotonic() + ns.timeout * 0.6
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES) if hasattr(sys.stdin, "buffer") else \
            sys.stdin.read(MAX_STDIN_BYTES).encode("utf-8")
        try:
            hook = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            hook = None
        if not isinstance(hook, dict):
            log("ignoring hook input that is not a JSON object (%d bytes)" % len(raw))
            return 0
        root = cg_recall_cli.span_dir()
        capture(hook, root, deadline)
    except ValueError as exc:
        log("not capturing: %s" % exc)
    except Exception as exc:                               # noqa: BLE001
        log("capture failed (%s: %s); Claude Code is unaffected" % (type(exc).__name__, exc))
    return 0


# --- install / uninstall -----------------------------------------------------------

def config_dir() -> Path:
    """~/.claude, or CLAUDE_CONFIG_DIR when Claude Code was told to use another."""
    chosen = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(chosen) if chosen else Path(os.path.expanduser("~")) / ".claude"


def settings_path(ns) -> Path:
    if ns.settings:
        return Path(ns.settings)
    if ns.scope == "project":
        return Path.cwd() / ".claude" / "settings.json"
    if ns.scope == "local":
        return Path.cwd() / ".claude" / "settings.local.json"
    return config_dir() / "settings.json"


def _quote(arg: str) -> str:
    return '"%s"' % arg if (" " in arg or "\t" in arg) else arg


def hook_command() -> str:
    """The command the hooks run: the installed console script when it is on
    PATH, else this file with this interpreter (a checkout)."""
    exe = shutil.which("context-guardian-claude-code")
    if exe:
        return "%s capture" % _quote(exe)
    return "%s %s capture" % (_quote(sys.executable), _quote(str(Path(__file__).resolve())))


def is_ours(hook: Any) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    return (isinstance(command, str) and command.rstrip().endswith(" capture")
            and ("context-guardian-claude-code" in command or "cg_claude_code" in command))


def our_hooks(command: str) -> Dict[str, Dict[str, Any]]:
    """One matcher group per event. No matcher: it matches every occurrence
    (manual and auto compaction; every reason a session ends)."""
    return {event: {"hooks": [{"type": "command", "command": command,
                               "timeout": HOOK_TIMEOUT_SECONDS}]} for event in EVENTS}


def merged(settings: Dict[str, Any], command: str) -> Dict[str, Any]:
    """settings with our hooks added. Everything already there is kept as it
    is, in its order; our groups are appended. Already installed: unchanged."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in the settings file is not an object')
    for event, group in our_hooks(command).items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError('"hooks.%s" in the settings file is not a list' % event)
        if any(is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks") or []):
            continue
        groups.append(group)
    return out


def removed(settings: Dict[str, Any]) -> Dict[str, Any]:
    """settings without our hooks. A group, event or "hooks" key is dropped
    only when removing ours is what emptied it."""
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept, changed = [], False
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                remaining = [h for h in g["hooks"] if not is_ours(h)]
                if len(remaining) != len(g["hooks"]):
                    changed = True
                    if not remaining:
                        continue
                    g = dict(g, hooks=remaining)
            kept.append(g)
        if changed:
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks and "hooks" in settings and settings["hooks"] != {}:
        del out["hooks"]
    return out


def _render(settings: Dict[str, Any]) -> str:
    # Two-space JSON, keys in their existing order, non-ASCII as is: the shape
    # Claude Code writes, so the parts of the file we did not touch come out
    # byte for byte as they went in.
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _read_settings(path: Path) -> Tuple[str, Dict[str, Any]]:
    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("the settings file is not a JSON object")
    return text, data


def _backup(path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name("%s.bak-%s" % (path.name, stamp))
    n = 1
    while target.exists():
        target = path.with_name("%s.bak-%s-%d" % (path.name, stamp, n))
        n += 1
    shutil.copy2(path, target)
    return target


def _write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    cg_archive.atomic_write_bytes(path, text.encode("utf-8"))
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=str(path), tofile="%s (new)" % path))


def mcp_commands() -> List[str]:
    installed = shutil.which("context-guardian-mcp")
    out = []
    if installed:
        out.append("claude mcp add --scope user --transport stdio context-guardian -- %s" % _quote(installed))
    out.append("claude mcp add --scope user --transport stdio context-guardian -- uvx context-guardian mcp")
    return out


def _edit(ns, change, verb: str, done: str) -> int:
    path = settings_path(ns)
    try:
        old_text, data = _read_settings(path)
        new_data = change(data)
    except (ValueError, OSError) as exc:
        print("cannot read %s: %s. Nothing was changed." % (path, exc), file=sys.stderr)
        return 1
    new_text = _render(new_data)
    if new_data == data and path.exists():
        print("%s: nothing to %s." % (path, verb))
        return 0
    diff = _diff(path, old_text, new_text)
    if not ns.apply:
        print("dry run: %s would change like this (run again with --apply to write it):\n" % path)
        print(diff or "(no change)")
        return 0
    backup = _backup(path) if path.exists() else None
    _write(path, new_text)
    print(diff)
    print("%s %s%s." % (done, path, "; backup at %s" % backup if backup else ""))
    return 0


def cmd_install(ns) -> int:
    command = hook_command()
    print("Hooks for %s (merged next to any hooks you already have):\n" % settings_path(ns))
    print(json.dumps({"hooks": {e: [g] for e, g in our_hooks(command).items()}}, indent=2))
    print("\nRecall MCP server (run one of these once):\n")
    for line in mcp_commands():
        print("  " + line)
    print()
    return _edit(ns, lambda data: merged(data, command), "install", "Hooks installed in")


def cmd_uninstall(ns) -> int:
    return _edit(ns, removed, "remove", "Hooks removed from")


# --- status --------------------------------------------------------------------------

def cmd_status(ns) -> int:
    path = settings_path(ns)
    try:
        _, data = _read_settings(path)
    except (ValueError, OSError) as exc:
        data = {}
        print("settings: cannot read %s (%s)" % (path, exc))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    have = [e for e in EVENTS if any(is_ours(h) for g in hooks.get(e) or [] if isinstance(g, dict)
                                     for h in g.get("hooks") or [])]
    if len(have) == len(EVENTS):
        print("hooks:        installed in %s (%s)" % (path, ", ".join(have)))
    else:
        print("hooks:        not installed in %s%s" % (path, " (only %s)" % ", ".join(have) if have else "")
              + "; run context-guardian-claude-code install --apply")
    root = cg_recall_cli.span_dir()
    runs = [d for d in cg_archive.run_dirs(root) if d.name.startswith(RUN_PREFIX)]
    print("sessions:     %d session%s captured under %s" % (len(runs), "" if len(runs) == 1 else "s", root))
    newest = 0.0
    for d in runs:
        for name in cg_archive.archived_files(d) + [STATE_FILE]:
            try:
                newest = max(newest, (d / name).stat().st_mtime)
            except OSError:
                pass
    print("last capture: %s" % (datetime.datetime.fromtimestamp(newest).astimezone().isoformat(timespec="seconds")
                                if newest else "never"))
    reports = [cg_archive.verify_run(d, cg_archive.read_anchor(cg_recall_cli.anchor_path()))
               for d in runs]
    c = cg_archive.summarise(reports)
    if not runs:
        print("seal:         nothing captured yet")
    elif c["breaks"] == 0 and c["unsealed"] == 0:
        print("seal:         all %d session(s) verify OK" % c["runs"])
    else:
        print("seal:         %d broken, %d unsealed of %d; context-guardian-recall --verify --all for detail"
              % (c["breaks"], c["unsealed"], c["runs"]))
    print("log:          %s" % log_path())
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="context-guardian-claude-code",
        description="Archive, seal and recall Claude Code sessions through its hooks.")
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="hook entry point: reads the hook JSON on stdin, always exits 0")
    cap.add_argument("--timeout", type=float, default=DEFAULT_CAPTURE_TIMEOUT,
                     help="hard limit in seconds (default %(default)s)")
    for name, helptext in (("install", "print the hooks JSON and the claude mcp add command; --apply edits"),
                           ("uninstall", "remove the hooks; --apply edits"),
                           ("status", "hooks, sessions captured, last capture, seal status")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--settings", metavar="PATH", help="the settings file (default: by --scope)")
        p.add_argument("--scope", choices=("user", "project", "local"), default="user",
                       help="user: ~/.claude/settings.json (default); project: .claude/settings.json; "
                            "local: .claude/settings.local.json")
        if name != "status":
            p.add_argument("--apply", action="store_true",
                           help="write the change (a timestamped backup is made first)")
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return {"capture": cmd_capture, "install": cmd_install, "uninstall": cmd_uninstall,
            "status": cmd_status}[ns.command](ns)


if __name__ == "__main__":
    sys.exit(main())
