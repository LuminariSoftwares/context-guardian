# Third-party notices

Context Guardian is MIT licensed (see [LICENSE](LICENSE)). This file lists what it includes or builds on that came from someone else, and on what terms.

## Code included in this repository

### dsh-compaction-instant 0.1.4 — MIT — author TsFreddie

- Files: `vendor/compiler.js`, `vendor/region.js` — vendored **unmodified**; their original file headers are kept.
- md5: `e74bb51763795abd1a839e61de4875fa` (compiler.js), `dc719f0745a532dd80ef1a3125f3bf6d` (region.js).
- Licence text and copyright notice: [`vendor/LICENSE.dsh-compaction-instant`](vendor/LICENSE.dsh-compaction-instant).
- Used by: `engine.js` imports `compiler.js`. `region.js` is kept beside it for reference and is not loaded.

The compiler itself ports the principle of [VCC](https://github.com/lllyasviel/VCC) (`skills/conversation-compiler/scripts/VCC.py`, lllyasviel), as its own header states.

## Ideas, with no code included

- **[dsh-openwolf](https://github.com/hawk2048/dsh-openwolf)** (MIT): snapshot session state immediately before a compaction. `engine.js` writes its own `precompact-<seq>.json` and a `FILES WRITTEN` list; it was written from a description of the behaviour, not from openwolf's source.
- **dsh-compaction-instant**: the model-facing `recall` / `search` contract (`type: seq | result | checkpoint`). `cg_recall.js` is an independent implementation written against a contract in [`docs/contracts/CONTRACT_cg_recall.md`](docs/contracts/CONTRACT_cg_recall.md).

## Runtime dependencies (not bundled)

- `@deepseek-ai/schemastery` (MIT) — configuration schema for the DSH bundle entry (`index.js`). `engine.js` has no dependencies.
- Python: `fastapi`, `httpx`, `uvicorn` and friends per `requirements.txt`, each under its own licence.

If you believe something here is attributed wrongly or incompletely, please open an issue — it will be fixed first and discussed second.
