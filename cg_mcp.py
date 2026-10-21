#!/usr/bin/env python3
"""cg_mcp.py -- Context Guardian as an MCP server (stdio).

    context-guardian-mcp          (or: python cg_mcp.py)

Lets any MCP client (Claude Desktop, Claude Code, Cursor, ...) look at what the
proxy has done, without curl or a terminal:

    get_proxy_status  the running proxy's /guardian/stats (loopback HTTP)
    list_spans        the runs in the span archive, or the spans in one run
    search_spans      find text in the spans compaction archived
    read_span         one archived span: its summary and original messages

All four only read. `guardian_status` (the 0.10.0 name of get_proxy_status)
is still accepted by tools/call but no longer listed. Nothing here changes the proxy, its settings or the
archive. Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout, as the MCP
stdio transport specifies; logs go to stderr only. Stdlib only, Python 3.10+.
MIT licensed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import cg_recall_cli

SERVER_NAME = "context-guardian"
SERVER_VERSION = "0.10.0"  # keep equal to pyproject.toml version
# Newest first. The client's version is echoed when supported, else ours.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
STATUS_TIMEOUT_SECONDS = 3.0
MAX_SEARCH_HITS = 50
MAX_LIST_ENTRIES = 100
DEFAULT_LIST_ENTRIES = 20
LIST_SUMMARY_CHARS = 100
# A span is larger than the window had room for, by construction; one read
# must not hand the whole thing back to the client's model.
READ_SPAN_MAX_CHARS = 20_000
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _annotations(title: str, *, open_world: bool) -> Dict[str, Any]:
    """The MCP tool annotations. Every tool here reads and nothing else, so
    readOnly is true and destructive false for all of them; repeating a call
    changes nothing, so idempotent is true. Only openWorld differs."""
    return {"title": title, "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": open_world}


RUN_ID_EXAMPLE = "20261015-120000"
RUN_ID_DOC = ("Proxy run id: the span archive folder name, as list_spans and search_spans print it "
              "before the slash, or `run_id` from get_proxy_status.")


def _run_param(extra: str) -> Dict[str, Any]:
    return {"type": "string", "pattern": RUN_ID_RE.pattern,
            "description": f"{RUN_ID_DOC} {extra} e.g. \"{RUN_ID_EXAMPLE}\".",
            "examples": [RUN_ID_EXAMPLE]}


def _max_param(what: str, top: int, default: int, example: int) -> Dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": top, "default": default,
            "description": (f"Most {what} to return, 1 to {top}, e.g. {example}. Defaults to "
                            f"{default}; out-of-range values are clamped."),
            "examples": [example]}


# /guardian/stats has ~50 keys; these are the ones an agent acts on. Nothing is
# required, so a proxy older or newer than this server still validates.
STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "version": {"type": "string", "description": "Proxy package version."},
        "upstream": {"type": "string", "description": "Backend URL requests are forwarded to."},
        "run_id": {"type": "string", "description": "This proxy run's id: the archive folder its spans go to."},
        "span_dir": {"type": "string", "description": "Directory spans are archived in (GUARDIAN_SPAN_DIR)."},
        "num_ctx": {"type": "integer", "description": "Context window, in tokens, the budget is computed from."},
        "effective_input_budget": {"type": "integer", "description": (
            "Tokens a request may use before it is compacted: num_ctx x compact_threshold - reserve_output.")},
        "last_known_total_tokens": {"type": "integer", "description": (
            "Estimated size of the latest request, tools array included.")},
        "requests_seen": {"type": "integer", "description": "Chat requests handled since start."},
        "compactions_performed": {"type": "integer", "description": "Compactions since start."},
        "tokens_saved_total": {"type": "integer", "description": "Estimated tokens compaction removed, in total."},
        "summaries_rejected_empty": {"type": "integer", "description": (
            "Summaries refused as empty or too short; those requests went upstream unchanged.")},
        "dry_run": {"type": "boolean", "description": (
            "True when GUARDIAN_DRY_RUN=1: compactions are computed and logged, requests forwarded unchanged.")},
        "calibration": {"type": "object", "description": (
            "Per model: `factor` applied to the characters/3.5 token estimate (1.0 until 5 samples) and "
            "`samples` of reported usage seen."),
            "additionalProperties": {"type": "object", "properties": {
                "factor": {"type": "number"}, "samples": {"type": "integer"}}}},
        "uptime_seconds": {"type": "number", "description": "Seconds since the proxy started."},
    },
}

TOOLS = [
    {
        "name": "get_proxy_status",
        "title": "Proxy status",
        "description": (
            "Get the running proxy's live settings and counters: context window and input budget, "
            "compactions and tokens saved, refused summaries, per-model token calibration. Use it to "
            "check that the proxy is up and compacting; for the conversation text compaction archived, "
            "use list_spans, search_spans or read_span instead, which work without the proxy. Sends one "
            "GET to http://GUARDIAN_HOST:GUARDIAN_PORT/guardian/stats (default 127.0.0.1:8786), "
            "%g s timeout, no credentials. If the proxy is not running, returns an error result naming "
            "the URL and the command that starts it." % STATUS_TIMEOUT_SECONDS),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": STATUS_OUTPUT_SCHEMA,
        "annotations": _annotations("Proxy status", open_world=True),
    },
    {
        "name": "list_spans",
        "title": "List archived spans",
        "description": (
            "List what the span archive holds without a search term: every proxy run with its span "
            "count, newest run first, or, given `run`, each span in that run with its message count "
            "and the start of its summary. Use it to find a run id or span number when you have "
            "nothing to search for; use search_spans when you know the text, and read_span to open "
            "one span. Reads local span files only (GUARDIAN_SPAN_DIR), no network. Returns one line "
            "per run, `<run>: <n> spans`, or per span, `<run>/<NNNN>.json, <n> messages: <summary "
            "start>`, plus a closing note when `max` cut the list; an error result when the archive "
            "or that run does not exist."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _run_param("List this run's spans instead of the runs; omit to list runs,"),
                "max": _max_param("runs or spans", MAX_LIST_ENTRIES, DEFAULT_LIST_ENTRIES, 10),
            },
            "additionalProperties": False,
        },
        "annotations": _annotations("List archived spans", open_world=False),
    },
    {
        "name": "search_spans",
        "title": "Search archived spans",
        "description": (
            "Search the conversation text compaction archived for a word or phrase: a file name, a "
            "decision, an error message that no longer fits in the context window. Use it when you "
            "know what to look for; use list_spans to browse without a term, and read_span to open a "
            "whole span from a hit. Plain-text, case-insensitive match over every archived message "
            "and each span's summary, oldest span first, one hit per message; `run` narrows it to one "
            "run and `max` caps the hits. Reads local span files only (GUARDIAN_SPAN_DIR), no network. "
            "Returns one line per hit, `<run>/<NNNN>.json msg <i> [<role>]: <snippet>` (`summary:` for "
            "a span's summary), plus a closing note when `max` stopped it; `No match for '<term>'.` "
            "when nothing matches; an error result when the archive does not exist yet."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string", "minLength": 1,
                    "description": ("Text to find, as a plain substring (not a regex), any case, "
                                    "e.g. \"migration plan\". Required; must contain a non-space "
                                    "character."),
                    "examples": ["migration plan", "parse.py"],
                },
                "run": _run_param("Search only this run; omit to search every run,"),
                "max": _max_param("hits", MAX_SEARCH_HITS, MAX_SEARCH_HITS, 10),
            },
            "required": ["term"],
            "additionalProperties": False,
        },
        "annotations": _annotations("Search archived spans", open_world=False),
    },
    {
        "name": "read_span",
        "title": "Read one archived span",
        "description": (
            "Read one archived span in full: the summary compaction wrote and the original messages "
            "it replaced, in order. Use it once list_spans or search_spans has given you a run id and "
            "span number; to find text across spans use search_spans instead, since one span can be "
            "larger than your context has room for. `run` and `index` together name one local file, "
            "GUARDIAN_SPAN_DIR/<run>/<NNNN>.json; no network. Returns a header `span <run>/<NNNN>.json, "
            "<n> messages`, a `summary:` line, then `[<i>] <role>: <text>` per message, cut at %d "
            "characters with a note; an error result when that span does not exist."
            % READ_SPAN_MAX_CHARS),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _run_param("Required,"),
                "index": {
                    "type": "integer", "minimum": 1,
                    "description": ("Span number within the run, counting from 1: the NNNN in the "
                                    "file name, e.g. 3 for 0003.json. Required."),
                    "examples": [3],
                },
            },
            "required": ["run", "index"],
            "additionalProperties": False,
        },
        "annotations": _annotations("Read one archived span", open_world=False),
    },
]
# Old names tools/call still accepts. Not listed: a second name for the same
# tool is exactly the ambiguity a client's model should not have to resolve.
DEPRECATED_ALIASES = {"guardian_status": "get_proxy_status"}


class ToolError(Exception):
    """A failure the client's model should read as a tool result."""


