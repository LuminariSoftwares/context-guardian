#!/usr/bin/env python3
"""Thin wrapper: runs main() of the root module cg_statusline (also installed as
`context-guardian-statusline`). Loaded by path, so it works from a checkout
without installing anything."""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "cg_statusline_impl", Path(__file__).resolve().parent.parent / "cg_statusline.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

if __name__ == "__main__":
    sys.exit(_mod.main())
