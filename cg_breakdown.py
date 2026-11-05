"""cg_breakdown.py -- where the context window went, by category.

breakdown_tokens(payload) splits one request into six parts, in estimated
tokens: system, tool_schemas, history, tool_results, summary, pins. The parts
partition the request, so they add up to the estimate the proxy already makes
for it (estimate_tokens(messages) + estimate_tool_tokens(payload)), within
rounding. /guardian/stats shows the latest request's split under `breakdown`.

The idea of a per-category breakdown of the context window comes from
context-mode (mksglu/context-mode, Elastic 2.0); this module is rewritten from
the idea and no code was copied.

Categories:
  system       role system/developer messages (and an Anthropic top-level `system`),
               except the proxy's own compaction summaries
  tool_schemas the `tools` / `functions` arrays
  tool_results role tool messages and tool_result content blocks
  summary      system messages the proxy inserted as compaction summaries
  pins         0: pinned facts ride inside the summary message, not as messages of
               their own, so they are counted under summary
  history      everything else

Stdlib only, pure: no I/O, and it never imports context_guardian. The proxy
passes its own estimators so each part is measured exactly as the request is;
the defaults apply the same chars-per-token rule for standalone use.
"""
from __future__ import annotations

import json
import os

KEYS = ("system", "tool_schemas", "history", "tool_results", "summary", "pins")
# Same prefix the proxy stamps on its compaction summaries (context_guardian.GUARDIAN_SUMMARY_MARKER).
SUMMARY_MARKER = "[Context Guardian auto-compaction"


def _cpt():
    try:
        return float(os.environ.get("GUARDIAN_CHARS_PER_TOKEN", "3.5")) or 3.5
    except ValueError:
        return 3.5


def _chars(blob):
    try:
        return len(json.dumps(blob, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(blob))


def _default_messages(messages):
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            total += len(str(m))
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    total += len(b["text"])
                else:
                    total += _chars(b) if isinstance(b, dict) else len(str(b))
        for key in ("tool_calls", "function_call"):
            if m.get(key):
                total += _chars(m[key])
    return int(total / _cpt())


def _default_tools(payload):
    chars = sum(_chars(payload[k]) for k in ("tools", "functions") if payload.get(k))
    return int(chars / _cpt())


def _is_summary(m):
    c = m.get("content")
    return m.get("role") == "system" and isinstance(c, str) and c.lstrip().startswith(SUMMARY_MARKER)


def breakdown_tokens(payload, estimate_messages=None, estimate_tools=None):
    """Six non-negative ints (KEYS) for one request payload. Never raises on odd input."""
    est_m = estimate_messages or _default_messages
    est_t = estimate_tools or _default_tools
    out = dict.fromkeys(KEYS, 0)
    if not isinstance(payload, dict):
        return out
    groups = {"system": [], "history": [], "tool_results": [], "summary": []}
    top = payload.get("system")
    if top:
        groups["system"].append({"role": "system", "content": top})
    messages = payload.get("messages")
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict):
            groups["history"].append(m)
        elif _is_summary(m):
            groups["summary"].append(m)
        elif m.get("role") in ("system", "developer"):
            groups["system"].append(m)
        elif m.get("role") == "tool":
            groups["tool_results"].append(m)
        elif isinstance(m.get("content"), list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]):
            blocks = m["content"]
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            rest = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_result")]
            groups["tool_results"].append({"role": m.get("role"), "content": results})
            if rest:
                groups["history"].append({**m, "content": rest})
        else:
            groups["history"].append(m)
    for key, msgs in groups.items():
        if msgs:
            out[key] = max(0, int(est_m(msgs)))
    out["tool_schemas"] = max(0, int(est_t(payload)))
    return out
