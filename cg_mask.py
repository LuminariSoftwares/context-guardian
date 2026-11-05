"""cg_mask.py -- mask key-shaped values in what recall shows.

Recall prints archived text back to a model or a terminal. Archived text can
hold an API key someone pasted. mask() replaces the value of anything
key-shaped with its first 3 characters, "****", and its last 4, so the model
or the screen never receives the whole secret. The archive on disk, its seal
and the span metadata are never touched: only the text on its way out.

The idea of a small set of provider-key and secret-assignment shapes comes
from code-guardian's code_guardian/scanners.py (same author, MIT); the
patterns here are rewritten from the idea, not copied. The two sets differ on
purpose: code-guardian flags secrets in source literals, while this module
redacts prose and tool output, so it adds assignment and long-run shapes.

Stdlib only. MASK_OFF (any value but empty, 0, false, no) disables masking and
is read on every call. mask("") is "", mask(mask(x)) == mask(x), and ordinary
prose and URLs come back unchanged. Fail open: an exception inside mask
returns the text as it came in and is logged to stderr once.
"""
from __future__ import annotations

import os
import re
import sys

# Provider key shapes (rewritten from the idea in code-guardian's scanners.py).
_PROVIDER = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"
    r"|(?:A3T[A-Z0-9]|AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|glpat-[A-Za-z0-9_\-]{18,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{12,}"
    r"|AIza[A-Za-z0-9_\-]{30,}"
    r")(?![A-Za-z0-9])"
)

# name=value / name: value / "name": "value" where the name says it is secret.
# Covers AWS secret pairs and long base64 or hex runs after a secret-named key.
# Added for prose and tool output; code-guardian does not need these shapes.
_ASSIGN = re.compile(
    r"(?P<head>(?<![A-Za-z0-9])[A-Za-z0-9_\-.]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|"
    r"private[_\-]?key|credential|auth)[A-Za-z0-9_\-.]*[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<val>[A-Za-z0-9+/_\-.=~*!@#$%^]{8,})",
    re.IGNORECASE,
)

_logged = False


def _enabled() -> bool:
    return os.environ.get("MASK_OFF", "").strip().lower() in ("", "0", "false", "no")


def _shape(value: str) -> str:
    return value[:3] + "****" + value[-4:]


def _assign_sub(m: "re.Match[str]") -> str:
    val = m.group("val")
    # A bare word ("password: required") is prose; a secret has a digit or symbol.
    if val.isalpha():
        return m.group(0)
    return m.group("head") + _shape(val)


def mask(text: str) -> str:
    global _logged
    if not text or not isinstance(text, str) or not _enabled():
        return text
    try:
        out = _PROVIDER.sub(lambda m: _shape(m.group(0)), text)
        return _ASSIGN.sub(_assign_sub, out)
    except Exception as exc:  # noqa: BLE001 -- fail open: a mask bug must not break recall
        if not _logged:
            _logged = True
            print("cg_mask: masking failed, showing text unmasked: %s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
        return text
