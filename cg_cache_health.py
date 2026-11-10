"""Cache-health ledger (GUARDIAN_CACHE_HEALTH): what compaction does to the reusable prefix.

Providers with prefix caching bill (or compute) the leading part of a request cheaply when
it is identical to the previous request's. A compaction rewrites the history, so it can lose
that. This module keeps, per session, a fingerprint of the last request the upstream saw and
a bounded list of rows, one per real compaction, saying how much of the previous request's
prefix survived and, when the upstream reports cached tokens, the observed cache hit rate.

Pure and standard-library only: it never touches a request or a response, holds no message
text (only short digests and counts), and raises nothing the caller cannot catch. The proxy
calls it after the response has been delivered (ADR 0008).
"""
import collections
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

Element = Tuple[str, int]               # (12-hex digest of the canonical JSON, estimated tokens)

SYSTEM_CHUNK_CHARS = 4096               # a string `system` is cut so an appended note keeps the head shared
REAL_SOURCES = ("generated", "extended")
LAST_ROWS = 5


def digest(obj: Any) -> str:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def fingerprint(payload: Dict[str, Any], route: str,
                est_message: Callable[[Any], int], est_tools: Callable[[Any], int],
                est_text: Callable[[Any], int]) -> List[Element]:
    """Ordered elements of a forwarded request, in the order providers cache them: tools, the
    Anthropic `system` field, then the messages. Token figures are the proxy's estimates."""
    out: List[Element] = []
    tools = payload.get("tools")
    if tools:
        out.append((digest(tools), int(est_tools(tools))))
    if route == "messages":
        system = payload.get("system")
        if isinstance(system, str):
            for i in range(0, len(system), SYSTEM_CHUNK_CHARS):
                chunk = system[i:i + SYSTEM_CHUNK_CHARS]
                out.append((digest(chunk), int(est_text(chunk))))
        elif isinstance(system, list):
            for block in system:
                out.append((digest(block), int(est_text(block))))
    for m in payload.get("messages") or []:
        out.append((digest(m), int(est_message(m))))
    return out


def shared_prefix(prev: List[Element], new: List[Element]) -> Tuple[int, int]:
    """(leading elements, their estimated tokens) that `prev` and `new` have in common."""
    n = tokens = 0
    for a, b in zip(prev, new):
        if a[0] != b[0]:
            break
        n += 1
        tokens += a[1]
    return n, tokens


def _rate(prompt: Optional[int], cached: Optional[int]) -> Optional[float]:
    if not isinstance(prompt, int) or not isinstance(cached, int) or prompt <= 0 or cached < 0:
        return None
    return round(min(1.0, cached / prompt), 4)


class _Session:
    def __init__(self) -> None:
        self.prev: Optional[List[Element]] = None
        self.seq = 0
        self.rows: "collections.deque" = collections.deque()
        self.compactions = 0
        self.reused = 0
        self.spills = 0
        self.breaks = 0
        self.reports = False            # the upstream has reported cached-token counts for this session
        self.hit: Optional[Tuple[int, float]] = None     # (seq, rate) of the latest request with a rate


