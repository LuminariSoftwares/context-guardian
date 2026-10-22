"""child_env(): the environment a test subprocess should be given.

NOT collected by pytest (no test_ prefix).

A from-scratch dict looks tidy and is a Windows failure waiting to happen:

    env = {"PATH": "/usr/bin:/bin", "GUARDIAN_NUM_CTX": "32768"}

On Linux and macOS that child starts. On Windows it dies before its first
line. CPython needs SYSTEMROOT to find the system DLLs, and the Winsock
provider catalogue lives behind it -- so `import asyncio` (which
`context_guardian.py` reaches through fastapi -> starlette) raises

    OSError: [WinError 10106] The requested service provider could not be
    loaded or initialized

at `import _overlapped`. The POSIX PATH is the same mistake twice: there is no
/usr/bin on Windows. Measured on CI 2026-10-22, run 39710228357: the four
`context-guardian --help` / `--version` checks failed this way and nothing
else did, because the two other from-scratch children here (cg_mcp.py and
cg_doctor.py) are stdlib-only and never reach asyncio -- latent, not fixed.

So: start from the real environment, drop what must not leak, add what the
test sets. Dropped always: every GUARDIAN_* variable, because conftest.py sets
three of them for the whole session and a child that inherited them would not
be exercising its own defaults. A test that needs the HOME lookup isolated as
well pops HOME / USERPROFILE / LOCALAPPDATA from what this returns -- no test
needs that today, because the ones that care pass GUARDIAN_DATA_DIR instead.
"""
import os

GUARDIAN_PREFIX = "GUARDIAN_"


def child_env(pythonpath=None, **overrides):
    """os.environ minus every GUARDIAN_*, plus `overrides`.

    `pythonpath` is PREPENDED to any inherited PYTHONPATH rather than
    replacing it, so a child that needs the repo importable does not lose
    whatever the runner put there.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(GUARDIAN_PREFIX)}
    if pythonpath is not None:
        prior = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (f"{pythonpath}{os.pathsep}{prior}" if prior else str(pythonpath))
    env.update({k: str(v) for k, v in overrides.items()})
    return env
