# Ollama + Context Guardian proxy
1. Start Ollama with a model pulled (`ollama pull <model>`), then set `GUARDIAN_NUM_CTX` in `guardian.env` to the CONTEXT column of `ollama ps`.
2. From the repo root, after `pip install -r requirements.txt`: `python examples/run_proxy.py examples/ollama-proxy/guardian.env`
3. Smoke, in a second terminal: `python examples/smoke_proxy.py examples/ollama-proxy/guardian.env` → `smoke_proxy: 3 checks, 3 passed, 0 failed`
4. Point your CLI at it: `OPENAI_BASE_URL=http://localhost:8786/v1`; live view at http://localhost:8786/guardian/health
