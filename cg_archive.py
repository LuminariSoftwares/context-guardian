#!/usr/bin/env python3
"""cg_archive.py -- seal the span archive so "lossless on disk" can be checked.

The README has promised "lossy in context, lossless on disk" since 0.3.0, and
nothing could check it: a span file edited, truncated or deleted after the fact
looked exactly like one that never changed. This module makes the promise
checkable.

Every file the archive writes gets one line in <span_dir>/<run_id>/chain.jsonl:

    {"seq": 3, "type": "span", "file": "0003.json", "sha256": "<hex of the bytes on disk>",
     "prev": "<entry_hash of line 2>", "ts": "2026-10-23T12:00:00-05:00",
     "entry_hash": "<sha256 of the canonical JSON of every other field>"}

Line 1's prev is "genesis:<run_id>". Retention pruning appends a chained
{"type": "pruned"} line, so a pruned span reads as expected rather than as a
deletion. `verify_run` walks a chain and reports OK or the first break.

TAMPER-EVIDENT, NOT TAMPER-PROOF. Anyone who can write the directory can edit
a span and rehash every line after it, and the chain alone cannot tell. The
answer is to keep a copy of each head somewhere that person cannot reach:
GUARDIAN_SEAL_ANCHOR_PATH appends every new head to a file meant to live
outside the data directory, and the verifier checks the chain against it.

The exact formats are in docs/ARCHIVE_FORMAT.md, precisely enough to write an
independent verifier; cg_seal.js writes the same chain from the DSH engine.

Never raises into a caller on the write side: every append is best-effort and
returns None on failure, so the proxy's fail-open contract holds. Stdlib only,
Python 3.10+, Windows and POSIX. MIT licensed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CHAIN_FILE = "chain.jsonl"
LOCK_FILE = "chain.lock"
GENESIS_PREFIX = "genesis:"
SPAN_NAME_RE = re.compile(r"^\d{4,}\.json$")
JOURNAL_NAME_RE = re.compile(r"^journal(?:\.\d+)?\.jsonl$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# The kinds of line a chain may hold. A verifier that meets anything else
# reports the line as edited rather than guessing what it meant.
ENTRY_TYPES = ("span", "journal", "pruned")

# How long an append waits for another writer before it gives up and logs. The
# proxy holds this lock for one small append, so contention lasts milliseconds;
# the ceiling only bounds a crashed writer that left its lock file behind.
LOCK_TIMEOUT_SECONDS = 2.0
# A lock file older than this belongs to a process that died holding it.
LOCK_STALE_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.005
# Enough to hold the last line of any chain: an entry is ~300 bytes.
TAIL_BYTES = 8192

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# One in-process lock per chain file. The lock FILE stops other processes (and
# the DSH engine, which takes the same file); this stops threads in this one
# from spinning against each other on it.
_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class SealError(Exception):
    """An append that could not be made. Callers on the request path catch it."""


# --- primitives ----------------------------------------------------------------

def canonical(obj: Dict[str, Any]) -> bytes:
    """The bytes an entry is hashed over: keys sorted, no whitespace, ASCII.

    ensure_ascii so that a verifier in any language produces the same bytes
    without having to agree on how to serialise non-ASCII text. Every value a
    chain carries is ASCII by construction (file names, hex digests, ISO
    timestamps, run ids matching [A-Za-z0-9_-]), so nothing is escaped in
    practice; the rule is there so the format stays well defined.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def entry_hash(fields: Dict[str, Any]) -> str:
    """sha256 over the canonical JSON of every field except entry_hash."""
    body = {k: v for k, v in fields.items() if k != "entry_hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex digest of a file's bytes, read in binary: what is on disk, not what
    some caller believed it wrote."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def now_iso() -> str:
    """Local time with its UTC offset, e.g. 2026-10-23T12:00:00-05:00."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def genesis(run_id: str) -> str:
    return GENESIS_PREFIX + run_id


def _make_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def fsync_dir(path: Path) -> None:
    """Make a rename durable on POSIX. Windows has no directory fsync and
    commits the rename with the file; nothing to do there."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to a temp file beside `path`, fsync it, os.replace it into
    place. Replacing an existing file (a claimed placeholder) is fine on both
    Windows and POSIX. Raises on failure, leaving no temp file behind."""
    path = Path(path)
    tmp = path.with_name(".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                     PRIVATE_FILE_MODE)
        try:
            _make_private(tmp, PRIVATE_FILE_MODE)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        replace_file(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def replace_file(src: Path, dst: Path) -> None:
    # On Windows a reader holding `dst` open makes the replace fail with a
    # sharing violation for as long as it reads; a few short retries ride
    # that out instead of losing the file.
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


# --- the lock ------------------------------------------------------------------

class ChainLock:
    """An exclusive lock on one run's chain, shared with the DSH engine.

    A lock FILE created with O_EXCL, not fcntl/msvcrt: those differ per OS and
    Node has neither, while an exclusive create means the same thing to Python
    and to Node on Windows, macOS and Linux. A lock file older than
    LOCK_STALE_SECONDS was left by a process that died holding it and is
    broken. Waiting longer than `timeout` raises SealError: an append that
    cannot get the lock is skipped and logged, never allowed to stall a request.
    """

    def __init__(self, run_dir: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = Path(run_dir) / LOCK_FILE
        self.timeout = timeout
        key = os.path.abspath(str(self.path))
        with _thread_locks_guard:
            self._thread_lock = _thread_locks.setdefault(key, threading.Lock())
        self._held = False

    def __enter__(self) -> "ChainLock":
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise SealError("timed out waiting for %s" % self.path)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 PRIVATE_FILE_MODE)
                except FileExistsError:
                    self._break_if_stale()
                    if time.monotonic() >= deadline:
                        raise SealError("timed out waiting for %s" % self.path)
                    time.sleep(LOCK_POLL_SECONDS)
                    continue
                try:
                    os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                self._held = True
                return self
        except BaseException:
            self._thread_lock.release()
            raise

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age > LOCK_STALE_SECONDS:
            try:
                self.path.unlink()
            except OSError:
                pass

    def __exit__(self, *exc) -> None:
        try:
            if self._held:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                self._held = False
        finally:
            self._thread_lock.release()


# --- writing -------------------------------------------------------------------

def _last_entry(chain_path: Path) -> Optional[Dict[str, Any]]:
    """The chain's last line, parsed, or None for an empty or absent chain.
    Raises SealError when the last line is not a sealed entry: appending after
    a damaged tail would chain onto something the verifier cannot trust."""
    try:
        size = chain_path.stat().st_size
    except FileNotFoundError:
        return None
    if size == 0:
        return None
    with open(chain_path, "rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read()
    lines = [ln for ln in tail.split(b"\n") if ln.strip()]
    if not lines:
        return None
    try:
        last = json.loads(lines[-1].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SealError("the last line of %s is not JSON" % chain_path) from exc
    if (not isinstance(last, dict) or not isinstance(last.get("seq"), int)
            or not isinstance(last.get("entry_hash"), str)):
        raise SealError("the last line of %s is not a chain entry" % chain_path)
    return last


def _append_line(path: Path, data: bytes) -> None:
    new_file = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                 PRIVATE_FILE_MODE)
    try:
        if new_file:
            _make_private(path, PRIVATE_FILE_MODE)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def append_anchor(anchor_path: Path, run_id: str, entry: Dict[str, Any]) -> None:
    """Record a new head in the anchor file. Best-effort by design: the anchor
    is an extra copy, and failing to write it must not cost the chain line that
    was already written."""
    line = {"run": run_id, "seq": entry["seq"], "head": entry["entry_hash"], "ts": entry["ts"]}
    anchor_path = Path(anchor_path)
    missing = [p for p in (anchor_path.parent, *anchor_path.parent.parents) if not p.exists()]
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    for made in missing:
        _make_private(made, PRIVATE_DIR_MODE)
    _append_line(anchor_path, canonical(line) + b"\n")


def append_locked(run_dir: Path, run_id: str, fields: Dict[str, Any],
                  anchor_path: Optional[Path] = None) -> Dict[str, Any]:
    """Append one entry. The caller holds ChainLock(run_dir). Raises on failure."""
    chain_path = Path(run_dir) / CHAIN_FILE
    last = _last_entry(chain_path)
    entry: Dict[str, Any] = {k: v for k, v in fields.items()
                             if k not in ("seq", "prev", "entry_hash", "ts")}
    entry["seq"] = 1 if last is None else last["seq"] + 1
    entry["prev"] = genesis(run_id) if last is None else last["entry_hash"]
    entry["ts"] = now_iso()
    entry["entry_hash"] = entry_hash(entry)
    _append_line(chain_path, canonical(entry) + b"\n")
    if anchor_path:
        try:
            append_anchor(Path(anchor_path), run_id, entry)
        except OSError:
            entry["_anchor_failed"] = True
    return entry


def append_entry(run_dir: Path, run_id: str, fields: Dict[str, Any],
                 anchor_path: Optional[Path] = None,
                 timeout: float = LOCK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Lock, append one entry, unlock. Raises SealError/OSError on failure;
    callers on the request path catch everything."""
    with ChainLock(run_dir, timeout=timeout):
        return append_locked(run_dir, run_id, fields, anchor_path)


def head(run_dir: Path) -> Optional[Dict[str, Any]]:
    """The last entry of a run's chain, or None. Never raises."""
    try:
        return _last_entry(Path(run_dir) / CHAIN_FILE)
    except (OSError, SealError):
        return None


# --- reading and verifying -----------------------------------------------------

@dataclass
class RunReport:
    run: str
    status: str                     # "ok" | "break" | "unsealed" | "empty"
    kind: Optional[str] = None      # break kind, see BREAK_KINDS
    file: Optional[str] = None
    seq: Optional[int] = None
    detail: str = ""
    head: Optional[str] = None
    head_seq: int = 0
    sealed: int = 0
    pruned: List[str] = field(default_factory=list)
    backfilled: int = 0
    unsealed: List[str] = field(default_factory=list)
    anchored: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "empty")


