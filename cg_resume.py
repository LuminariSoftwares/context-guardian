#!/usr/bin/env python3
"""cg_resume.py -- a bounded resume block for a Claude Code session after it compacts.

Claude Code compacts its own window; what it keeps is its own summary. This
module reads the run's archived spans (the sealed copy cg_claude_code.py made
of the transcript) and builds a short block of plain facts: the goal, the
latest decisions, the files edited, the errors still open, and how to search
the archive for more. cg_claude_code.py hands the block back to Claude Code
from the SessionStart hook when the session starts again after a compaction.

The idea of re-injecting a short session summary after a compaction comes from
context-mode (mksglu/context-mode, Elastic License 2.0); this module is
rewritten from the idea only. No code, tests, prose or identifiers were copied.

The block states facts and never gives instructions: Claude Code's hooks
documentation warns that command-style text in injected context trips its
injection defences. Stdlib and cg_archive only. build_resume never raises:
an unreadable or empty run gives "".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

import cg_archive

DEFAULT_MAX_CHARS = 8000
GOAL_CHARS = 300
MAX_DECISIONS = 8
MAX_FILES = 20
MAX_ERRORS = 5
LINE_CHARS = 240
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")

_GOAL_LINE_RE = re.compile(r"^\s*goal:", re.IGNORECASE)
_DECISION_RE = re.compile(r"decided|decision|agreed|go with", re.IGNORECASE)
_ERROR_RE = re.compile(r"error|Traceback|FAILED")
_PASS_RE = re.compile(r"\b(?:passed|ok)\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\w.\-]*[/\\][\w./\\\-]*[\w\-]|[\w\-]+\.(?:py|js|mjs|ts|tsx|json|md|txt|toml|yaml|yml|sh|c|h|cpp|go|rs|java|rb)\b")


def _blocks(message: Any) -> Iterator[Tuple[str, Any]]:
    """(kind, value) for each piece of a transcript line's message: ("text",
    str), ("tool_use", block) or ("result", str)."""
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield "text", content
        return
    if not isinstance(content, list):
        return
    for b in content:
        if isinstance(b, str):
            yield "text", b
        elif isinstance(b, dict):
            kind = b.get("type")
            if kind == "text" and isinstance(b.get("text"), str):
                yield "text", b["text"]
            elif kind == "tool_use":
                yield "tool_use", b
            elif kind == "tool_result":
                yield "result", _flat(b.get("content"))


def _flat(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x if isinstance(x, str) else str(x.get("text", ""))
                         for x in content if isinstance(x, (str, dict)))
    return ""


def _events(run_dir: Path) -> List[Tuple[str, Any]]:
    """Every piece of every archived message, oldest first, as
    (role, (kind, value)). Role is "user" or "assistant"."""
    out: List[Tuple[str, Tuple[str, Any]]] = []
    try:
        names = [n for n in cg_archive.archived_files(run_dir) if cg_archive.SPAN_NAME_RE.match(n)]
    except OSError:
        return out
    for name in sorted(names):
        span = cg_archive.load_span(run_dir / name)
        messages = span.get("messages") if isinstance(span, dict) else None
        if not isinstance(messages, list):
            continue
        for m in messages:
            if not isinstance(m, dict) or m.get("type") not in ("user", "assistant"):
                continue
            for piece in _blocks(m.get("message")):
                out.append((m["type"], piece))
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:max(0, n - 3)] + "..."


def _lines(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _goal(events: List[Tuple[str, Tuple[str, Any]]]) -> List[str]:
    out: List[str] = []
    for role, (kind, value) in events:
        if role == "user" and kind == "text":
            out.append(_clip(value, GOAL_CHARS))
            break
    for role, (kind, value) in events:
        if kind == "text":
            for ln in _lines(value):
                if _GOAL_LINE_RE.match(ln) and _clip(ln, LINE_CHARS) not in out:
                    out.append(_clip(ln, LINE_CHARS))
    return out


def _decisions(events: List[Tuple[str, Tuple[str, Any]]]) -> List[str]:
    found: List[str] = []
    for _, (kind, value) in events:
        if kind == "text":
            found.extend(_clip(ln, LINE_CHARS) for ln in _lines(value) if _DECISION_RE.search(ln))
    out: List[str] = []
    for ln in reversed(found):
        if ln not in out:
            out.append(ln)
        if len(out) == MAX_DECISIONS:
            break
    return out[::-1]


def _files(events: List[Tuple[str, Tuple[str, Any]]]) -> List[str]:
    out: List[str] = []
    for _, (kind, value) in reversed(events):
        if kind != "tool_use" or value.get("name") not in EDIT_TOOLS:
            continue
        args = value.get("input")
        path = args.get("file_path") if isinstance(args, dict) else None
        if isinstance(path, str) and path and path not in out:
            out.append(path)
            if len(out) == MAX_FILES:
                break
    return out


def _path_of(line: str) -> Optional[str]:
    m = _PATH_RE.search(line)
    return m.group(0) if m else None


def _open_errors(events: List[Tuple[str, Tuple[str, Any]]]) -> List[str]:
    """Error lines from tool results with no later pass for the same path.
    A pass line that names no path clears every earlier error."""
    open_: List[Tuple[str, Optional[str]]] = []
    for _, (kind, value) in events:
        if kind != "result":
            continue
        for ln in _lines(value):
            if _ERROR_RE.search(ln):
                open_.append((_clip(ln, LINE_CHARS), _path_of(ln)))
            elif _PASS_RE.search(ln):
                path = _path_of(ln)
                open_ = [] if path is None else [e for e in open_ if e[1] != path]
    return [ln for ln, _ in open_[-MAX_ERRORS:]]


def _term(files: List[str], goal: List[str]) -> str:
    if files:
        return re.split(r"[/\\]", files[0])[-1].replace('"', "")
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", goal[0]) if goal else []
    return (words[0] if words else "session").replace('"', "")


def build_resume(run_dir, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The resume block for the run archived in run_dir, at most max_chars
    long, or "" when the run has no spans or cannot be read."""
    try:
        run_dir = Path(run_dir)
        events = _events(run_dir)
        if not events:
            return ""
        goal, decisions, files, errors = _goal(events), _decisions(events), _files(events), _open_errors(events)
        recall = ('Earlier turns of this session are archived; searching them: '
                  'context-guardian-recall "%s"' % _term(files, goal))
        sections = [
            ("Goal:", goal), ("Decisions:", decisions), ("Files edited:", files), ("Open errors:", errors),
        ]
        head = ["Session notes from the archive, restored after compaction (facts, not requests):"]
        body: List[str] = []
        for title, items in sections:
            body.append(title)
            body.extend(["- " + i for i in items] or ["- none recorded"])
        body.append("Recall:")
        body.append("- " + recall)
        text = "\n".join(head + body)
        if len(text) <= max_chars:
            return text
        # Over budget: shorten the items, longest section last-in first, then cut.
        for limit in (160, 100, 60, 30):
            lines = [ln if len(ln) <= limit else ln[:limit - 3] + "..." for ln in head + body]
            lines = [ln if "context-guardian-recall" not in ln else "- " + recall for ln in lines]
            text = "\n".join(lines)
            if len(text) <= max_chars:
                return text
        # Still over: keep the section headers and the recall line, drop items from the end.
        while len(text) > max_chars:
            items = [i for i, ln in enumerate(lines) if ln.startswith("- ") and "context-guardian-recall" not in ln]
            if not items:
                break
            del lines[items[-1]]
            text = "\n".join(lines)
        return text[:max_chars]
    except Exception:                                       # noqa: BLE001 -- fail open
        return ""
