"""Model-driven context tools (GUARDIAN_CONTEXT_TOOLS): the pure half (ADR 0009).

The proxy offers the model four tools. The model PROPOSES (`guardian_compress` names a range of
turns; nothing else the model writes ever enters the history); the proxy VALIDATES the proposal
here, and the proxy's own fenced summariser does the summarising on the next request. This module
holds the turn model, the validation, the per-session fold store, the four tool bodies and the
re-framing of a finished completion as a server-sent stream. It does no I/O and calls nothing
upstream; the proxy hands in estimators and applies what comes out.

Standard library only. MIT licensed.
"""
import collections
import hashlib
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

TOOL_PREFIX = "guardian_"
T_STATUS = "guardian_context_status"
T_SEARCH = "guardian_search_context"
T_COMPRESS = "guardian_compress"
T_DECOMPRESS = "guardian_decompress"
TOOL_NAMES = (T_STATUS, T_SEARCH, T_COMPRESS, T_DECOMPRESS)

MIN_FOLD_TOKENS = 1500
STATUS_MAX_CHARS = 3000
SEARCH_MAX_HITS = 8
SEARCH_MAX_CHARS = 2400
PREVIEW_CHARS = 60
SESSIONS_MAX = 64


def tool_definitions() -> List[Dict[str, Any]]:
    def fn(name, description, props, required):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}
    return [
        fn(T_STATUS, "Show how full the context window is and which turns can be folded into a summary.", {}, []),
        fn(T_SEARCH, "Keyword search over the turns and summaries currently in your context.",
           {"query": {"type": "string", "description": "words to look for"}}, ["query"]),
        fn(T_COMPRESS, "Propose folding a finished range of turns into a summary the system writes. "
                       "Takes effect on your next request. Use turn numbers from guardian_context_status.",
           {"from_turn": {"type": "integer"}, "to_turn": {"type": "integer"}}, ["from_turn", "to_turn"]),
        fn(T_DECOMPRESS, "Cancel a fold so its turns come back unchanged.",
           {"fold_id": {"type": "string", "description": "for example f1"}}, ["fold_id"]),
    ]


def is_proxy_tool(name: Any) -> bool:
    return name in TOOL_NAMES


def client_tool_collides(tools: Any) -> bool:
    """True when the client defines a tool in the proxy's namespace. Tolerates any junk in `tools`."""
    for t in tools if isinstance(tools, list) else []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name.startswith(TOOL_PREFIX):
            return True
    return False


def protected_turns(turns: List[Dict[str, Any]], message_count: int, protect_turns: int, keep_messages: int) -> set:
    """Turn numbers that can never be folded: the first (the task), the last `protect_turns`, and
    every turn touching the newest `keep_messages` messages."""
    out = set()
    if turns:
        out.add(1)
        for t in turns[-max(1, protect_turns):]:
            out.add(t["n"])
        floor = message_count - max(0, keep_messages)
        for t in turns:
            if t["end"] >= floor:
                out.add(t["n"])
    return out


# ---- turns -------------------------------------------------------------------------------

def message_text(m: Any) -> str:
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p["text"] for p in c if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def segment_turns(non_system: List[Dict[str, Any]], hash_of: Callable[[Any], str],
                  tokens_of: Callable[[Any], int]) -> List[Dict[str, Any]]:
    """Turns over the non-system messages: a `user` message and the messages after it up to the
    next `user` message. Leading non-user messages join the first turn."""
    turns: List[Dict[str, Any]] = []
    for i, m in enumerate(non_system):
        starts = isinstance(m, dict) and m.get("role") == "user"
        # a user message opens a turn, except that leading non-user messages share the first one
        if (starts and not (len(turns) == 1 and not turns[0]["preview"] and turns[0]["start"] == 0
                            and not turns[0].get("has_user"))) or not turns:
            turns.append({"n": len(turns) + 1, "start": i, "end": i, "hashes": [], "tokens": 0, "preview": ""})
        if starts:
            turns[-1]["has_user"] = True
        t = turns[-1]
        t["end"] = i
        t["hashes"].append(hash_of(m))
        t["tokens"] += int(tokens_of(m))
        if not t["preview"] and isinstance(m, dict) and m.get("role") == "user":
            t["preview"] = " ".join(message_text(m).split())[:PREVIEW_CHARS]
    return turns