BREAK_KINDS = {
    "content-changed": "the file's bytes no longer match the hash sealed for them",
    "file-missing": "a sealed file is gone and no prune record says why",
    "entry-edited": "a chain line was changed after it was written",
    "entry-reordered": "chain lines are out of order, or one was removed",
    "chain-forked": "two lines claim the same place in the chain",
    "unsealed-file": "a file sits in a sealed run with no chain line for it",
    "anchor-mismatch": "the chain disagrees with the head recorded in the anchor file",
    "unreadable-chain": "a chain line is not a sealed entry",
}


def archived_files(run_dir: Path) -> List[str]:
    """The files in a run that the chain is meant to cover, sorted: span files
    (NNNN.json) and journal files. Engine snapshots (precompact-*.json) and the
    sidecars (chain, index, lock) are not archive content."""
    try:
        names = [p.name for p in Path(run_dir).iterdir() if p.is_file()]
    except OSError:
        return []
    return sorted(n for n in names if SPAN_NAME_RE.match(n) or JOURNAL_NAME_RE.match(n))


def read_chain(run_dir: Path) -> Tuple[List[Dict[str, Any]], Optional[Tuple[int, str]]]:
    """(entries, problem). problem is (line number, message) for the first
    line that is not a JSON object; entries stop before it."""
    entries: List[Dict[str, Any]] = []
    path = Path(run_dir) / CHAIN_FILE
    with open(path, "rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return entries, (lineno, "line %d is not JSON" % lineno)
            if not isinstance(obj, dict):
                return entries, (lineno, "line %d is not a JSON object" % lineno)
            entries.append(obj)
    return entries, None


def read_anchor(anchor_path: Optional[Path]) -> Dict[str, List[Tuple[int, str]]]:
    """run -> [(seq, head), ...] from the anchor file. Missing file: {}.
    Unparseable lines are skipped: the anchor is evidence, and a torn last
    line from a crash must not hide every line before it."""
    out: Dict[str, List[Tuple[int, str]]] = {}
    if not anchor_path:
        return out
    try:
        with open(anchor_path, "rb") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(obj, dict) and isinstance(obj.get("run"), str)
                        and isinstance(obj.get("seq"), int) and isinstance(obj.get("head"), str)):
                    out.setdefault(obj["run"], []).append((obj["seq"], obj["head"]))
    except OSError:
        return {}
    return out


