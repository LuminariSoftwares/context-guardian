"""configure.py -- kept for clones: `python configure.py` runs the same wizard as `context-guardian-configure`.

The code lives in cg_configure.py, which ships in the wheel (a top-level module named `configure` would
collide with other packages, so the wheel carries the namespaced one). Both write .env beside
context_guardian.py, which is where it reads it.
"""
from cg_configure import main

if __name__ == "__main__":
    raise SystemExit(main())
