# 0006. MCP server: stdlib only, hand-written JSON-RPC, read-only

**Status:** accepted. 0.10.0 (`62146ee`).

## Context

Users asked to see what the proxy had done from inside an MCP client instead of a terminal. The server is launched
by the client, often through `uvx`, on every session start, and it must not be able to make the proxy's behaviour
worse.

## Decision

`cg_mcp.py` implements the MCP stdio transport directly: newline-delimited JSON-RPC 2.0, `initialize`, `ping`,
`tools/list`, `tools/call`, protocol versions 2025-06-18 / 2025-03-26 / 2024-11-05. Standard library only. Four
tools, all read-only and annotated so (`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`;
`openWorldHint` true only for `get_proxy_status`, the one tool that opens a socket, to loopback). Tool names are
`verb_noun`; a renamed tool keeps its old name as an unlisted alias (`guardian_status`, the 0.10.0 name). Inputs that become
paths are validated against `^[A-Za-z0-9_-]+$` before they touch the filesystem, and `read_span` output is capped at
20,000 characters. Tool failures are tool results with `isError: true`, not protocol errors, so the client's model
can read them.

## Alternatives

- **The official `mcp` Python SDK**: less code and new protocol features for free, but a dependency tree
  (pydantic, anyio, starlette...) for three small tools, and a Python floor set by the SDK rather than by us.
- **Expose write tools** (trigger a compaction, change settings): useful, but a model could then change the proxy
  that guards its own context; not worth it until there is a concrete need and an approval story.
- **MCP resources instead of tools**: fewer clients support them well.

## Consequences

- Starts fast, installs nothing extra, and runs under any Python 3.10+.
- New protocol features (structured output, resources, prompts) must be added by hand; `PROTOCOL_VERSIONS` must be
  bumped deliberately.
- The schemas in `cg_mcp.TOOLS` are the single source for the README reference and the quality tests.
