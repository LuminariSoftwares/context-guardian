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

import collections
import datetime
import hashlib
import json
import math
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


def sha256_prefix(path: Path, size: int) -> str:
    """Hex digest of a file's first `size` bytes; a shorter file hashes what
    it has, so a truncated journal mismatches rather than raising."""
    h = hashlib.sha256()
    left = size
    with open(path, "rb") as fh:
        while left > 0:
            block = fh.read(min(left, 1 << 20))
            if not block:
                break
            h.update(block)
            left -= len(block)
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


def write_span_file(run_dir: Path, start_index: int, payload, probe: int = 50) -> Optional[Tuple[Path, int, Dict[str, Any]]]:
    """Write one span file: claim the first free NNNN.json at or after
    start_index with an exclusive create, write payload(index) to a temp file,
    fsync it and rename it over the claim. Returns (path, index, payload), or
    None when no index within `probe` is free. Raises when the write fails,
    after removing the claim and the temp file, so the index is free again.

    The claim is what stops two concurrent writers computing the same index
    from clobbering each other; the temp file and rename are what make the
    bytes on disk final before they are sealed.
    """
    run_dir = Path(run_dir)
    path = None
    for candidate in range(start_index, start_index + probe):
        attempt = run_dir / ("%04d.json" % candidate)
        try:
            with open(attempt, "x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        _make_private(attempt, PRIVATE_FILE_MODE)
        path, index = attempt, candidate
        break
    if path is None:
        return None
    tmp = run_dir / (".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        body = payload(index)
        with open(tmp, "x", encoding="utf-8") as fh:
            _make_private(tmp, PRIVATE_FILE_MODE)
            json.dump(body, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(tmp, path)
        fsync_dir(run_dir)
    except BaseException:
        for leftover in (tmp, path):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return path, index, body


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

    def __init__(self, run_dir: Path, timeout: float = LOCK_TIMEOUT_SECONDS, name: str = LOCK_FILE):
        self.path = Path(run_dir) / name
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


def _append_line(path: Path, data: bytes, durable: bool = True) -> None:
    new_file = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                 PRIVATE_FILE_MODE)
    try:
        if new_file:
            _make_private(path, PRIVATE_FILE_MODE)
        os.write(fd, data)
        if durable:
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
    # (journal file, bytes written after its last seal): covered by nothing,
    # reported, and not a break -- a journal is sealed in batches.
    tails: List[Tuple[str, int]] = field(default_factory=list)

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
        size = e.get("size") if e["type"] == "journal" else None
        try:
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                digest = sha256_prefix(path, size)
                extra = path.stat().st_size - size
            else:
                digest, extra = sha256_file(path), 0
        except OSError as exc:
            return brk("file-missing", "%s cannot be read: %s" % (name, exc), e)
        if digest != e["sha256"]:
            return brk("content-changed", "sha256 of %s on disk differs from the sealed hash" % name, e)
        if extra > 0:
            rep.tails.append((name, extra))
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
        for name, extra in r.tails:
            notes.append("%s: %d bytes after the last seal, not covered yet" % (name, extra))
        return "%s  OK        %s; head %s" % (r.run, ", ".join(notes), (r.head or "")[:16])
    if r.status == "empty":
        return "%s  OK        nothing archived" % r.run
    if r.status == "unsealed":
        return ("%s  UNSEALED  %s; run context-guardian-recall --verify --seal-existing"
                % (r.run, r.detail))
    where = " at seq %d" % r.seq if r.seq is not None else ""
    return "%s  BREAK     %s%s: %s" % (r.run, r.kind, where, r.detail)


# --- receipts --------------------------------------------------------------------
#
# Real numbers instead of adjectives: how much was archived, how often anything
# was recalled, how much of the archive was read back. One counts-only JSON line
# per event in <data dir>/logs/receipts.jsonl, local only.
#
# PRIVACY BY CONSTRUCTION. A receipt may carry a tool name from the fixed list
# below, a timestamp, and integers or booleans -- nothing else is written, so no
# message text, query text, file path or run id can reach the file whatever a
# caller passes. tests/test_receipts.py plants a unique string in all four and
# asserts it never appears.

RECEIPT_TOOLS = frozenset((
    "cli.search", "mcp.list_spans", "mcp.search_spans", "mcp.read_span",
    "mcp.list_digests", "mcp.get_seal_status", "proxy.compaction", "proxy.recall_seen",
))
RECALL_FIELDS = ("query_terms", "spans_matched", "spans_opened", "chars_returned")
_OFF = ("0", "false", "no", "off")
_SINCE_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)
_SINCE_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def receipts_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """GUARDIAN_RECEIPTS, read at call time. Default on."""
    env = os.environ if env is None else env
    return (env.get("GUARDIAN_RECEIPTS") or "1").strip().lower() not in _OFF


def receipts_path(env: Optional[Dict[str, str]] = None) -> Path:
    """GUARDIAN_RECEIPTS_PATH, else <data dir>/logs/receipts.jsonl -- under logs/
    so a checkout's .gitignore already covers it."""
    env = os.environ if env is None else env
    chosen = (env.get("GUARDIAN_RECEIPTS_PATH") or "").strip()
    if chosen:
        return Path(chosen)
    import cg_paths
    return cg_paths.data_dir() / cg_paths.RECEIPTS_SUBPATH


def record_receipt(tool: str, path: Optional[Path] = None, **counts: Any) -> bool:
    """Append one receipt. Only int and bool values are kept. Never raises:
    a receipt is bookkeeping, and bookkeeping must not cost a recall or a
    request. Returns whether a line was written."""
    try:
        if tool not in RECEIPT_TOOLS or not receipts_enabled():
            return False
        line: Dict[str, Any] = {"ts": now_iso(), "tool": tool}
        for key, value in counts.items():
            if isinstance(value, bool) or (isinstance(value, int) and value >= 0):
                line[key] = value
        target = Path(path) if path else receipts_path()
        missing = [p for p in (target.parent, *target.parent.parents) if not p.exists()]
        target.parent.mkdir(parents=True, exist_ok=True)
        for made in missing:
            _make_private(made, PRIVATE_DIR_MODE)
        new_file = not target.exists()
        with open(target, "a", encoding="utf-8") as fh:
            if new_file:
                _make_private(target, PRIVATE_FILE_MODE)
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        return True
    except Exception:                                      # noqa: BLE001
        return False


def record_recall(tool: str, query_terms: int = 0, spans_matched: int = 0,
                  spans_opened: int = 0, chars_returned: int = 0) -> bool:
    """One recall-path receipt with all four counts, zeros included, so every
    recall line has the same shape."""
    return record_receipt(tool, query_terms=query_terms, spans_matched=spans_matched,
                          spans_opened=spans_opened, chars_returned=chars_returned)


def parse_since(text: Optional[str]) -> Optional[datetime.timedelta]:
    """'7d', '24h', '30m', '2w' -> timedelta; None for no limit. Raises
    ValueError on anything else."""
    if not text:
        return None
    m = _SINCE_RE.match(text)
    if not m:
        raise ValueError("--since takes a number and a unit: 30m, 24h, 7d, 2w")
    return datetime.timedelta(seconds=int(m.group(1)) * _SINCE_UNITS[m.group(2).lower()])


def _parse_ts(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return ts


def read_receipts(path: Path, since: Optional[datetime.timedelta] = None) -> List[Dict[str, Any]]:
    """Every well-formed receipt, oldest first. A line that is not a JSON
    object with a known tool and a readable timestamp is skipped: one torn or
    hand-edited line must not hide the rest."""
    out: List[Dict[str, Any]] = []
    cutoff = (datetime.datetime.now().astimezone() - since) if since else None
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for raw in fh:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("tool") not in RECEIPT_TOOLS:
                continue
            ts = _parse_ts(obj.get("ts"))
            if ts is None or (cutoff is not None and ts < cutoff):
                continue
            out.append(obj)
    return out


def _count(e: Dict[str, Any], key: str) -> int:
    value = e.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def archive_size(span_dir: Path) -> Tuple[int, int, int]:
    """(runs, span files, bytes) currently on disk."""
    runs = spans = size = 0
    for d in run_dirs(span_dir):
        names = [n for n in archived_files(d) if SPAN_NAME_RE.match(n)]
        if names:
            runs += 1
        for name in archived_files(d):
            try:
                size += (d / name).stat().st_size
            except OSError:
                pass
        spans += len(names)
    return runs, spans, size


def receipts_summary(events: List[Dict[str, Any]], span_dir: Path,
                     seal_reports: Optional[List[RunReport]] = None) -> Dict[str, Any]:
    """Everything --receipts prints, as numbers."""
    recall = [e for e in events if e["tool"].startswith(("cli.", "mcp."))]
    compactions = [e for e in events if e["tool"] == "proxy.compaction"]
    runs, spans, size = archive_size(span_dir)
    by_tool: Dict[str, int] = {}
    for e in recall:
        name = e["tool"].split(".", 1)[1]
        by_tool[name] = by_tool.get(name, 0) + 1
    chars_returned = sum(_count(e, "chars_returned") for e in recall)
    out = {
        "archive_runs": runs, "archive_spans": spans, "archive_bytes": size,
        "recall_calls": len(recall), "recall_by_tool": by_tool,
        "spans_opened": sum(_count(e, "spans_opened") for e in recall),
        "spans_matched": sum(_count(e, "spans_matched") for e in recall),
        "chars_returned": chars_returned,
        # Characters returned against bytes archived: the same unit for ASCII
        # text and close for the rest, and a number nobody can inflate by
        # opening the same span twice past 100%.
        "share_read": min(1.0, chars_returned / size) if size else 0.0,
        "compactions": len(compactions),
        "compactions_with_pointer": sum(1 for e in compactions if e.get("pointer") is True),
        "chars_archived": sum(_count(e, "chars_archived") for e in compactions),
        "tokens_saved": sum(_count(e, "tokens_saved") for e in compactions),
        # GUARDIAN_SUMMARY_MEMO: where each compaction's summary came from.
        "summaries_reused": sum(1 for e in compactions if e.get("reused") is True),
        "summaries_extended": sum(1 for e in compactions if e.get("extended") is True),
        "summaries_generated": sum(1 for e in compactions
                                   if e.get("reused") is not True and e.get("extended") is not True),
        "recall_calls_seen_by_proxy": sum(_count(e, "calls") for e in events
                                          if e["tool"] == "proxy.recall_seen"),
    }
    if seal_reports is not None:
        out["seal"] = summarise(seal_reports)
    return out


# --- --savings: the compaction log's before -> after totals ---------------------------

def log_path(env: Optional[Dict[str, str]] = None) -> Path:
    """GUARDIAN_LOG_PATH, else <data dir>/logs/context_guardian_log.json: the
    proxy's own resolution."""
    env = os.environ if env is None else env
    chosen = (env.get("GUARDIAN_LOG_PATH") or "").strip()
    if chosen:
        return Path(chosen)
    import cg_paths
    return cg_paths.data_dir() / cg_paths.LOG_SUBPATH


def read_log(path: Path, since: Optional[datetime.timedelta] = None) -> List[Dict[str, Any]]:
    """Every JSON-object line of the compaction log, oldest first. A line that
    is not a JSON object is skipped. With `since`, an event whose local
    "timestamp" (YYYY-MM-DDTHH:MM:SS) is older than now - since, or has none
    that reads, is left out."""
    out: List[Dict[str, Any]] = []
    cutoff = (datetime.datetime.now().astimezone() - since) if since else None
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for raw in fh:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            if cutoff is not None:
                ts = _parse_ts(obj.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
            out.append(obj)
    return out


def savings_summary(log_events: List[Dict[str, Any]], receipts: List[Dict[str, Any]],
                    window: str = "all time") -> Dict[str, Any]:
    """Everything --savings prints, counted from the log and the receipts.
    A "dry_run" event is never counted: it forwarded the request unchanged."""
    comps = [e for e in log_events if e.get("event") == "compaction"]
    before = sum(_count(e, "estimated_tokens_before") for e in comps)
    after = sum(_count(e, "estimated_tokens_after") for e in comps)
    recall = [e for e in receipts if e["tool"].startswith(("cli.", "mcp."))]
    return {
        "window": window,
        "compactions": len(comps),
        "tokens_before": before,
        "tokens_after": after,
        "tokens_kept_out": sum(_count(e, "tokens_saved") for e in comps),
        "pct_smaller": round(100 * (1 - after / before), 1) if before else 0.0,
        "oversized_spills": sum(1 for e in log_events if e.get("event") == "oversized_spill")
        + sum(_count(e, "oversized_spilled") for e in comps),
        "summaries_reused": sum(1 for e in comps if e.get("summary_source") == "reused"),
        "recall_calls": len(recall),
        "chars_returned": sum(_count(e, "chars_returned") for e in recall),
    }


def format_savings(s: Dict[str, Any]) -> str:
    head = "Context Guardian savings (%s)" % s["window"]
    if not s["compactions"] and not s["oversized_spills"]:
        return head + "\nno compactions in window"
    n = lambda key: format(s[key], ",")  # noqa: E731
    return "\n".join([
        head,
        "  compactions   %s" % n("compactions"),
        "  requests      %s -> %s tokens (%.1f%% smaller)" % (n("tokens_before"), n("tokens_after"), s["pct_smaller"]),
        "  kept out      %s tokens" % n("tokens_kept_out"),
        "  oversized     %s pasted message(s) cut to head + tail" % n("oversized_spills"),
        "  summaries     %s reused (no summariser call)" % n("summaries_reused"),
        "  recall        %s call(s), %s chars returned" % (n("recall_calls"), n("chars_returned")),
    ])


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1e6) if n >= 100_000 else "%d KB" % round(n / 1000)


def _seal_line(seal: Optional[Dict[str, int]]) -> str:
    if not seal:
        return "not checked"
    if seal["runs"] == 0:
        return "nothing archived yet"
    if seal["breaks"] == 0 and seal["unsealed"] == 0:
        return "all %d run(s) verify OK" % seal["runs"]
    parts = []
    if seal["breaks"]:
        parts.append("%d break(s)" % seal["breaks"])
    if seal["unsealed"]:
        parts.append("%d unsealed run(s)" % seal["unsealed"])
    return "%s of %d run(s); run context-guardian-recall --verify --all" % (", ".join(parts), seal["runs"])


def _n(count: int, word: str) -> str:
    """'1 compaction', '3 compactions'."""
    return "%s %s%s" % (format(count, ","), word, "" if count == 1 else "s")


def format_receipts(s: Dict[str, Any], window: str, share: bool = False) -> str:
    """The plain summary, or (share=True) a short block with no paths, no text
    and no run ids, safe to paste anywhere."""
    by_tool = ", ".join("%s %d" % kv for kv in sorted(s["recall_by_tool"].items())) or "none"
    pct = "%.1f%%" % (100 * s["share_read"])
    if share:
        seal = s.get("seal") or {}
        seal_text = ("sealed, all runs verify" if seal.get("runs") and not seal.get("breaks")
                     and not seal.get("unsealed") else _seal_line(seal).split(";")[0])
        return "\n".join([
            "Context Guardian receipts (%s)" % window,
            "- %s, ~%s tokens kept out of the window" % (_n(s["compactions"], "compaction"),
                                                         format(s["tokens_saved"], ",")),
            "- %s archived (%s), %s" % (_n(s["archive_spans"], "span"), _mb(s["archive_bytes"]), seal_text),
            "- %s opened %s; %s of the archive was read back"
            % (_n(s["recall_calls"], "recall call"), _n(s["spans_opened"], "span"), pct),
            "- local only: no telemetry, nothing left this machine",
        ])
    return "\n".join([
        "Context Guardian receipts (%s)" % window,
        "  archive      %d span(s) in %d run(s), %s on disk" % (s["archive_spans"], s["archive_runs"],
                                                             _mb(s["archive_bytes"])),
        "  compactions  %d recorded, %d with an archive pointer, %s chars archived, ~%s tokens saved"
        % (s["compactions"], s["compactions_with_pointer"], format(s["chars_archived"], ","),
           format(s["tokens_saved"], ",")),
        "  summaries    %d generated, %d reused, %d extended (summariser calls saved: %d)"
        % (s["summaries_generated"], s["summaries_reused"], s["summaries_extended"], s["summaries_reused"]),
        "  recall       %d recall call(s) (%s); %d span(s) opened, %d matched"
        % (s["recall_calls"], by_tool, s["spans_opened"], s["spans_matched"]),
        "  read back    %s of the archive (%s chars returned)" % (pct, format(s["chars_returned"], ",")),
        "  model used   %d recall tool call(s) seen in requests through the proxy"
        % s["recall_calls_seen_by_proxy"],
        "  seal         %s" % _seal_line(s.get("seal")),
    ])


# --- the skim index ----------------------------------------------------------------
#
# A model should be able to skim the whole archive in a few hundred tokens and
# open only what it needs. Reading spans to find out what is in them costs
# exactly what compaction saved -- a span is, by construction, bigger than the
# room it freed. So every span write appends one digest line to
# <run>/index.jsonl: the first sentence of the summary Guardian already made
# (no extra model call), the top keywords, and the sizes.
#
# The line is a pure function of the span file's content, so --reindex rebuilds
# exactly what live indexing wrote, and the DSH engine (cg_seal.js) computes the
# same lines with the same rules. Not sealed: anything in it can be rebuilt
# from the sealed spans.

INDEX_FILE = "index.jsonl"
CHARS_PER_TOKEN = 3.5
DIGEST_MAX_CHARS = 160
INDEX_TERMS = 8
# Keywords come from the first this-many characters of a span. The digest is a
# skim, not a concordance, and scanning a 400 KB tool-heavy span word by word
# put milliseconds on every compaction for terms the first 50,000 characters
# already show. The DSH engine applies the same cap.
TERMS_SCAN_CHARS = 50_000
_WORD_RE = re.compile(r"[a-z0-9_]{3,40}")
_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?: |$)")
# Common English and conversational filler. Frequency over what is left is a
# crude keyword score, and crude is the point: it is deterministic, free, and
# the same in Python and JavaScript.
STOPWORDS = frozenset("""
about above after again against all also and any are aren because been before being below between both but can
cannot could did didn does doesn doing don done down during each even every few for from further get going got
had has have having her here hers him his how into isn its itself just know let lets like look looks made make
many may more most much must need never new not now off okay once one ones only other our ours out over own
per please same see she should since some still such sure than that the their theirs them then there these
they thing things think this those though through too two under until upon use used using very via want was
wasn way well were weren what when where which while who whom why will with within without won would yes yet
you your yours yourself
""".split())


def est_tokens(text: str) -> int:
    """The estimate every cap in the recall tools is measured with:
    ceil(characters / 3.5), the proxy's own characters-per-token."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _inner(m: Any) -> Any:
    """A Claude Code transcript line wraps its message: {"type": "user",
    "message": {"role": ..., "content": ...}, ...}. Archived verbatim, it is
    read through to the message inside. Anything with its own content is
    itself."""
    if isinstance(m, dict) and "content" not in m and isinstance(m.get("message"), dict):
        return m["message"]
    return m


def message_role(m: Any) -> str:
    inner = _inner(m)
    if isinstance(inner, dict) and isinstance(inner.get("role"), str):
        return inner["role"]
    if isinstance(m, dict) and isinstance(m.get("type"), str):
        return m["type"]
    return "?"


def message_text(m: Any) -> str:
    """Everything searchable in one archived message, as one string."""
    m = _inner(m)
    if not isinstance(m, dict):
        return str(m)
    parts = []
    content = m.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    for key in ("tool_calls", "function_call"):
        if m.get(key):
            parts.append(json.dumps(m[key], ensure_ascii=False))
    return "\n".join(parts)


def plain_text(m: Any) -> str:
    """A message's prose only: string content, or the text of its text blocks.
    What the index sizes and keywords are computed over -- tool-call JSON is
    serialised differently by every language, and the index must come out the
    same from the proxy and from the engine."""
    m = _inner(m)
    if not isinstance(m, dict):
        return ""
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def first_sentence(text: str, limit: int = DIGEST_MAX_CHARS) -> str:
    """The first sentence, whitespace collapsed, at most `limit` characters."""
    flat = " ".join(str(text).split())
    m = _SENTENCE_RE.match(flat)
    out = m.group(1) if m else flat
    return out if len(out) <= limit else out[:limit - 3] + "..."


def top_terms(text: str, n: int = INDEX_TERMS) -> List[str]:
    # Counter, not a dict loop: counting runs in C, and on a 100 KB span the
    # loop was the single largest cost of a compaction (13 ms of a 22 ms
    # write_span in scripts/bench_proxy.py). Stopwords are dropped after
    # counting, which touches each distinct word once instead of every word.
    counts = collections.Counter(_WORD_RE.findall(text[:TERMS_SCAN_CHARS].lower()))
    ranked = sorted(((-c, w) for w, c in counts.items()
                     if w not in STOPWORDS and not w.isdigit()))
    return [w for _, w in ranked[:n]]


def index_entry(span: Dict[str, Any], name: str) -> Dict[str, Any]:
    """The index line for one span file's parsed content."""
    messages = span.get("messages") if isinstance(span.get("messages"), list) else []
    text = "\n".join(plain_text(m) for m in messages)
    chars = len(text)
    summary = span.get("summary")
    if isinstance(summary, str) and summary.strip():
        digest, source = first_sentence(summary), "summary"
    else:
        first_user = next((plain_text(m) for m in messages
                           if message_role(m) == "user" and plain_text(m).strip()), "")
        digest, source = first_sentence(first_user), "first-user-message"
        if not digest:
            # A span whose user turns are all tool results (common in a Claude
            # Code transcript) has no user prose; the first message that has
            # any says more than an empty digest does.
            first_any = next((plain_text(m) for m in messages if plain_text(m).strip()), "")
            digest, source = first_sentence(first_any), "first-message"
    at = span.get("at")
    return {"span": name[:-len(".json")] if name.endswith(".json") else name,
            "ts": at if isinstance(at, str) else "",
            "messages": len(messages), "chars": chars, "est_tokens": int(chars / CHARS_PER_TOKEN),
            "digest": digest, "digest_from": source, "terms": top_terms(text)}


def index_line(entry: Dict[str, Any]) -> bytes:
    return (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def append_index(run_dir: Path, entry: Dict[str, Any]) -> None:
    """Append one digest line under the run's lock. Raises on failure; the
    proxy's caller logs and carries on."""
    # Not fsynced: the index is rebuildable from the spans, and a line lost to
    # a power cut costs a skim, while an fsync costs every compaction.
    with ChainLock(run_dir):
        _append_line(Path(run_dir) / INDEX_FILE, index_line(entry), durable=False)


def read_index(run_dir: Path) -> List[Dict[str, Any]]:
    """The run's index lines; a line that does not parse is skipped."""
    out = []
    try:
        fh = open(Path(run_dir) / INDEX_FILE, "rb")
    except OSError:
        return out
    with fh:
        for raw in fh:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get("span"), str):
                out.append(obj)
    return out


def load_span(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def reindex(span_dir: Path, run: Optional[str] = None) -> Dict[str, int]:
    """Rebuild index.jsonl from the span files on disk, for archives written
    before the index existed. Replaces the file atomically."""
    out = {}
    targets = [Path(span_dir) / run] if run else run_dirs(span_dir)
    for d in targets:
        names = [n for n in archived_files(d) if SPAN_NAME_RE.match(n)]
        if not names:
            continue
        lines = []
        for name in names:
            span = load_span(d / name)
            if span is not None:
                lines.append(index_line(index_entry(span, name)))
        with ChainLock(d):
            atomic_write_bytes(d / INDEX_FILE, b"".join(lines))
        out[d.name] = len(lines)
    return out


def digests(span_dir: Path, run: Optional[str] = None) -> List[Tuple[float, str, Dict[str, Any]]]:
    """(mtime, run, index entry) for every span still on disk, newest first.
    A span with no index line (written before the index) is indexed on the
    fly, without writing anything."""
    out = []
    targets = [Path(span_dir) / run] if run else run_dirs(span_dir)
    for d in targets:
        if not d.is_dir():
            continue
        names = {n for n in archived_files(d) if SPAN_NAME_RE.match(n)}
        indexed = {}
        for e in read_index(d):
            indexed[e["span"] + ".json"] = e
        for name in names:
            entry = indexed.get(name)
            if entry is None:
                span = load_span(d / name)
                if span is None:
                    continue
                entry = index_entry(span, name)
            try:
                mtime = (d / name).stat().st_mtime
            except OSError:
                continue
            out.append((mtime, d.name, entry))
    out.sort(key=lambda t: (t[0], t[1], t[2]["span"]), reverse=True)
    return out


def digest_line(run: str, e: Dict[str, Any]) -> str:
    terms = e.get("terms") if isinstance(e.get("terms"), list) else []
    ts = str(e.get("ts") or "")[:16]
    return "%s/%s %s ~%st: %s%s" % (run, e.get("span"), ts, e.get("est_tokens", "?"), e.get("digest", ""),
                                   " [%s]" % ", ".join(str(t) for t in terms[:4]) if terms else "")


# --- the capture journal (GUARDIAN_CAPTURE=journal) ---------------------------------
#
# Spans hold what compaction evicted. The journal holds the whole session: every
# message of a run, once, in the order it was first seen, in
# <run>/journal.jsonl (then journal.1.jsonl, journal.2.jsonl, ... as it rotates).
#
# "Once" is the hard part. Clients resend the whole history on every request,
# and compaction rewrites what goes upstream, so a message is identified by a
# hash of what it says (role, content, tool calls, tool call id) and COUNTED: it
# is journaled when a request carries more copies of it than the journal holds.
# A message that genuinely repeats ("yes", twice) is therefore journaled twice,
# and a client that trims its own history (fewer copies) adds nothing.
#
# Sealed in batches, not per line: the running SHA-256 of the current file is
# kept as it is written, so a seal is O(1) however large the file, and each
# journal chain line records the byte size it covers. Bytes written after the
# last seal are reported by the verifier as an uncovered tail, not as a break.

JOURNAL_ROTATE_MAX_BYTES = 32 * 1024 * 1024
JOURNAL_SEAL_EVERY_LINES = 64
JOURNAL_SEAL_EVERY_SECONDS = 5.0


def journal_name(i: int) -> str:
    return "journal.jsonl" if i == 0 else "journal.%d.jsonl" % i


def journal_files(run_dir: Path) -> List[Path]:
    """The run's journal files in write order."""
    def number(name):
        m = re.match(r"^journal(?:\.(\d+))?\.jsonl$", name)
        return int(m.group(1) or 0) if m else None
    found = []
    for name in archived_files(run_dir):
        k = number(name)
        if k is not None:
            found.append((k, Path(run_dir) / name))
    return [p for _, p in sorted(found)]


def _hash_part(value: Any) -> bytes:
    # Strings, the common case, are hashed as their own bytes: serialising
    # every resent message to JSON on every request was most of the journal's
    # per-request cost. Anything else goes through canonical-ish JSON. The
    # leading tag keeps "null" the string apart from None.
    if isinstance(value, str):
        return b"s" + value.encode("utf-8", "replace")
    return b"j" + json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8", "replace")


def message_hash(m: Dict[str, Any]) -> str:
    """Identity of a message for the journal: what it says (role, content,
    tool calls, tool call id), not where it is in the history."""
    h = hashlib.sha256()
    for key in ("role", "content", "tool_calls", "tool_call_id"):
        part = _hash_part(m.get(key))
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return h.hexdigest()


def read_journal(run_dir: Path):
    """Yield (path, entry) for every journal line that parses, in order."""
    for path in journal_files(run_dir):
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("n"), int):
                    yield path, obj


class Journal:
    """One run's capture journal. Thread-safe; every method may raise, and the
    proxy's callers catch everything (fail open)."""

    def __init__(self, run_dir: Path, run_id: str, cap_bytes: int, seal: bool = True,
                 anchor_path: Optional[Path] = None, skip=None):
        self.run_dir, self.run_id = Path(run_dir), run_id
        self.cap = max(0, int(cap_bytes))
        self.rotate = min(JOURNAL_ROTATE_MAX_BYTES, max(1, self.cap // 4))
        self.seal_on, self.anchor = seal, anchor_path
        self.skip = skip or (lambda m: False)
        self.counts: Dict[str, int] = {}
        self.n = self.requests = self.total = 0
        self.file_index = 0
        self.cur_bytes = 0
        self.hasher = hashlib.sha256()
        self.unsealed_lines = 0
        self.last_seal = time.monotonic()
        self.stopped = False
        self.stop_reported = False
        self._opened = False
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.run_dir / journal_name(self.file_index)

    def _open(self) -> None:
        """First use: create the run directory, and pick up a file that already
        exists (hashing what is in it) rather than sealing a wrong digest."""
        if self._opened:
            return
        missing = [p for p in (self.run_dir, *self.run_dir.parents) if not p.exists()]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for made in missing:
            _make_private(made, PRIVATE_DIR_MODE)
        existing = journal_files(self.run_dir)
        if existing:
            for p in existing[:-1]:
                self.total += p.stat().st_size
            last = existing[-1]
            m = re.match(r"^journal(?:\.(\d+))?\.jsonl$", last.name)
            self.file_index = int(m.group(1) or 0)
            with open(last, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    self.hasher.update(block)
                    self.cur_bytes += len(block)
            self.total += self.cur_bytes
            for _, entry in read_journal(self.run_dir):
                h = entry.get("hash")
                if isinstance(h, str):
                    self.counts[h] = self.counts.get(h, 0) + 1
                self.n = max(self.n, entry["n"] + 1)
        self._opened = True

    def capture(self, messages: Any) -> int:
        """Journal the messages of one request that the journal does not hold
        yet. Returns how many lines were written."""
        if not isinstance(messages, list):
            return 0
        with self._lock:
            if self.stopped:
                return 0
            self._open()
            self.requests += 1
            seen_now: Dict[str, int] = {}
            pending = []
            for m in messages:
                if not isinstance(m, dict) or self.skip(m):
                    continue
                h = message_hash(m)
                seen_now[h] = seen_now.get(h, 0) + 1
                # The k-th copy in this request is new when the journal holds
                # fewer than k copies (self.counts changes only once written).
                if seen_now[h] > self.counts.get(h, 0):
                    pending.append((h, m))
            written = 0
            for h, m in pending:
                line = (json.dumps({"n": self.n, "request": self.requests, "ts": now_iso(), "hash": h,
                                    "message": m}, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                if self.total + len(line) > self.cap:
                    self.stopped = True
                    break
                if self.cur_bytes and self.cur_bytes + len(line) > self.rotate:
                    self._seal_locked()
                    self.file_index += 1
                    self.cur_bytes = 0
                    self.hasher = hashlib.sha256()
                _append_line(self.path, line, durable=False)
                self.hasher.update(line)
                self.cur_bytes += len(line)
                self.total += len(line)
                self.counts[h] = self.counts.get(h, 0) + 1
                self.n += 1
                self.unsealed_lines += 1
                written += 1
            if self.stopped or self.unsealed_lines >= JOURNAL_SEAL_EVERY_LINES or (
                    self.unsealed_lines and time.monotonic() - self.last_seal >= JOURNAL_SEAL_EVERY_SECONDS):
                self._seal_locked()
            return written

    def _seal_locked(self) -> None:
        if not self.seal_on:
            self.unsealed_lines = 0
            return
        if not self.unsealed_lines or not self.cur_bytes:
            return
        append_entry(self.run_dir, self.run_id,
                     {"type": "journal", "file": self.path.name,
                      "sha256": self.hasher.copy().hexdigest(), "size": self.cur_bytes},
                     anchor_path=self.anchor)
        self.unsealed_lines = 0
        self.last_seal = time.monotonic()

    def flush(self) -> None:
        """Seal whatever is unsealed (shutdown)."""
        with self._lock:
            if self._opened:
                self._seal_locked()

    def stats(self) -> Dict[str, Any]:
        return {"messages": self.n, "requests": self.requests, "bytes": self.total,
                "files": self.file_index + 1 if self._opened and self.total else 0,
                "stopped": self.stopped}