def chain(hashes: List[str]) -> List[str]:
    """chain[i] = digest of hashes[:i] (so chain[0] is the empty prefix)."""
    out, h = [], hashlib.sha256(b"cg-fold-chain")
    for x in hashes:
        out.append(h.hexdigest()[:16])
        h.update(x.encode("utf-8"))
    out.append(h.hexdigest()[:16])
    return out


# ---- the per-session fold store ----------------------------------------------------------

class FoldStore:
    """Folds by session, in memory only. Updates are synchronous (no await inside), so a fold is
    committed whole or not at all."""

    def __init__(self, max_sessions: int = SESSIONS_MAX) -> None:
        self.max_sessions = max_sessions
        self._s: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()

    def _rec(self, sid: str) -> Dict[str, Any]:
        rec = self._s.pop(sid, None) or {"next": 1, "folds": collections.OrderedDict()}
        self._s[sid] = rec
        while len(self._s) > self.max_sessions:
            self._s.popitem(last=False)
        return rec

    def next_id(self, sid: str) -> str:
        rec = self._rec(sid)
        fid = "f%d" % rec["next"]
        rec["next"] += 1
        return fid

    def live(self, sid: str) -> List[Dict[str, Any]]:
        rec = self._s.get(sid)
        return list(rec["folds"].values()) if rec else []

    def commit(self, sid: str, adds: List[Dict[str, Any]], cancels: List[str]) -> None:
        rec = self._rec(sid)
        for fid in cancels:
            rec["folds"].pop(fid, None)
        for fold in adds:
            if fold["id"] not in cancels:
                rec["folds"][fold["id"]] = fold

    def drop(self, sid: str, fid: str) -> None:
        rec = self._s.get(sid)
        if rec:
            rec["folds"].pop(fid, None)

    def clear(self, sid: str) -> int:
        rec = self._s.get(sid)
        n = len(rec["folds"]) if rec else 0
        if rec:
            rec["folds"].clear()
        return n

    def sessions(self) -> int:
        return len(self._s)

    def total(self) -> int:
        return sum(len(r["folds"]) for r in self._s.values())


def locate(fold: Dict[str, Any], hashes: List[str], prefix_chain: List[str]) -> bool:
    """Is the fold's run in `hashes` (the client's non-system message hashes) exactly where it was
    recorded, with the same turns before it?"""
    pos, run = fold["pos"], fold["hashes"]
    return (pos + len(run) <= len(hashes) and hashes[pos:pos + len(run)] == run
            and prefix_chain[pos] == fold["anchor"])


# ---- one request's view and the four tools ----------------------------------------------------

