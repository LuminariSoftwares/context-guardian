# DeepSeek Harness preset row (native engine, no proxy)
1. `compaction-row.yml` is the one row Context Guardian adds to your agent preset's `compaction` group.
2. Let setup write it for you: `npm run setup` (dry run), then `npm run setup -- --apply` — see [docs/dsh-integration.md](../../docs/dsh-integration.md).
3. Smoke, from a clone, touching only a temp folder: `node examples/dsh-preset/smoke.mjs` → `dsh_preset_smoke: 5 checks, 5 passed, 0 failed`
4. In DSH, open a NEW session with that preset and type `/guardian` to see the engine and your model's window.
