"""Exact token counts from the upstream's own tokenizer, chat template included.

The proxy's estimate is characters / 3.5. It is content-blind: dense JSON and
CJK text read LOW, so compaction fires late and the backend overflows first.
llama.cpp and vLLM can count for us, so on those two the count is exact and the
estimate (calibrated, when the proxy has learned a factor) is only the fallback.

The ladder, per request:

  vLLM       ONE POST <root>/tokenize {"model", "messages",
             "add_generation_prompt": true[, "tools"]}  ->  "count".
  llama.cpp  POST <root>/apply-template {"messages"}  ->  "prompt", then
             POST <root>/tokenize {"content": prompt}  ->  len("tokens").
             A cache of running totals keyed by the messages' hashes
             (cg_archive.message_hash) means the next turn sends only the NEW
             messages, plus one template-overhead probe per (model, tools). Past
             GUARDIAN_COUNT_RECOUNT_MISSES new messages, or on any trouble with
             the incremental path, the whole request is counted again.
  Ollama     has no tokenizer route on the servers measured (POST /api/tokenize
             answers 404), so there is no rung: it stays on the estimate, and
             this module never calls /api/tokenize.

Any error or timeout falls back to the estimate for THAT request and adds one
to tokenize_failures; the request itself always goes through. GUARDIAN_ESTIMATE_MARGIN
multiplies only a count that is not exact.

All state (probe results, the cache, the counters) is module-level on purpose:
a reload resets it, which is what the tests rely on.
"""
import collections
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

import cg_archive

log = logging.getLogger("context_guardian.count")

_OFF = ("0", "false", "no", "off")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > minimum else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= 0 else default


# Read at import, as the proxy's own knobs are.
ENABLED = os.environ.get("GUARDIAN_TOKENIZE", "1").strip().lower() not in _OFF
TIMEOUT = _env_float("GUARDIAN_TOKENIZE_TIMEOUT", 0.5)
ESTIMATE_MARGIN = _env_float("GUARDIAN_ESTIMATE_MARGIN", 1.0)
CACHE_SIZE = _env_int("GUARDIAN_COUNT_CACHE", 4096)
RECOUNT_MISSES = _env_int("GUARDIAN_COUNT_RECOUNT_MISSES", 8)

CHARS_PER_TOKEN = 3.5
# A probe that found no tokenizer is asked again after this long; a probe that
# could not reach the server, sooner.
PROBE_NEGATIVE_TTL = 300.0
PROBE_ERROR_TTL = 30.0

# (bare root, model) -> (kind, expires_at). A positive answer does not expire.
_probes: Dict[Tuple[str, str], Tuple[str, float]] = {}
# Running totals: chain key of a message list -> tokens of that whole request.
_totals: "collections.OrderedDict[str, int]" = collections.OrderedDict()
# (root, model, tools) -> tokens the template adds around any messages, or None
# when it cannot be measured (the incremental path is then off for that key).
_overhead: Dict[Tuple[str, str, str], Optional[int]] = {}
_counters = {"tokenize_failures": 0, "count_cache_hits": 0, "count_cache_misses": 0}


def stats() -> Dict[str, int]:
    return dict(_counters)


def bare_root(upstream_root: Optional[str] = None) -> str:
    """The server root: the upstream URL without a trailing slash or /v1."""
    root = (upstream_root if upstream_root is not None
            else os.environ.get("GUARDIAN_UPSTREAM_URL", "http://localhost:11434/v1")).strip().rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _kind_of(name: Optional[str]) -> Optional[str]:
    """'vLLM' | 'llama.cpp' | 'Ollama' (the window detection's names) or this
    module's own, as 'vllm' | 'llamacpp' | 'ollama' | ''. None stays None."""
    if name is None:
        return None
    flat = "".join(ch for ch in str(name).lower() if ch.isalnum())
    return flat if flat in ("vllm", "llamacpp") else ""


