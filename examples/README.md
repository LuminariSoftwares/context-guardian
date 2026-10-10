# Examples

| folder | backend | start | smoke |
|---|---|---|---|
| [ollama-proxy](ollama-proxy/) | Ollama `:11434` | `python examples/run_proxy.py examples/ollama-proxy/guardian.env` | `python examples/smoke_proxy.py examples/ollama-proxy/guardian.env` |
| [lmstudio-proxy](lmstudio-proxy/) | LM Studio `:1234` | `python examples/run_proxy.py examples/lmstudio-proxy/guardian.env` | `python examples/smoke_proxy.py examples/lmstudio-proxy/guardian.env` |
| [dsh-preset](dsh-preset/) | any model DSH drives | `npm run setup -- --apply` | `node examples/dsh-preset/smoke.mjs` |

vLLM, LiteLLM or llama.cpp server: copy `ollama-proxy/guardian.env` and change `GUARDIAN_UPSTREAM_URL` to that server's `/v1` URL and `GUARDIAN_NUM_CTX` to its context length.
`run_proxy.py` and `smoke_proxy.py` use the Python standard library only (plus the proxy's own `requirements.txt`). Run every command from the repo root.
