# Architecture decision records

Short records of the choices already in the code, written after the fact from the source and the commit history
(each one cites where). Format: context, decision, alternatives, consequences.

| # | Decision | Status |
|---|---|---|
| [0001](0001-fail-open.md) | Fail open: any compaction failure forwards the client's request unchanged | accepted (0.4.0, widened 0.10.0) |
| [0002](0002-estimate-tokens-from-characters-with-calibration.md) | Estimate tokens from characters, calibrate per model from reported usage | accepted (0.7.0) |
| [0003](0003-archive-originals-as-plain-json-spans.md) | Archive what compaction removes as plain JSON spans on disk | accepted (0.3.0) |
| [0004](0004-two-front-doors-one-archive.md) | Two front doors (HTTP proxy, native DSH engine) sharing one archive format | accepted (DSH engine 0.1.0-alpha.2) |
| [0005](0005-keep-recent-by-token-budget.md) | Keep recent messages by token budget, never splitting a tool call from its result | accepted (0.6.0) |
| [0006](0006-stdlib-read-only-mcp-server.md) | MCP server: stdlib only, hand-written JSON-RPC, read-only tools | accepted (0.10.0) |
| [0007](0007-seal-the-archive-with-a-hash-chain.md) | Seal the archive with a hash chain; tamper-evident, anchored outside | accepted (0.12.0) |

Add a record when a change makes one of these false, or makes a new choice a reviewer would ask "why?" about.
Supersede rather than edit: mark the old one `superseded by NNNN`.
