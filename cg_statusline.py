#!/usr/bin/env python3
"""One-line context usage from a running Context Guardian, for a client's status line.

    context-guardian-statusline --url http://127.0.0.1:8786 [--session ID]
                                [--format claude|opencode|plain] [--timeout SECONDS]

Prints `ctx <pct>% (<used>/<window>) <exact|est>` from GET <url>/guardian/usage and
exits 0. Any failure (unreachable, timeout, non-200, bad JSON) prints `ctx ?` and
exits 1: a status line must never hang or crash the client that runs it.

Standard library only. `--format claude` reads and ignores the JSON Claude Code
pipes in; every format prints the same line.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8786"
DEFAULT_TIMEOUT = 2.0
_MAX_BODY = 65536


def k(n: int) -> str:
    """1234 -> 1.2k, 32000 -> 32k, 950 -> 950."""
    if n < 1000:
        return str(n)
    text = "%.1f" % (n / 1000)
    return (text[:-2] if text.endswith(".0") else text) + "k"


def render(body: dict) -> str:
    used, window, pct = body["used_after_compaction"], body["window"], body["pct"]
    for v in (used, window, pct):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
    return "ctx %d%% (%s/%s) %s" % (round(pct), k(int(used)), k(int(window)),
                                    "exact" if body.get("count_source") == "tokenize" else "est")


def fetch(url: str, session: str, timeout: float) -> dict:
    base = url.rstrip("/")
    if urllib.parse.urlparse(base).scheme not in ("http", "https"):
        raise ValueError("url must be http or https")
    target = base + "/guardian/usage"
    if session:
        target += "?" + urllib.parse.urlencode({"session": session})
    # No environment proxy: this is a call to the local proxy, never through one.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(target, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError("status %s" % resp.status)
        body = json.loads(resp.read(_MAX_BODY).decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("not an object")
    return body


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None) -> int:
    ap = _Parser(prog="context-guardian-statusline",
                 description="Print the proxy's own context usage as one status line.")
    ap.add_argument("--url", default=DEFAULT_URL, help="proxy base URL (default %(default)s)")
    ap.add_argument("--session", default="", help="session id (default: the most recent session)")
    ap.add_argument("--format", choices=("claude", "opencode", "plain"), default="plain",
                    help="claude reads and ignores stdin; the line is the same for every format")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds before giving up (default %(default)s)")
    try:
        args = ap.parse_args(argv)
        if args.format == "claude" and not sys.stdin.isatty():
            sys.stdin.read()
        if not args.timeout > 0:
            raise ValueError("timeout must be positive")
        print(render(fetch(args.url, args.session, args.timeout)))
        return 0
    except SystemExit:
        raise
    except Exception:                                      # noqa: BLE001
        print("ctx ?")
        return 1


if __name__ == "__main__":
    sys.exit(main())