def _entry_shape_problem(e: Dict[str, Any]) -> Optional[str]:
    if not isinstance(e.get("seq"), int) or isinstance(e.get("seq"), bool):
        return "seq is not an integer"
    if e.get("type") not in ENTRY_TYPES:
        return "unknown entry type %r" % (e.get("type"),)
    if not isinstance(e.get("file"), str) or not e["file"]:
        return "no file"
    if not isinstance(e.get("sha256"), str) or not HEX64_RE.match(e["sha256"]):
        return "sha256 is not a 64-digit hex digest"
    for key in ("prev", "entry_hash", "ts"):
        if not isinstance(e.get(key), str):
            return "%s is missing" % key
    return None


def verify_run(run_dir: Path, anchor: Optional[Dict[str, List[Tuple[int, str]]]] = None) -> RunReport:
    """Walk one run's chain and its files. Reports OK or the FIRST break.

    Order of checks, which is also the order a reader should trust them in:
    the chain itself (every line well formed, hashed, consecutive and linked),
    then each sealed file against its hash, then files the chain does not
    mention, then the anchor.
    """
    run_dir = Path(run_dir)
    run = run_dir.name
    files = archived_files(run_dir)
    chain_path = run_dir / CHAIN_FILE
    if not chain_path.exists():
        if files:
            return RunReport(run, "unsealed", unsealed=files,
                             detail="%d file(s) and no chain: written before sealing existed, "
                                    "or with GUARDIAN_SEAL=0" % len(files))
        return RunReport(run, "empty")
    rep = RunReport(run, "ok")
    try:
        entries, problem = read_chain(run_dir)
    except OSError as exc:
        return RunReport(run, "break", kind="unreadable-chain", detail=str(exc))

    def brk(kind, detail, entry=None, file=None):
        rep.status, rep.kind, rep.detail = "break", kind, detail
        if entry is not None:
            rep.seq = entry.get("seq") if isinstance(entry.get("seq"), int) else None
            rep.file = entry.get("file") if isinstance(entry.get("file"), str) else None
        if file is not None:
            rep.file = file
        return rep

    # 1. The chain.
    seen_seq: Dict[int, Dict[str, Any]] = {}
    children: Dict[str, int] = {}
    hashes = {}
    prev_hash = genesis(run)
    for pos, e in enumerate(entries, start=1):
        shape = _entry_shape_problem(e)
        if shape:
            return brk("unreadable-chain", "line %d: %s" % (pos, shape), e)
        if e["seq"] in seen_seq:
            return brk("chain-forked", "two lines claim seq %d" % e["seq"], e)
        if e["prev"] in children:
            return brk("chain-forked", "seq %d chains onto the same line as seq %d"
                       % (e["seq"], children[e["prev"]]), e)
        if entry_hash(e) != e["entry_hash"]:
            return brk("entry-edited", "seq %d no longer matches its own entry_hash" % e["seq"], e)
        if e["seq"] != pos:
            return brk("entry-reordered", "line %d carries seq %d" % (pos, e["seq"]), e)
        if e["prev"] != prev_hash:
            if e["prev"] in hashes:
                return brk("entry-reordered", "seq %d points back past its predecessor" % e["seq"], e)
            return brk("entry-edited", "seq %d does not point at seq %d: that line was changed "
                       "and rehashed" % (e["seq"], e["seq"] - 1), e)
        seen_seq[e["seq"]] = e
        children[e["prev"]] = e["seq"]
        hashes[e["entry_hash"]] = e["seq"]
        prev_hash = e["entry_hash"]
    if problem:
        return brk("unreadable-chain", problem[1])
    if entries:
        rep.head, rep.head_seq = entries[-1]["entry_hash"], entries[-1]["seq"]

    # 2. Each sealed file against its hash, in the order it was sealed. The
    # LAST content entry for a file is the one that counts: a journal file is
    # appended to and resealed.
    sealed: Dict[str, Dict[str, Any]] = {}
    pruned: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        if e["type"] == "pruned":
            pruned[e["file"]] = e
        else:
            sealed[e["file"]] = e
            pruned.pop(e["file"], None)
            if e.get("backfilled") is True:
                rep.backfilled += 1
    rep.sealed = len(sealed)
    for name, e in sorted(sealed.items(), key=lambda kv: kv[1]["seq"]):
        if name in pruned:
            if pruned[name]["sha256"] != e["sha256"]:
                return brk("content-changed", "%s changed before it was pruned" % name, e)
            continue
        path = run_dir / name
        if not path.exists():
            return brk("file-missing", "%s is gone and no prune record says why" % name, e)
        try:
            digest = sha256_file(path)
        except OSError as exc:
            return brk("file-missing", "%s cannot be read: %s" % (name, exc), e)
        if digest != e["sha256"]:
            return brk("content-changed", "sha256 of %s on disk differs from the sealed hash" % name, e)
    rep.pruned = sorted(pruned)

    # 3. Files the chain does not mention.
    rep.unsealed = [n for n in files if n not in sealed]
    if rep.unsealed:
        return brk("unsealed-file", "%s has no chain line (a seal that failed, or a file added "
                   "later; --seal-existing seals it as backfilled)" % rep.unsealed[0],
                   file=rep.unsealed[0])

    # 4. The anchor: every head it recorded for this run must still be the
    # entry at that seq. A chain rewritten after the fact cannot reproduce a
    # head it did not have.
    if anchor and run in anchor:
        by_seq = {e["seq"]: e["entry_hash"] for e in entries}
        for seq, recorded in anchor[run]:
            if by_seq.get(seq) != recorded:
                return brk("anchor-mismatch",
                           "the anchor recorded head %s… at seq %d; the chain %s"
                           % (recorded[:12], seq,
                              "has %s… there" % by_seq[seq][:12] if seq in by_seq
                              else "has no seq %d" % seq))
        rep.anchored = True
    return rep