def _limit(args: Dict[str, Any], default: int, top: int) -> int:
    value = args.get("max", default)
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(value, top))


def _run_arg(args: Dict[str, Any], *, required: bool) -> Optional[str]:
    run = args.get("run")
    if run is None and not required:
        return None
    # Validated before it touches a path: a run id is one directory name.
    if not isinstance(run, str) or not RUN_ID_RE.match(run):
        raise ToolError("`run` must be a run id such as %s." % RUN_ID_EXAMPLE)
    return run


def _span_root():
    root = cg_recall_cli.span_dir()
    if not root.is_dir():
        raise ToolError(f"No span archive at {root}: nothing has been compacted yet.")
    return root


def _get_proxy_status(_args: Dict[str, Any]):
    host = os.environ.get("GUARDIAN_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = os.environ.get("GUARDIAN_PORT", "8786")
    url = f"http://{host}:{port}/guardian/stats"
    try:
        with urllib.request.urlopen(url, timeout=STATUS_TIMEOUT_SECONDS) as resp:
            stats = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ToolError(f"Context Guardian proxy not reachable at {url} ({exc}). "
                        "Start it with `context-guardian`.") from exc
    if not isinstance(stats, dict):
        raise ToolError(f"{url} answered, but not with Context Guardian stats.")
    return json.dumps(stats, indent=1, sort_keys=True), stats


def _list_spans(args: Dict[str, Any]) -> str:
    run = _run_arg(args, required=False)
    limit = _limit(args, DEFAULT_LIST_ENTRIES, MAX_LIST_ENTRIES)
    root = _span_root()
    lines = []
    if run is None:
        runs = sorted((d for d in root.iterdir() if d.is_dir() and RUN_ID_RE.match(d.name)),
                      key=lambda d: d.name, reverse=True)
        for d in runs[:limit]:
            lines.append(f"{d.name}: {len(list(d.glob('*.json')))} spans")
        total, what = len(runs), "runs"
    else:
        run_dir = root / run
        if not run_dir.is_dir():
            raise ToolError(f"No run {run} in the span archive; list_spans without `run` lists them.")
        spans = sorted(run_dir.glob("*.json"))
        for path in spans[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if not isinstance(data, dict):
                lines.append(f"{run}/{path.name}: unreadable")
                continue
            messages = data.get("messages")
            count = len(messages) if isinstance(messages, list) else 0
            summary = " ".join(str(data.get("summary", "")).split())
            if len(summary) > LIST_SUMMARY_CHARS:
                summary = summary[:LIST_SUMMARY_CHARS] + "..."
            lines.append(f"{run}/{path.name}, {count} messages: {summary}")
        total, what = len(spans), "spans"
    if not lines:
        return f"No {what} in the span archive yet."
    if total > limit:
        lines.append(f"(showing {limit} of {total} {what}; raise max to see more)")
    return "\n".join(lines)


def _search_spans(args: Dict[str, Any]) -> str:
    term = args.get("term")
    if not isinstance(term, str) or not term.strip():
        raise ToolError("search_spans needs a non-empty `term`.")
    run = _run_arg(args, required=False)
    limit = _limit(args, MAX_SEARCH_HITS, MAX_SEARCH_HITS)
    root = _span_root()
    lines = []
    for path, idx, role, text in cg_recall_cli.search(root, term, run):
        label = "summary" if idx == "summary" else f"msg {idx} [{role}]"
        lines.append(f"{path.parent.name}/{path.name} {label}: {text}")
        if len(lines) >= limit:
            lines.append(f"(stopped at {limit} hits; narrow the term or pass run)")
            break
    return "\n".join(lines) if lines else f"No match for {term!r}."


def _read_span(args: Dict[str, Any]) -> str:
    run, index = _run_arg(args, required=True), args.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ToolError("`index` must be a positive integer.")
    path = cg_recall_cli.span_dir() / run / f"{index:04d}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"No readable span {run}/{index:04d}.json.") from exc
    if not isinstance(data, dict):
        raise ToolError(f"Span {run}/{index:04d}.json is not a span.")
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    out = [f"span {run}/{index:04d}.json, {len(messages)} messages",
           f"summary: {data.get('summary', '')}", ""]
    for i, m in enumerate(messages):
        role = m.get("role", "?") if isinstance(m, dict) else "?"
        out.append(f"[{i}] {role}: {cg_recall_cli.message_text(m)}")
    text = "\n".join(out)
    if len(text) > READ_SPAN_MAX_CHARS:
        text = (text[:READ_SPAN_MAX_CHARS]
                + f"\n... (cut at {READ_SPAN_MAX_CHARS} chars; search_spans finds the rest)")
    return text


# A handler returns the text result, or (text, structuredContent) for a tool
# that declares an outputSchema.
HANDLERS = {"get_proxy_status": _get_proxy_status, "list_spans": _list_spans,
            "search_spans": _search_spans, "read_span": _read_span}


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, the reply out (None for a notification)."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notifications (initialized, cancelled) need no reply

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": SERVER_NAME, "title": "Context Guardian",
                                  "version": SERVER_VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(DEPRECATED_ALIASES.get(name, name)) if isinstance(name, str) else None
        if handler is None:
            return err(-32602, f"Unknown tool: {name}")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            out = handler(args)
            text, structured = out if isinstance(out, tuple) else (out, None)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if structured is not None:
                result["structuredContent"] = structured
            return ok(result)
        except ToolError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return err(-32601, f"Method not found: {method}")


def main() -> int:
    # The transport is UTF-8 whatever the console code page (Windows).
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
        else:
            if not isinstance(msg, dict):
                reply = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": "Invalid Request"}}
            else:
                try:
                    reply = handle(msg)
                except Exception as exc:                   # noqa: BLE001
                    print(f"cg_mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
                    reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                             "error": {"code": -32603, "message": "Internal error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
