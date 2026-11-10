"""Empty / degenerate-turn retry (GUARDIAN_EMPTY_RETRY): the pure half.

A local model sometimes finishes a turn with nothing to show: an empty message, a
reasoning block and no answer, or a parrot of the "[Context Guardian auto-compaction"
header the proxy put at the top of the history. This module decides when a response is
that, builds the one retry request, and splices a streaming retry into the stream the
client is already reading. It does no I/O of its own: the proxy sends the retry and
hands the bytes in. It holds no state between turns, so a nudge cannot outlive the
request it was built for (ADR 0001: any failure here means the upstream's own answer
reaches the client untouched).

Standard library only.
"""
import copy
import json
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

# The header every summary message the proxy injects starts with
# (context_guardian.GUARDIAN_SUMMARY_MARKER). Kept in step by a test.
MARKER = "[Context Guardian auto-compaction"

DEFAULT_NUDGE = ("Your previous reply was empty. Please answer the user's most recent "
                 "message now.")

# A turn that stopped for any other reason (ran out of room, was filtered, asked for a
# tool) is not "completed with nothing to say"; a retry would not help it.
_DONE_OPENAI = (None, "stop")
_DONE_ANTHROPIC = (None, "end_turn", "stop_sequence")

# Text that begins like the header is held back while it might still be the header, but
# not without limit: past this many characters it is somebody's real answer.
ECHO_HOLD_MAX = 64 * 1024

_SEP = re.compile(rb"\r?\n\r?\n")


def is_degenerate_text(text: Any) -> bool:
    """No visible text, or text that is nothing but the injected header's echo."""
    if not isinstance(text, str):
        return True
    t = text.strip()
    return t == "" or t.startswith(MARKER)


def _could_become_echo(text: str) -> bool:
    """True while `text` is still a prefix of the header (or already starts with it)."""
    t = text.lstrip()
    return t == "" or MARKER.startswith(t) or t.startswith(MARKER)


# ---- whole (non-streaming) responses ---------------------------------------------

def response_is_empty(route: str, data: Any) -> bool:
    """`data` is a parsed completed response. `route` is "chat" (OpenAI chat/completions)
    or "messages" (Anthropic). Anything unexpected is NOT empty: a retry is only for a
    response we positively recognise as a finished turn with nothing in it."""
    if not isinstance(data, dict):
        return False
    if route == "chat":
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return False
        c = choices[0]
        if c.get("finish_reason") not in _DONE_OPENAI:
            return False
        msg = c.get("message")
        if not isinstance(msg, dict):
            return False
        if msg.get("tool_calls") or msg.get("function_call"):
            return False
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in (None, "text"))
        if content is not None and not isinstance(content, str):
            return False
        return is_degenerate_text(content or "")
    if route == "messages":
        if data.get("type") not in (None, "message"):
            return False
        if data.get("stop_reason") not in _DONE_ANTHROPIC:
            return False
        blocks = data.get("content")
        if not isinstance(blocks, list):
            return False
        text = ""
        for b in blocks:
            if not isinstance(b, dict):
                return False
            kind = b.get("type")
            if kind == "text":
                text += b.get("text") or ""
            elif kind in ("thinking", "redacted_thinking"):
                continue
            else:                                   # tool_use, server_tool_use, anything new: a real answer
                return False
        return is_degenerate_text(text)
    return False


def nudged_payload(route: str, payload: Dict[str, Any], nudge: str) -> Dict[str, Any]:
    """A copy of the forwarded request with `nudge` as trailing user text. The caller's
    payload is never modified, and the copy is used for the one retry request only."""
    out = copy.deepcopy(payload)
    messages = out.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("no messages to nudge")
    if route == "chat":
        messages.append({"role": "user", "content": nudge})
        return out
    last = messages[-1]
    if isinstance(last, dict) and last.get("role") == "user":
        content = last.get("content")
        if isinstance(content, str):
            blocks: List[Any] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = list(content)
        else:
            raise ValueError("unrecognised user content")
        blocks.append({"type": "text", "text": nudge})
        last["content"] = blocks
    else:
        messages.append({"role": "user", "content": nudge})
    return out


# ---- streams -------------------------------------------------------------------------

def _parse_frame(frame: bytes) -> Tuple[Optional[str], Optional[str], Any]:
    """(event name, data text, parsed JSON or None) of one SSE frame."""
    event, data = None, []
    for line in frame.decode("utf-8", "replace").split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:][1:] if line[5:6] == " " else line[5:])
    text = "\n".join(data) if data else None
    obj = None
    if text and text.strip() != "[DONE]":
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    return event, text, obj