def run_dirs(span_dir: Path) -> List[Path]:
    """Run directories under the span dir, oldest name first."""
    try:
        return sorted(p for p in Path(span_dir).iterdir() if p.is_dir())
    except OSError:
        return []


def latest_run(span_dir: Path) -> Optional[Path]:
    """The run directory most recently written to (by mtime of its newest
    archived file, falling back to the directory's own)."""
    best, best_t = None, -1.0
    for d in run_dirs(span_dir):
        times = []
        for name in archived_files(d) + [CHAIN_FILE]:
            try:
                times.append((d / name).stat().st_mtime)
            except OSError:
                pass
        if not times:
            continue
        t = max(times)
        if t > best_t:
            best, best_t = d, t
    return best


def verify(span_dir: Path, run: Optional[str] = None, all_runs: bool = False,
           anchor_path: Optional[Path] = None) -> List[RunReport]:
    """Reports for the chosen runs: one run by id, every run, or (default) the
    most recently written run."""
    anchor = read_anchor(anchor_path) if anchor_path else None
    if run is not None:
        targets = [Path(span_dir) / run]
    elif all_runs:
        targets = run_dirs(span_dir)
    else:
        latest = latest_run(span_dir)
        targets = [latest] if latest else []
    return [verify_run(d, anchor) for d in targets if d.is_dir()]