class Context:
    """What the tools need to know about the request being answered."""

    def __init__(self, *, session: str, store: FoldStore, non_system: List[Dict[str, Any]],
                 forwarded_hashes: set, hash_of: Callable[[Any], str], tokens_of: Callable[[Any], int],
                 protect_turns: int, keep_messages: int, max_folds: int, used_tokens: int, window: int,
                 budget: int, forwarded_messages: List[Dict[str, Any]], applied: List[str]) -> None:
        self.session, self.store = session, store
        self.hash_of, self.tokens_of = hash_of, tokens_of
        self.hashes = [hash_of(m) for m in non_system]
        self.chain = chain(self.hashes)
        self.turns = segment_turns(non_system, hash_of, tokens_of)
        self.max_folds = max_folds
        self.used, self.window, self.budget = used_tokens, window, budget
        self.forwarded = forwarded_messages
        self.applied = set(applied)             # fold ids whose turns are folded in the forwarded view
        self.pending: List[Dict[str, Any]] = []
        self.cancels: List[str] = []
        self.events: List[Dict[str, Any]] = []   # audit records the proxy writes to the log (even for a discarded loop)
        self.calls = 0
        self.protected = protected_turns(self.turns, len(non_system), protect_turns, keep_messages)
        self.fold_of_turn: Dict[int, str] = {}
        for f in store.live(session):
            a, b = f["turns"]
            for n in range(a, b + 1):
                self.fold_of_turn[n] = f["id"]
        self.forwarded_hashes = forwarded_hashes

    # -- state of a turn
    def state(self, t: Dict[str, Any]) -> str:
        n = t["n"]
        if n in self.fold_of_turn:
            fid = self.fold_of_turn[n]
            return "folded %s" % fid + ("" if fid in self.applied else " (pending)")
        for p in self.pending:
            if p["turns"][0] <= n <= p["turns"][1]:
                return "folded %s (proposed)" % p["id"]
        if n in self.protected:
            return "protected"
        if all(h in self.forwarded_hashes for h in t["hashes"]):
            return "visible"
        return "summarised"

    # -- the tools
    def status(self) -> str:
        lines = ["context: ~%d of %d tokens used (compaction budget %d)" % (self.used, self.window, self.budget)]
        live = self.store.live(self.session)
        for f in live:
            lines.append("fold %s: turns T%d-T%d, ~%d tokens%s" % (
                f["id"], f["turns"][0], f["turns"][1], f["tokens"], "" if f["id"] in self.applied else " (applies next request)"))
        for p in self.pending:
            lines.append("fold %s: turns T%d-T%d proposed this request" % (p["id"], p["turns"][0], p["turns"][1]))
        ranges, cur = [], None
        for t in self.turns:
            ok = self.state(t) == "visible"
            if ok:
                cur = [t["n"], t["n"], t["tokens"]] if cur is None else [cur[0], t["n"], cur[2] + t["tokens"]]
            elif cur is not None:
                ranges.append(cur)
                cur = None
        if cur is not None:
            ranges.append(cur)
        ranges = [r for r in ranges if r[2] >= MIN_FOLD_TOKENS]
        lines.append("foldable ranges (>= %d tokens): %s" % (MIN_FOLD_TOKENS, ", ".join(
            "T%d-T%d ~%d tok" % (a, b, t) for a, b, t in ranges) or "none"))
        lines.append("turns:")
        shown = []
        for t in self.turns:
            shown.append("T%d %d tok %s %s" % (t["n"], t["tokens"], self.state(t), t["preview"]))
        text = "\n".join(lines) + "\n" + "\n".join(shown)
        if len(text) > STATUS_MAX_CHARS:                       # elide the middle, never the header
            head = "\n".join(lines) + "\n"
            room = STATUS_MAX_CHARS - len(head) - 40
            body = "\n".join(shown)
            text = head + body[:room // 2] + "\n... (turns elided) ...\n" + body[-(room // 2):]
        return text

    def search(self, query: Any) -> str:
        words = [w for w in str(query or "").lower().split() if w]
        if not words:
            return "give some words to look for"
        by_hash = {h: t["n"] for t in self.turns for h in t["hashes"]}
        hits = []
        for i, m in enumerate(self.forwarded):
            text = message_text(m)
            low = text.lower()
            score = sum(1 for w in words if w in low)
            if not score:
                continue
            at = low.find(next(w for w in words if w in low))
            snippet = " ".join(text[max(0, at - 60):at + 100].split())
            turn = by_hash.get(self.hash_of(m))
            hits.append((score, i, "%s [%s] %s" % ("T%d" % turn if turn else "summary", m.get("role", "?"), snippet)))
        hits.sort(key=lambda h: (-h[0], h[1]))
        out, used = [], 0
        for _, _, line in hits[:SEARCH_MAX_HITS]:
            if used + len(line) > SEARCH_MAX_CHARS:
                break
            out.append(line)
            used += len(line)
        return "\n".join(out) if out else "no match in the turns and summaries currently in context"

    def compress(self, args: Any) -> Tuple[bool, str]:
        a, b = _int(args.get("from_turn")), _int(args.get("to_turn"))
        if a is None or b is None:
            return False, "from_turn and to_turn must be whole numbers"
        if not (1 <= a <= b <= len(self.turns)):
            return False, "turns run from 1 to %d, and from_turn must not exceed to_turn" % len(self.turns)
        if self.pending:
            return False, "only one fold can be proposed per request; ask again on your next request"
        if len(self.store.live(self.session)) + len(self.pending) >= self.max_folds:
            return False, "this session already has %d folds; cancel one with guardian_decompress first" % self.max_folds
        total = 0
        for t in self.turns[a - 1:b]:
            st = self.state(t)
            if st != "visible":
                why = {"protected": "protected (the first turn, the most recent turns, or the kept window)",
                       "summarised": "already summarised by automatic compaction"}.get(st, st)
                return False, "turn T%d cannot be folded: %s" % (t["n"], why)
            total += t["tokens"]
        if total < MIN_FOLD_TOKENS:
            return False, "T%d-T%d is only ~%d tokens; a fold needs at least %d to be worth it" % (a, b, total, MIN_FOLD_TOKENS)
        first, last = self.turns[a - 1], self.turns[b - 1]
        run = self.hashes[first["start"]:last["end"] + 1]
        fold = {"id": self.store.next_id(self.session), "turns": (a, b), "pos": first["start"], "hashes": run,
                "anchor": self.chain[first["start"]], "tokens": total, "count": len(run),
                "summary": None, "span": None, "pointer": "", "pins": [], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.pending.append(fold)
        self.events.append({"tool": T_COMPRESS, "ok": True, "fold": fold["id"], "turns": [a, b], "est_tokens": total})
        return True, ("Fold %s accepted: turns T%d-T%d (~%d tokens). The system will summarise them and the fold takes "
                      "effect on your next request. Use guardian_decompress(\"%s\") to cancel it."
                      % (fold["id"], a, b, total, fold["id"]))

    def decompress(self, args: Any) -> Tuple[bool, str]:
        fid = str(args.get("fold_id") or "").strip()
        known = [f["id"] for f in self.store.live(self.session)] + [p["id"] for p in self.pending]
        if fid not in known:
            return False, "unknown fold %r; folds in this session: %s" % (fid, ", ".join(known) or "none")
        if fid not in self.cancels:
            self.cancels.append(fid)
        self.events.append({"tool": T_DECOMPRESS, "ok": True, "fold": fid})
        return True, "Fold %s cancelled; its turns come back unchanged on your next request." % fid

    def call(self, name: str, raw_args: Any) -> Tuple[bool, str]:
        """Run one proxy tool call. Never raises: a failure is a rejected call with a reason."""
        self.calls += 1
        try:
            if isinstance(raw_args, str):
                args = json.loads(raw_args) if raw_args.strip() else {}
            else:
                args = raw_args if isinstance(raw_args, dict) else {}
            if not isinstance(args, dict):
                return self._reject(name, "arguments must be a JSON object")
            if name == T_STATUS:
                self.events.append({"tool": name, "ok": True})
                return True, self.status()
            if name == T_SEARCH:
                self.events.append({"tool": name, "ok": True})
                return True, self.search(args.get("query"))
            if name == T_COMPRESS:
                ok, text = self.compress(args)
            elif name == T_DECOMPRESS:
                ok, text = self.decompress(args)
            else:
                return self._reject(name, "unknown tool")
            if not ok:
                self.events.append({"tool": name, "ok": False, "reason": text[:200]})
            return ok, text
        except ValueError:
            return self._reject(name, "arguments are not valid JSON")
        except Exception as exc:                           # noqa: BLE001
            return self._reject(name, "could not run: %s" % type(exc).__name__)

    def _reject(self, name: str, reason: str) -> Tuple[bool, str]:
        self.events.append({"tool": name, "ok": False, "reason": reason})
        return False, reason


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


# ---- a finished completion as a stream ------------------------------------------------------------

def sse_from_completion(data: Dict[str, Any], include_usage: bool = False) -> bytes:
    """Re-frame a non-streaming chat completion as a short OpenAI-style SSE stream: one chunk that
    carries the whole message, one that finishes it, usage if asked, [DONE]."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    base = {"id": data.get("id") or "chatcmpl-guardian", "object": "chat.completion.chunk",
            "created": data.get("created") or int(time.time()), "model": data.get("model") or ""}
    delta: Dict[str, Any] = {"role": "assistant"}
    if msg.get("content") is not None:
        delta["content"] = msg["content"]
    for key in ("reasoning_content", "reasoning", "refusal"):
        if msg.get(key):
            delta[key] = msg[key]
    calls = msg.get("tool_calls")
    if calls:
        delta["tool_calls"] = [dict(c, index=i) for i, c in enumerate(calls) if isinstance(c, dict)]
    frames = [dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
              dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}])]
    if include_usage and isinstance(data.get("usage"), dict):
        frames.append(dict(base, choices=[], usage=data["usage"]))
    out = b"".join(b"data: " + json.dumps(f, ensure_ascii=False).encode("utf-8") + b"\n\n" for f in frames)
    return out + b"data: [DONE]\n\n"