class StreamScan:
    """Reads a response stream frame by frame and says, for each, whether it may go to
    the client now ("pass"), must wait ("hold"), or proves the turn has content
    ("release": send everything waiting, and stop watching).

    Frames that carry the end of the turn (finish reason, [DONE], usage) are held until
    the end of the stream, and so is any text that might still turn out to be the echo of
    the header. Reasoning deltas and role frames are never held: they are sent as they
    arrive, so a slow reasoning model is not silenced.
    """

    def __init__(self, route: str):
        self.route = route
        self.text = ""
        self.visible = False          # a real answer or a tool call has been seen
        self.finished = False         # the turn's end was seen
        self.finish = None            # the finish / stop reason, if given
        self.broken = False           # an error event: leave the stream alone
        self.holding = False          # a frame has been held: every later one waits too
        self.forwarded_blocks = 0     # Anthropic: content blocks already sent to the client
        self._block_kind: Dict[int, str] = {}

    # -- helpers
    def _text_delta(self, piece: str) -> str:
        self.text += piece
        if self.text.strip() == "":
            return "hold"
        if _could_become_echo(self.text) and len(self.text) <= ECHO_HOLD_MAX:
            return "hold"
        self.visible = True
        return "release"

    def classify(self, frame: bytes) -> str:
        event, text, obj = _parse_frame(frame)
        if self.route == "chat":
            kind = self._chat(text, obj)
        else:
            kind = self._anthropic(event, obj)
        if kind == "pass" and self.holding:
            return "hold"
        if kind == "hold":
            self.holding = True
        return kind

    def _chat(self, text: Optional[str], obj: Any) -> str:
        if text is not None and text.strip() == "[DONE]":
            self.finished = True
            return "hold"
        if not isinstance(obj, dict):
            return "pass"
        if "error" in obj and "choices" not in obj:
            self.broken = True
            return "release"
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return "pass"
        if not choices:
            return "hold" if self.finished or self.finish is not None else "pass"
        c = choices[0] if isinstance(choices[0], dict) else {}
        d = c.get("delta") if isinstance(c.get("delta"), dict) else {}
        if d.get("tool_calls") or d.get("function_call"):
            self.visible = True
            return "release"
        verdict = "pass"
        piece = d.get("content")
        if isinstance(piece, str) and piece != "":
            verdict = self._text_delta(piece)
            if verdict == "release":
                return "release"
        if d.get("refusal"):
            self.visible = True
            return "release"
        reason = c.get("finish_reason")
        if reason is not None:
            self.finish = reason
            self.finished = True
            return "hold"
        if verdict == "hold":
            return "hold"
        return "pass"

    def _anthropic(self, event: Optional[str], obj: Any) -> str:
        if not isinstance(obj, dict):
            return "pass"
        kind = obj.get("type") or event
        if kind == "error":
            self.broken = True
            return "release"
        if kind == "content_block_start":
            block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
            btype = block.get("type")
            self._block_kind[obj.get("index", -1)] = str(btype)
            if btype in ("tool_use", "server_tool_use") or (btype not in ("text", "thinking", "redacted_thinking")
                                                           and btype is not None):
                self.visible = True
                return "release"
            if btype == "text":
                verdict = self._text_delta(block.get("text") or "") if block.get("text") else "hold"
                return "release" if verdict == "release" else "hold"
            return "pass"
        if kind == "content_block_delta":
            d = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            if d.get("type") == "text_delta":
                return self._text_delta(d.get("text") or "") if d.get("text") else "hold"
            if d.get("type") == "input_json_delta":
                self.visible = True
                return "release"
            return "pass"
        if kind == "content_block_stop":
            return "hold" if self._block_kind.get(obj.get("index", -1)) == "text" else "pass"
        if kind == "message_delta":
            self.finish = (obj.get("delta") or {}).get("stop_reason") if isinstance(obj.get("delta"), dict) else None
            self.finished = True
            return "hold"
        if kind == "message_stop":
            self.finished = True
            return "hold"
        return "pass"

    def note_sent(self, frame: bytes) -> None:
        """Called for every frame that really went to the client (Anthropic block count)."""
        if self.route != "messages":
            return
        _, _, obj = _parse_frame(frame)
        if isinstance(obj, dict) and obj.get("type") == "content_block_start":
            self.forwarded_blocks += 1

    @property
    def degenerate(self) -> bool:
        """The stream ended as a completed turn with nothing visible in it."""
        done = _DONE_OPENAI if self.route == "chat" else _DONE_ANTHROPIC
        if not self.finished or self.visible or self.broken or self.finish not in done:
            return False
        return self.text.strip() == "" or self.text.lstrip().startswith(MARKER)


