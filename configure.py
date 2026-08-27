#!/usr/bin/env python3
"""
configure.py
============
Interactive first-run setup for Context Guardian. Asks a few questions about
YOUR hardware/backend and writes them to .env, so you never hand-edit a config
file and guess.

    python configure.py

Every question has a sensible default (press Enter to accept). Numeric answers
are validated, so a typo tells you rather than crashing Guardian at startup.
Re-run any time to change your answers, or edit .env directly.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT / ".env"

# Context-window presets, smallest to largest, with rough guidance.
#
# The VRAM notes are DELIBERATELY approximate and framed for the audience this
# tool serves: someone running a quantized ~7-14B model on a consumer GPU. The
# context window drives the KV-cache size, which is only PART of your VRAM use
# (the model weights are the other, larger part), and it scales with the
# model's size too -- a 30B model at 32K needs far more than an 8B at 32K. So
# treat these as "you'll want at least this much headroom on a typical mid-size
# model", not a guarantee. The real number for YOUR setup comes from
# `ollama ps` while your model is loaded. Bigger model => bump a tier.
CONTEXT_OPTIONS = [
    (4096,   "Light use / short chats.        Roughly 8 GB+ VRAM on a small model."),
    (8192,   "Everyday agent use.             Roughly 10 GB+ VRAM."),
    (16384,  "Comfortable coding/agent work.  Roughly 12 GB+ VRAM."),
    (32768,  "Full agent sessions (default).  Roughly 12-16 GB VRAM on a 7-14B model."),
    (65536,  "Large context.                  Roughly 20-24 GB VRAM, or heavy quant."),
    (131072, "Very large context.             24 GB+ VRAM / aggressive quantization."),
]
DEFAULT_CTX = 32768
STANDARD_CTX = {tok for tok, _ in CONTEXT_OPTIONS}


def ask_str(prompt: str, default: str) -> str:
    print("\n" + prompt)
    return input(f"  [{default}]: ").strip() or default


def ask_int(prompt: str, default: int, lo: int = 1, hi: int = 10_000_000) -> int:
    """A validated positive integer. Re-asks on garbage instead of writing a
    value that makes Guardian throw ValueError at import."""
    while True:
        raw = input(f"\n{prompt}\n  [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = int(raw.replace(",", "").replace("_", ""))
        except ValueError:
            print(f"  '{raw}' is not a whole number. Digits only "
                  f"(e.g. 8192), no units or letters.")
            continue
        if not (lo <= val <= hi):
            print(f"  {val} is out of range. Expected {lo}..{hi}.")
            continue
        return val


def ask_float(prompt: str, default: float, lo: float, hi: float) -> float:
    while True:
        raw = input(f"\n{prompt}\n  [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            print(f"  '{raw}' is not a number.")
            continue
        if not (lo <= val <= hi):
            print(f"  {val} is out of range. Expected {lo}..{hi}.")
            continue
        return val


def ask_context() -> int:
    """A menu of context-window presets with VRAM guidance, plus a custom
    option that CONFIRMS a non-standard value back to the user."""
    print("\nContext window size (in TOKENS) of the model you're actually running.")
    print("This is the single most important setting -- it MUST match how your")
    print("backend/model is really configured (check `ollama ps` while loaded),")
    print("not the model's theoretical max.\n")
    for i, (tok, blurb) in enumerate(CONTEXT_OPTIONS, 1):
        star = "  <- default" if tok == DEFAULT_CTX else ""
        print(f"  {i}) {tok:>7,} tokens   {blurb}{star}")
    print(f"  {len(CONTEXT_OPTIONS) + 1}) other        Enter a custom number")

    default_idx = [i for i, (t, _) in enumerate(CONTEXT_OPTIONS, 1)
                   if t == DEFAULT_CTX][0]
    while True:
        raw = input(f"\n  Choose 1-{len(CONTEXT_OPTIONS) + 1} "
                    f"[{default_idx}]: ").strip()
        if not raw:
            return DEFAULT_CTX
        if not raw.isdigit():
            print("  Enter the NUMBER of an option above.")
            continue
        choice = int(raw)
        if 1 <= choice <= len(CONTEXT_OPTIONS):
            return CONTEXT_OPTIONS[choice - 1][0]
        if choice == len(CONTEXT_OPTIONS) + 1:
            # Ceiling deliberately far above any real context window: an
            # unusually large number should reach the CONFIRMATION below
            # (showing the user their actual figure), not be silently rejected
            # as out of range. Letters/negatives are still refused by ask_int.
            custom = ask_int("  Custom context window, in tokens", DEFAULT_CTX,
                             lo=256, hi=100_000_000)
            if custom in STANDARD_CTX:
                return custom
            # Non-standard value: confirm it back with the ACTUAL number, so a
            # fat-fingered 132768 or 13212348 is caught before it is written.
            print(f"\n  {custom:,} is not one of the standard sizes.")
            ok = input(f"  Are you sure you want a context window of "
                       f"{custom:,} tokens? [y/N]: ").strip().lower()
            if ok == "y":
                return custom
            print("  Okay, let's pick again.")
            continue
        print(f"  {choice} is not one of the options.")


def main() -> int:
    print("Context Guardian -- first-run setup")
    print("=" * 40)
    print("Press Enter on any question to accept the default in [brackets].")

    if ENV_PATH.exists():
        if input(f"\n.env already exists at {ENV_PATH}. Overwrite? [y/N]: "
                 ).strip().lower() != "y":
            print("Left the existing .env untouched. Edit it by hand to tweak "
                  "one value.")
            return 0

    answers = {}
    answers["GUARDIAN_UPSTREAM_URL"] = ask_str(
        "OpenAI-compatible backend Guardian should forward to\n"
        "  (your Ollama /v1 endpoint, or another proxy in front of it)",
        "http://localhost:11434/v1")
    answers["GUARDIAN_NUM_CTX"] = str(ask_context())
    answers["GUARDIAN_PORT"] = str(ask_int(
        "Port Context Guardian itself should listen on", 8786,
        lo=1, hi=65535))
    answers["GUARDIAN_COMPACT_THRESHOLD"] = str(ask_float(
        "Fraction of the context window at which Guardian compacts (0.1-1.0)",
        0.85, lo=0.1, hi=1.0))
    answers["GUARDIAN_KEEP_RECENT_MESSAGES"] = str(ask_int(
        "Most-recent messages to always keep verbatim (never summarized)", 8,
        lo=1, hi=1000))
    answers["GUARDIAN_UPSTREAM_TIMEOUT"] = str(ask_int(
        "Seconds to wait for the upstream backend to respond\n"
        "  (raise this if a slow local \"thinking\" model gives 500s only on "
        "real requests)", 600, lo=1, hi=86400))
    answers["GUARDIAN_LOG_PATH"] = ask_str(
        "Where to log compaction events (JSON lines)",
        "logs/context_guardian_log.json")

    lines = ["# Generated by configure.py -- edit freely, or re-run configure.py.\n"]
    for k, v in answers.items():
        lines.append(f"{k}={v}\n")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")

    ctx = int(answers["GUARDIAN_NUM_CTX"])
    print(f"\nWrote {ENV_PATH}")
    if ctx not in STANDARD_CTX:
        print(f"  (custom context window: {ctx:,} tokens)")
    print("\nNext steps:")
    print("  1. pip install -r requirements.txt")
    print("  2. Make sure your real backend is already running.")
    print("  3. python context_guardian.py")
    print(f"  4. Point your CLI's OPENAI_BASE_URL at "
          f"http://localhost:{answers['GUARDIAN_PORT']}/v1")
    print("\nSee README.md's \"Testing before you trust it\" section before "
          "pointing a real session at it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