def seal_existing(span_dir: Path, run: Optional[str] = None,
                  anchor_path: Optional[Path] = None) -> Dict[str, int]:
    """Seal every archived file no chain line mentions, marked backfilled.

    For archives written before sealing existed: from this point on they can
    be verified, and the label says honestly that the seal is late -- a
    backfilled hash proves the file has not changed SINCE the backfill, not
    that it matches what was first written.
    """
    out: Dict[str, int] = {}
    targets = [Path(span_dir) / run] if run else run_dirs(span_dir)
    for d in targets:
        if not d.is_dir():
            continue
        files = archived_files(d)
        if not files:
            continue
        with ChainLock(d):
            known = set()
            if (d / CHAIN_FILE).exists():
                entries, _ = read_chain(d)
                known = {e.get("file") for e in entries if isinstance(e, dict)}
            count = 0
            for name in files:
                if name in known:
                    continue
                append_locked(d, d.name, {"type": "span" if SPAN_NAME_RE.match(name) else "journal",
                                          "file": name, "sha256": sha256_file(d / name),
                                          "backfilled": True}, anchor_path)
                count += 1
        out[d.name] = count
    return out


def heads(span_dir: Path) -> Dict[str, Tuple[int, str]]:
    """run -> (seq, entry_hash) of each sealed run's last line."""
    out = {}
    for d in run_dirs(span_dir):
        last = head(d)
        if last is not None:
            out[d.name] = (last["seq"], last["entry_hash"])
    return out


def summarise(reports: Iterable[RunReport]) -> Dict[str, int]:
    """Counts for the receipts line and /guardian/stats."""
    reports = list(reports)
    return {"runs": len(reports),
            "ok": sum(1 for r in reports if r.status == "ok"),
            "breaks": sum(1 for r in reports if r.status == "break"),
            "unsealed": sum(1 for r in reports if r.status == "unsealed")}


def format_report(r: RunReport) -> str:
    """One human line per run, as `context-guardian-recall --verify` prints it."""
    if r.status == "ok":
        notes = ["%d sealed" % r.sealed]
        if r.pruned:
            notes.append("%d pruned as expected (%s)" % (len(r.pruned), ", ".join(r.pruned[:5])
                                                         + (", ..." if len(r.pruned) > 5 else "")))
        if r.backfilled:
            notes.append("%d backfilled (sealed late)" % r.backfilled)
        if r.anchored:
            notes.append("anchor agrees")
        return "%s  OK        %s; head %s" % (r.run, ", ".join(notes), (r.head or "")[:16])
    if r.status == "empty":
        return "%s  OK        nothing archived" % r.run
    if r.status == "unsealed":
        return ("%s  UNSEALED  %s; run context-guardian-recall --verify --seal-existing"
                % (r.run, r.detail))
    where = " at seq %d" % r.seq if r.seq is not None else ""
    return "%s  BREAK     %s%s: %s" % (r.run, r.kind, where, r.detail)