async def frames_of(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-cut raw chunks into whole SSE frames (each keeps its terminating blank line).
    Bytes are never changed, only regrouped; a trailing partial frame is yielded last."""
    buf = b""
    async for chunk in chunks:
        buf += chunk
        while True:
            m = _SEP.search(buf)
            if not m:
                break
            yield buf[:m.end()]
            buf = buf[m.end():]
    if buf:
        yield buf


def _shift_block_index(frame: bytes, offset: int) -> bytes:
    """An Anthropic frame with its content-block index moved by `offset`."""
    event, _, obj = _parse_frame(frame)
    if offset and isinstance(obj, dict) and isinstance(obj.get("index"), int) and str(obj.get("type", "")).startswith("content_block"):
        obj["index"] += offset
        head = ("event: %s\n" % event) if event else ""
        return (head + "data: " + json.dumps(obj) + "\n\n").encode("utf-8")
    return frame


async def splice(route: str, chunks: AsyncIterator[bytes],
                 start_retry: Callable[[], Awaitable[Optional[AsyncIterator[bytes]]]],
                 on_event: Callable[[str], None] = lambda name: None) -> AsyncIterator[bytes]:
    """The stream the client reads. Bytes of a good turn pass through unchanged and in
    order. When the turn ends empty, one retry stream (from `start_retry`, which returns
    None when it cannot be started) is read, and if it carries content the held tail of
    the original is replaced by it. Every other outcome sends the original's own bytes.

    `on_event` hears "retry", "recovered" and "error"; it must not raise (and is guarded
    anyway)."""
    def note(name: str) -> None:
        try:
            on_event(name)
        except Exception:                                   # noqa: BLE001
            pass

    scan: Optional[StreamScan]
    try:
        scan = StreamScan(route)
    except Exception:                                       # noqa: BLE001
        scan = None
    held: List[bytes] = []
    passthrough = scan is None
    try:
        async for frame in frames_of(chunks):
            if passthrough:
                yield frame
                continue
            try:
                verdict = scan.classify(frame)
                if verdict == "pass":
                    scan.note_sent(frame)
                elif verdict == "release":
                    for h in held:
                        scan.note_sent(h)
                    scan.note_sent(frame)
            except Exception:                              # noqa: BLE001
                note("error")
                verdict = "fail"
            if verdict == "pass":
                yield frame
            elif verdict == "hold":
                held.append(frame)
            else:                                           # release, or the scanner failed
                for h in held:
                    yield h
                held = []
                yield frame
                passthrough = True
    except Exception:
        # The upstream stream broke mid-turn: whatever was held was part of what the
        # upstream sent, so it goes out before the error does. (GeneratorExit and
        # cancellation are not Exceptions: a client that left gets nothing more.)
        if held and not passthrough:
            for h in held:
                yield h
        raise
    if passthrough or scan is None:
        return

    retry_frames: Optional[List[bytes]] = None
    try:
        if scan.degenerate:
            note("retry")
            retry_frames = await _read_retry(route, start_retry, scan.forwarded_blocks)
    except Exception:                                       # noqa: BLE001
        note("error")
        retry_frames = None
    if retry_frames:
        note("recovered")
        for f in retry_frames:
            yield f
        return
    for h in held:
        yield h


async def _read_retry(route: str, start_retry, offset: int) -> Optional[List[bytes]]:
    """The retry's frames, all of them, when it carried content; else None. Read to the
    end before anything is sent, so a retry that turns out empty (or fails half way)
    costs the client nothing."""
    stream = await start_retry()
    if stream is None:
        return None
    try:
        scan = StreamScan(route)
        out: List[bytes] = []
        async for frame in frames_of(stream):
            scan.classify(frame)
            if route == "messages":
                event, _, obj = _parse_frame(frame)
                kind = (obj or {}).get("type") if isinstance(obj, dict) else event
                if kind in ("message_start", "ping"):
                    continue
                frame = _shift_block_index(frame, offset)
            out.append(frame)
        if scan.visible and scan.finished and not scan.broken:
            return out
        return None
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
