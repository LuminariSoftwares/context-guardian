#!/usr/bin/env python3
"""cg_mcp.py -- Context Guardian as an MCP server (stdio).

    context-guardian-mcp          (or: python cg_mcp.py)

Lets any MCP client (Claude Desktop, Claude Code, Cursor, ...) look at what the
proxy has done, without curl or a terminal:

    guardian_status   the running proxy's /guardian/stats (loopback HTTP)
    search_spans      find text in the spans compaction archived
    read_span         one archived span: its summary and original messages

All three only read. Nothing here changes the proxy, its settings or the
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


TOOLS = [
    {
        "name": "guardian_status",
        "title": "Proxy status",
        "description": ("Context Guardian's live status from the running proxy, as "
                        "JSON: window and budget, compactions, tokens saved, rejected "
                        "summaries, calibration. Read-only: one local HTTP request to "
                        "GUARDIAN_HOST:GUARDIAN_PORT (default 127.0.0.1:8786), no "
                        "credentials. Returns an error saying how to start the proxy "
                        "when it is not running. Call it to see whether compaction is "
                        "working; use search_spans or read_span for the archived "
                        "conversation text instead."),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": _annotations("Proxy status", open_world=True),
    },
    {
        "name": "search_spans",
        "title": "Search archived spans",
        "description": ("Case-insensitive search of the conversation text "
                        "compaction archived, for details that no longer fit in the "
                        "window. Read-only, local files only. Returns one line per hit: "
                        "span file, message index and role, snippet (at most 50 hits). "
                        "Use read_span to open a whole span from a hit; use "
                        "guardian_status for proxy counters, not conversation text."),
        "inputSchema": {"type": "object", "properties": {
            "term": {"type": "string", "description": (
                "Text to find, matched case-insensitively as plain text (not a "
                "regex), e.g. \"migration plan\". Must not be empty.")},
            "run": {"type": "string", "description": (
                "Only search this proxy run id, e.g. \"20261015-120000\" (the "
                "folder name in the span archive). Omit to search every run.")},
            "max": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_HITS, "description": (
                "Maximum number of hits to return, from 1 to %d, e.g. 10. "
                "Defaults to %d." % (MAX_SEARCH_HITS, MAX_SEARCH_HITS))},
        }, "required": ["term"]},
        "annotations": _annotations("Search archived spans", open_world=False),
    },
    {
        "name": "read_span",
        "title": "Read one archived span",
        "description": ("Read one archived span: the summary compaction wrote and "
                        "the original messages it replaced. Read-only, local files "
                        "only. Returns text, cut at %d characters for long spans, or "
                        "an error when no such span exists. Use it after search_spans "
                        "gives you the run id and index; use search_spans to find "
                        "text, not this." % READ_SPAN_MAX_CHARS),
        "inputSchema": {"type": "object", "properties": {
            "run": {"type": "string", "description": (
                "Run id, e.g. \"20261015-120000\": the folder name that "
                "search_spans prints before the slash in each hit.")},
            "index": {"type": "integer", "minimum": 1, "description": (
                "Span number within the run, counting from 1, e.g. 3: the "
                "file number search_spans prints (0003.json is 3).")},
        }, "required": ["run", "index"]},
        "annotations": _annotations("Read one archived span", open_world=False),
    },
]


class ToolError(Exception):
    """A failure the client's model should read as a tool result."""


def _guardian_status(_args: Dict[str, Any]) -> str:
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
    return json.dumps(stats, indent=1, sort_keys=True)


def _search_spans(args: Dict[str, Any]) -> str:
    term = args.get("term")
    if not isinstance(term, str) or not term.strip():
        raise ToolError("search_spans needs a non-empty `term`.")
    run = args.get("run")
    if run is not None and (not isinstance(run, str) or not RUN_ID_RE.match(run)):
        raise ToolError("`run` must be a run id such as 20261015-120000.")
    limit = args.get("max", MAX_SEARCH_HITS)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = MAX_SEARCH_HITS
    limit = max(1, min(limit, MAX_SEARCH_HITS))
    root = cg_recall_cli.span_dir()
    if not root.is_dir():
        raise ToolError(f"No span archive at {root}: nothing has been compacted yet.")
    lines = []
    for path, idx, role, text in cg_recall_cli.search(root, term, run):
        label = "summary" if idx == "summary" else f"msg {idx} [{role}]"
        lines.append(f"{path.parent.name}/{path.name} {label}: {text}")
        if len(lines) >= limit:
            lines.append(f"(stopped at {limit} hits; narrow the term or pass run)")
            break
    return "\n".join(lines) if lines else f"No match for {term!r}."


def _read_span(args: Dict[str, Any]) -> str:
    run, index = args.get("run"), args.get("index")
    # Validated before it touches a path: a run id is one directory name.
    if not isinstance(run, str) or not RUN_ID_RE.match(run):
        raise ToolError("`run` must be a run id such as 20261015-120000.")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ToolError("`index` must be a positive integer.")
    path = cg_recall_cli.span_dir() / run / f"{index:04d}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"No readable span {run}/{index:04d}.json.") from exc
    if not isinstance(data, dict):
        raise ToolError(f"Span {run}/{index:04d}.json is not a span.")
    out = [f"span {run}/{index:04d}.json, {len(data.get('messages') or [])} messages",
           f"summary: {data.get('summary', '')}", ""]
    for i, m in enumerate(data.get("messages") or []):
        role = m.get("role", "?") if isinstance(m, dict) else "?"
        out.append(f"[{i}] {role}: {cg_recall_cli.message_text(m)}")
    text = "\n".join(out)
    if len(text) > READ_SPAN_MAX_CHARS:
        text = (text[:READ_SPAN_MAX_CHARS]
                + f"\n... (cut at {READ_SPAN_MAX_CHARS} chars; search_spans finds the rest)")
    return text


HANDLERS = {"guardian_status": _guardian_status, "search_spans": _search_spans,
            "read_span": _read_span}


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
        handler = HANDLERS.get(name)
        if handler is None:
            return err(-32602, f"Unknown tool: {name}")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            return ok({"content": [{"type": "text", "text": handler(args)}], "isError": False})
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