class CacheHealth:
    def __init__(self, head_tokens: int = 1024, max_sessions: int = 64, max_rows: int = 50) -> None:
        self.head_tokens = max(1, int(head_tokens))
        self.max_sessions = max(1, int(max_sessions))
        self.max_rows = max(1, int(max_rows))
        self._sessions: "collections.OrderedDict[str, _Session]" = collections.OrderedDict()
        self.errors = 0
        self.skipped_large = 0

    # -- feeding -------------------------------------------------------------------
    def observe(self, session: str, elements: List[Element], *, route: str = "chat",
                source: Optional[str] = None, spill: bool = False, at: str = "") -> int:
        """Record the request the upstream just answered. `source` is the summary memo source
        when this request was compacted ("generated", "extended" or "reused"), else None.
        Returns the request's sequence number in its session (the key usage is filed under)."""
        rec = self._sessions.pop(session, None) or _Session()
        self._sessions[session] = rec
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        rec.seq += 1
        seq = rec.seq
        prev = rec.prev
        if prev is not None and prev:
            n, shared = shared_prefix(prev, elements)
            # A row is a compaction that really rewrote the prefix: a new or extended summary
            # always did; a reused summary did only when the request's front differs from the
            # last one we recorded (the compacted request was not recorded, or another session
            # filled the memo).
            if source in REAL_SOURCES or (source == "reused" and n < len(prev)):
                rec.compactions += 1
                rec.rows.append(self._row(rec, seq, route, source, at, prev, elements, n, shared))
                while len(rec.rows) > self.max_rows:
                    rec.rows.popleft()
            elif source == "reused":
                rec.reused += 1
            elif spill:
                rec.spills += 1
            elif n < len(prev):
                rec.breaks += 1
        rec.prev = elements
        return seq

    def _row(self, rec: _Session, seq: int, route: str, source: str, at: str,
             prev: List[Element], new: List[Element], n: int, shared: int) -> Dict[str, Any]:
        before = sum(t for _, t in prev)
        after = sum(t for _, t in new)
        head_n = min(self.head_tokens, before)
        k, running = 0, 0
        for _, t in prev:
            if running >= head_n:
                break
            running += t
            k += 1
        hit_before = rec.hit[1] if rec.hit and rec.hit[0] == seq - 1 else None
        return {
            "seq": seq, "at": at, "route": route, "source": source,
            "before_tokens": before, "after_tokens": after,
            "shared_tokens": shared, "shared_pct": round(100.0 * shared / before, 1) if before else 0.0,
            "head_n": head_n, "head_identical": n >= k,
            "head_hash_before": hashlib.sha256("".join(d for d, _ in prev[:k]).encode()).hexdigest()[:8],
            "head_hash_after": hashlib.sha256("".join(d for d, _ in new[:k]).encode()).hexdigest()[:8],
            "hit_before": hit_before, "hit_at": None,
        }

    def forget_previous(self, session: str) -> None:
        """Drop the remembered previous request (it was not recorded, so it is stale)."""
        rec = self._sessions.get(session)
        if rec is not None:
            rec.prev = None

    def usage(self, session: str, seq: int, prompt: Optional[int], cached: Optional[int]) -> None:
        """File the upstream's reported prompt/cached counts under the request they belong to."""
        rec = self._sessions.get(session)
        if rec is None:
            return
        if cached is not None:
            rec.reports = True
        rate = _rate(prompt, cached)
        if rate is None:
            return
        if rec.hit is None or seq >= rec.hit[0]:
            rec.hit = (seq, rate)
        for row in rec.rows:
            if row["seq"] == seq:
                row["hit_at"] = rate

    # -- reading -------------------------------------------------------------------
    def prefix_pct(self, session: str) -> Optional[int]:
        """Mean share of the previous prefix kept over the session's last compactions, or None."""
        rec = self._sessions.get(session)
        rows = list(rec.rows)[-LAST_ROWS:] if rec else []
        if not rows:
            return None
        return int(round(sum(r["shared_pct"] for r in rows) / len(rows)))

    def line(self, session: Optional[str] = None) -> str:
        sid = session or (next(reversed(self._sessions)) if self._sessions else None)
        rec = self._sessions.get(sid) if sid is not None else None
        rows = list(rec.rows)[-LAST_ROWS:] if rec else []
        if not rows:
            return "no compaction measured yet"
        n = len(rows)
        kept = int(round(sum(r["shared_pct"] for r in rows) / n))
        heads = {r["head_n"] for r in rows}
        head = ("first {:,} tokens".format(heads.pop()) if len(heads) == 1 else "leading tokens")
        ident = sum(1 for r in rows if r["head_identical"])
        text = "last %d compaction%s kept %d%% of the prefix (%s identical in %d/%d)" % (
            n, "" if n == 1 else "s", kept, head, ident, n)
        before = [r["hit_before"] for r in rows if r["hit_before"] is not None]
        at = [r["hit_at"] for r in rows if r["hit_at"] is not None]
        if at or before:
            parts = []
            if before:
                parts.append("%d%% before" % round(100 * sum(before) / len(before)))
            if at:
                parts.append("%d%% on the compacted request" % round(100 * sum(at) / len(at)))
            text += "; cache hit rate " + ", ".join(parts)
        elif not rec.reports:
            text += "; no cached-token counts were seen (the upstream may report none, or a stream carried no usage frame), so only prefix stability is shown"
        text += " (token counts are estimates"
        if any(r["route"] == "messages" for r in rows):
            text += "; hosted Anthropic caches only up to the client's cache_control breakpoints"
        return text + ")"

    def summary(self) -> Dict[str, Any]:
        recs = list(self._sessions.items())
        latest_id, latest = (recs[-1] if recs else (None, None))
        out: Dict[str, Any] = {
            "enabled": True, "sessions": len(recs),
            "compactions": sum(r.compactions for _, r in recs),
            "reused_turns": sum(r.reused for _, r in recs),
            "spills": sum(r.spills for _, r in recs),
            "prefix_breaks_without_compaction": sum(r.breaks for _, r in recs),
            "reports_cached_tokens": any(r.reports for _, r in recs),
            "errors": self.errors, "skipped_large": self.skipped_large,
            "line": self.line(latest_id) if latest is not None else "no request measured yet",
            "latest_session": None,
        }
        if latest is not None:
            rows = [dict(r) for r in list(latest.rows)[-LAST_ROWS:]]
            for r in rows[:-1]:                      # hashes only for the last row
                r.pop("head_hash_before", None)
                r.pop("head_hash_after", None)
            out["latest_session"] = {"id": latest_id, "rows": rows}
        return out
