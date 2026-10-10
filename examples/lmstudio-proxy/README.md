# LM Studio + Context Guardian proxy
1. In LM Studio load a model, note its Context Length, start the local server (port 1234), and set `GUARDIAN_NUM_CTX` in `guardian.env` to that length.
2. From the repo root, after `pip install -r requirements.txt`: `python examples/run_proxy.py examples/lmstudio-proxy/guardian.env`
3. Smoke, in a second terminal: `python examples/smoke_proxy.py examples/lmstudio-proxy/guardian.env` → `smoke_proxy: 3 checks, 3 passed, 0 failed`
4. Point your CLI at it: `OPENAI_BASE_URL=http://localhost:8787/v1` (8787, so it can run beside the Ollama example)
