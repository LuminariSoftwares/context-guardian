"""Start the Context Guardian proxy with the settings in one example's guardian.env.

usage: python examples/run_proxy.py examples/ollama-proxy/guardian.env

Standard library only. Each KEY=VALUE line becomes an environment variable
unless that variable is already set (a real environment variable wins, the same
rule the proxy uses for its own .env). Works from a clone, or with
`pip install context-guardian` and this file run from anywhere.
"""
import os
import sys
from pathlib import Path


def load_env_file(path):
    loaded = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip())
        return 2
    loaded = load_env_file(argv[0])
    print("run_proxy: %d settings from %s" % (len(loaded), argv[0]))
    # A clone has context_guardian.py one folder up; a pip install has it on sys.path already.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import context_guardian  # reads the GUARDIAN_* variables at import

    context_guardian.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