async def _post(client: httpx.AsyncClient, url: str, body: Dict[str, Any],
                headers: Optional[Dict[str, str]]) -> Any:
    resp = await client.post(url, json=body, headers=headers or None, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def probe_tokenizer(client: httpx.AsyncClient, upstream_root: str, model: str,
                          headers: Optional[Dict[str, str]] = None) -> str:
    """'vllm' | 'llamacpp' | 'ollama' | '' for the server at `upstream_root`.

    vLLM answers /tokenize {"prompt"} with a "count"; llama.cpp answers
    /tokenize {"content"} with "tokens" alone. Anything else, Ollama included,
    is '' ('ollama' is never returned: it has no tokenizer route to use).
    Cached per (root, model). The proxy never calls this: it passes `kind=` from
    its window detection, so with detection off it sends no tokenizer request.
    """
    bare = bare_root(upstream_root)
    key = (bare, model)
    cached = _probes.get(key)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    kind, ttl = "", PROBE_NEGATIVE_TTL
    try:
        resp = await client.post(bare + "/tokenize", json={"model": model, "prompt": "hi"},
                                 headers=headers or None, timeout=TIMEOUT)
        if resp.status_code == 200 and isinstance(_json_dict(resp).get("count"), int):
            kind = "vllm"
        elif resp.status_code < 500:
            resp = await client.post(bare + "/tokenize", json={"content": "hi"},
                                     headers=headers or None, timeout=TIMEOUT)
            if resp.status_code == 200 and isinstance(_json_dict(resp).get("tokens"), list):
                kind = "llamacpp"
        if not kind and resp.status_code >= 500:
            # A server that has a /tokenize and is failing it: a failure to
            # count, not the absence of a tokenizer. Asked again soon.
            raise httpx.HTTPStatusError("tokenizer probe answered %d" % resp.status_code,
                                        request=resp.request, response=resp)
    except httpx.HTTPError as exc:
        _counters["tokenize_failures"] += 1
        ttl = PROBE_ERROR_TTL
        log.debug("tokenizer probe of %s failed: %s", bare, exc)
    _probes[key] = (kind, float("inf") if kind else time.monotonic() + ttl)
    return kind


def _json_dict(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _char_estimate(messages: List[Any], tools: Any) -> int:
    """The proxy's fallback when it does not hand one in: characters / 3.5 of
    what goes on the wire."""
    def size(blob: Any) -> int:
        if blob is None:
            return 0
        if isinstance(blob, str):
            return len(blob)
        try:
            return len(json.dumps(blob, ensure_ascii=False))
        except (TypeError, ValueError):
            return len(str(blob))
    chars = 0
    for m in messages:
        if isinstance(m, dict):
            chars += size(m.get("content")) + size(m.get("tool_calls"))
        else:
            chars += size(m)
    return int((chars + size(tools)) / CHARS_PER_TOKEN)


def _fallback(messages, tools, estimate: Optional[int], calibrated: bool) -> Tuple[int, str]:
    base = _char_estimate(messages, tools) if estimate is None else estimate
    return max(0, int(base * ESTIMATE_MARGIN)), ("calibrated" if calibrated else "estimate")


async def count_request(client: httpx.AsyncClient, messages: List[Dict[str, Any]], tools: Any,
                        model: str, upstream_root: Optional[str] = None, *,
                        kind: Optional[str] = None, headers: Optional[Dict[str, str]] = None,
                        estimate: Optional[int] = None,
                        calibrated: bool = False) -> Tuple[int, str]:
    """(tokens, source) for a request; source is 'tokenize' | 'calibrated' | 'estimate'.

    `kind` is what the caller already knows about the upstream ('vLLM',
    'llama.cpp', 'Ollama' or '' for none): the proxy takes it from its window
    detection so no probe goes out on the request path. None means unknown, and
    the upstream is probed once per (root, model). `estimate` is the caller's
    own fallback figure (the proxy's, with its calibration factor already
    applied, flagged by `calibrated`); without one it is characters / 3.5.
    Never raises.
    """
    try:
        if ENABLED and isinstance(messages, list) and all(isinstance(m, dict) for m in messages):
            bare = bare_root(upstream_root)
            use = _kind_of(kind)
            if use is None:
                use = await probe_tokenizer(client, bare, model, headers)
            if use in ("vllm", "llamacpp"):
                try:
                    count = await (_count_vllm if use == "vllm" else _count_llamacpp)(
                        client, bare, messages, tools, model, headers)
                    return count, "tokenize"
                except Exception as exc:                   # noqa: BLE001
                    _counters["tokenize_failures"] += 1
                    log.debug("%s token count failed, using the estimate: %s: %s",
                              use, type(exc).__name__, exc)
    except Exception as exc:                               # noqa: BLE001
        log.debug("token counting skipped: %s: %s", type(exc).__name__, exc)
    return _fallback(messages if isinstance(messages, list) else [], tools, estimate, calibrated)


async def count_text(client: httpx.AsyncClient, text: str, model: str,
                     upstream_root: Optional[str] = None, *, kind: Optional[str] = None,
                     headers: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """(tokens, source) for plain text, no chat template. Same ladder and the
    same fallback as count_request."""
    try:
        if ENABLED:
            bare = bare_root(upstream_root)
            use = _kind_of(kind)
            if use is None:
                use = await probe_tokenizer(client, bare, model, headers)
            try:
                if use == "vllm":
                    body = await _post(client, bare + "/tokenize", {"model": model, "prompt": text}, headers)
                    return int(body["count"]), "tokenize"
                if use == "llamacpp":
                    body = await _post(client, bare + "/tokenize", {"content": text}, headers)
                    return len(body["tokens"]), "tokenize"
            except Exception as exc:                       # noqa: BLE001
                _counters["tokenize_failures"] += 1
                log.debug("%s text count failed, using the estimate: %s", use, exc)
    except Exception as exc:                               # noqa: BLE001
        log.debug("text counting skipped: %s", exc)
    return max(0, int(len(text) / CHARS_PER_TOKEN * ESTIMATE_MARGIN)), "estimate"


# --- vLLM -------------------------------------------------------------------------

async def _count_vllm(client, bare, messages, tools, model, headers) -> int:
    body: Dict[str, Any] = {"messages": messages, "add_generation_prompt": True}
    if model:
        body["model"] = model
    if tools:
        body["tools"] = tools
    return int((await _post(client, bare + "/tokenize", body, headers))["count"])


# --- llama.cpp --------------------------------------------------------------------

async def _render_tokens(client, bare, messages, tools, headers) -> int:
    """Tokens of the templated prompt for `messages`: apply-template, tokenize."""
    body: Dict[str, Any] = {"messages": messages}
    if tools:
        body["tools"] = tools
    prompt = (await _post(client, bare + "/apply-template", body, headers))["prompt"]
    if not isinstance(prompt, str):
        raise ValueError("apply-template returned no prompt")
    return len((await _post(client, bare + "/tokenize", {"content": prompt}, headers))["tokens"])


def _tools_key(tools: Any) -> str:
    if not tools:
        return ""
    try:
        return hashlib.sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return hashlib.sha256(str(tools).encode("utf-8")).hexdigest()


def _chain(bare: str, model: str, tools_key: str, messages: List[Dict[str, Any]]) -> List[str]:
    """One key per prefix of `messages`: key i names messages[0..i], from the
    messages' own hashes, so it holds the same wherever the list came from."""
    running = hashlib.sha256(("%s\0%s\0%s" % (bare, model, tools_key)).encode("utf-8"))
    keys = []
    for m in messages:
        running.update(cg_archive.message_hash(m).encode("ascii"))
        keys.append(running.copy().hexdigest())
    return keys


def _remember(key: str, total: int) -> None:
    if CACHE_SIZE <= 0:
        return
    _totals[key] = total
    _totals.move_to_end(key)
    while len(_totals) > CACHE_SIZE:
        _totals.popitem(last=False)


async def _template_overhead(client, bare, model, tools, tools_key, headers) -> Optional[int]:
    """Tokens the template adds around the messages themselves (BOS, the
    generation prompt, a tool preamble): the render of no messages. Asked once
    per (root, model, tools); None when the template will not render nothing."""
    key = (bare, model, tools_key)
    if key not in _overhead:
        try:
            _overhead[key] = await _render_tokens(client, bare, [], tools, headers)
        except httpx.TransportError:
            raise
        except Exception as exc:                           # noqa: BLE001
            log.debug("template overhead probe failed, counting whole requests: %s", exc)
            _overhead[key] = None
    return _overhead[key]


async def _count_llamacpp(client, bare, messages, tools, model, headers) -> int:
    tools_key = _tools_key(tools)
    keys = _chain(bare, model, tools_key, messages) if CACHE_SIZE > 0 else []
    n = len(messages)
    hit = next((i for i in range(n - 1, -1, -1) if keys[i] in _totals), None) if keys else None
    _counters["count_cache_hits"] += 0 if hit is None else hit + 1
    _counters["count_cache_misses"] += n if hit is None else n - hit - 1
    total: Optional[int] = None
    if hit is not None:
        known = _totals[keys[hit]]
        _totals.move_to_end(keys[hit])
        fresh = messages[hit + 1:]
        if not fresh:
            total = known
        elif len(fresh) <= RECOUNT_MISSES:
            try:
                base = await _template_overhead(client, bare, model, tools, tools_key, headers)
                if base is not None:
                    total = known + await _render_tokens(client, bare, fresh, tools, headers) - base
            except httpx.TransportError:
                raise
            except Exception as exc:                       # noqa: BLE001
                log.debug("counting only the new messages failed, recounting the request: %s", exc)
    if total is None:
        total = await _render_tokens(client, bare, messages, tools, headers)
    if keys:
        _remember(keys[-1], total)
    return total
