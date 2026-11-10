# 0010. Launcher: `context-guardian launch <client>` points one client at the proxy for one process

**Status:** proposed (this change). Item D of the 2026-11 feature batch.

## Context

Pointing a client at the proxy means setting a base-URL variable or editing the client's settings, starting the proxy in
another terminal, and remembering to stop it. `tests/clients/run_clients.py` (2026-10-31) pointed aider, OpenCode, Continue
(`cn`) and Claude Code at a proxy and records PASS rows. A launcher that does the same for one process removes the setup
step without touching anything the user owns.

## Decision

`context-guardian launch <client> [options] [-- client arguments]`, in `cg_launch.py` (standard library). It is dispatched
**first** in `context_guardian.main()` (before the `--help` / `--version` scan, as `mcp` is), so `launch claude -- --help`
reaches Claude Code, not the proxy; the usage text lists it. It does exactly this:

1. **Resolve the proxy address the way the proxy does.** `GUARDIAN_PORT` / `GUARDIAN_HOST` from the environment, then the
   data directory's `.env` (`cg_doctor.read_env_file`; the proxy loads `.env` with `override=False`), default 8786. The URL the
   client gets is always `http://127.0.0.1:<port>`; a non-loopback `GUARDIAN_HOST` is never put in a client's environment (a
   proxy we start is told `GUARDIAN_HOST=127.0.0.1` and `GUARDIAN_PORT=<port>` explicitly). `--port` and `--url` override.
2. **Find or start the proxy.** If `/guardian/stats` answers (it carries a `version` key; polled with no environment proxy),
   use it and leave it running. If nothing answers, start `context_guardian.py` with the current environment as a child in
   its own session (POSIX `start_new_session=True`, Windows `CREATE_NEW_PROCESS_GROUP`) so the Ctrl-C a user presses to cancel a
   turn in the client never reaches it; its output goes to a temp file. Wait up to 60 s for `/guardian/stats` (the run_clients
   timeout). If the port is held by something that does not answer as a guardian, start ours on a free port (retry once on a
   bind race). A proxy that never becomes ready: the last output lines are printed, the **client is not started**, exit 2, and
   the message prints the command that starts the client directly. This is a deliberate reading of ADR 0001: 0001 protects a
   *request* from the proxy; here the user asked for a protected session, and silently running the client without it would
   send Claude Code to the vendor's API instead of the local backend, a privacy change. `--direct-if-proxy-fails` opts in.
3. **Run the client with the variables set for that one process.** A copy of the environment, plus the client's variables, plus
   `127.0.0.1,localhost` appended to `NO_PROXY`/`no_proxy` (never replaced), so a corporate `HTTP(S)_PROXY` cannot route local
   traffic away. Nothing is exported to the shell and no user file (shell profile, client settings, project) is written.
   The only files created are the proxy's output temp file and, for `continue`, a temp config; both removed at the end.
4. **Signals and shutdown.** While the client runs, the launcher handles SIGINT with a no-op *function* (never `SIG_IGN`, which the client would inherit across `exec` and lose its Ctrl-C; a test checks the client's disposition),
   forwards SIGTERM/SIGHUP to the client, and cleans up afterwards in a `finally`, whatever happened. It terminates a proxy it
   started: POSIX SIGTERM, wait 5 s, SIGKILL; Windows CTRL_BREAK_EVENT, wait, then `terminate()`, then `kill()`. A proxy that
   was already running is never touched. Windows has no SIGHUP: closing the console sends CTRL_CLOSE_EVENT to every process on it, the proxy included, so nothing is orphaned there. The exit code is the client's; a client killed by signal N exits 128+N; launcher
   problems exit 2; a missing client binary exits 127. Cleanup failures are printed to stderr and never change the exit code.
   Limit, stated: `SIGKILL` of the launcher cannot be caught and leaves an idle loopback proxy running.

**Clients.** A table in `cg_launch.py`; the same status words are printed by `context-guardian launch --list`:

| client | how | status |
|---|---|---|
| `claude-code` (`claude`) | `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`; prints a note that the proxy's `GUARDIAN_UPSTREAM_URL` must serve the Anthropic Messages API | the variable passed the 2026-10-31 matrix row (2.1.302); the launcher itself is run against the installed Claude Code in this PR's evidence |
| `openclaude` | `CLAUDE_CODE_USE_OPENAI=1`, `OPENAI_BASE_URL=.../v1`; `OPENAI_API_KEY=local` only if unset; warns when `OPENAI_MODEL` is unset | README Compatibility (tested by hand there); launcher untested |
| `aider` | `OPENAI_API_BASE=.../v1`, `OPENAI_API_KEY=local` only if unset; warns that `--model openai/<model>` is needed | the matrix passed with `--openai-api-base`; the environment form is from aider's documentation: **untested** |
| `continue` (`cn`) | `cn --config <temp yaml>` with one OpenAI model (needs `--model NAME`); the temp file is removed at the end; the launcher says that this replaces the user's own Continue config (models, rules, MCP servers) for that run | argv and yaml shape are the ones run_clients proved; launcher untested |
| `openai` | `launch openai -- <binary> [args]`: sets `OPENAI_BASE_URL` and `OPENAI_API_BASE` for any CLI that reads either | **untested** |
| `opencode`, `cline`, `cursor` | need a setting in the client's own config or UI: the exact steps from the README are printed, the client is not started, nothing is changed; exit 2 | steps only |

*Deviation from the brief's `--apply` pattern, stated for the maintainer to accept:* the brief says a client that needs a
settings-file edit should follow `context-guardian-claude-code install` (dry run, `--apply` with a backup). Every client above
that can be pointed at the proxy for one process is; the three left are a JSON file the user owns (`opencode.json`) and two GUI
apps, and an `--apply` that edits `opencode.json` is a separate, riskier change left as an open item. Print-only changes nothing,
which is the stricter reading of "change no user config files".

The proxy's own settings (upstream URL, window) come from its normal environment/`.env`; the launcher guesses no model or window.

**DSH setup.** The npm bundle gains `npm run launch -- <client> [args]`: `launch.mjs` finds Python the way `index.js` does
(`GUARDIAN_PYTHON`, a `.venv` beside the package, `python`/`python3`), first checks that `fastapi`, `uvicorn` and `httpx` import
(the bridge's Python often lacks them) and otherwise prints `pip install context-guardian` and stops, then runs `cg_launch.py`
with stdio inherited, in `INIT_CWD` (npm runs scripts in the package directory), handling SIGINT with a no-op listener (`process.on('SIGINT', noop)`, never an ignored signal) so Ctrl-C reaches the
client only; its pre-check names the Python it tried in the install hint; on Windows `npm run` goes through `npm.cmd`, which can show cmd.exe's "Terminate batch job" prompt on Ctrl-C, so `node launch.mjs <client>` is the documented way to start it there (not tested on Windows in this PR). The DSH engine needs no proxy; nothing in the engine changes. `launch.mjs` is in `package.json` `files` and in the
`check` script.

## Failure modes

Port race (the child exits; reported as above); client not on PATH (127; a proxy we started is stopped); Windows `.cmd` shims are
resolved with `shutil.which` (they run through cmd.exe: the 8,191-character command line limit the README already notes and the
"Terminate batch job" prompt apply, documented); a client that forks and detaches (the launcher waits on the process it started);
terminal closed (SIGHUP forwarded, cleanup runs); the readiness poll raising is a launcher problem: the client is not started and
the proxy we started is stopped.

## Tests (written first)

Pure: per-client environment (only the documented variables, a copy, `OPENAI_API_KEY` only when unset, `NO_PROXY` appended),
steps text, argument parsing (`--` separator, `--port`, `--url`, `--no-start`, `--list`), `.env` port resolution, the loopback-only
URL, exit-code mapping. With a fake client (a Python script) and a real proxy subprocess: the variable is visible to the client and
absent afterwards in the parent; proxy started and stopped; an already-running proxy is reused and left running; port held by a
non-guardian -> ours on another port; client exit code and signal code propagated; missing client -> 127; proxy fails to start ->
client not run, exit 2, and with `--direct-if-proxy-fails` the client runs without the variable; a client that cancels with
SIGINT does not take the proxy down (POSIX). **Failure injections:** exceptions in cleanup, the banner write and the temp-file
removal leave the client's exit code intact and no child running; the readiness poll raising does not start the client. HOME,
USERPROFILE and APPDATA are pointed at temp folders and snapshotted (never the real home); the child proxy gets the test run's
`GUARDIAN_DATA_DIR`, span/log/cache paths and `GUARDIAN_VERSION_CHECK=0`; leftover children are killed in a `finally`. Windows:
the signal handlers are unit-tested (no console on CI). Probe from the description: start with no proxy listening, run a fake
client that asks `$ANTHROPIC_BASE_URL/guardian/stats`, expect its request to be counted, then expect the port closed after the
launcher returns; also run against the installed Claude Code and a mock upstream.

## Alternatives

Writing the variable into a shell profile or settings (persistent, the thing to avoid); a background daemon (another process to
manage); running the proxy in-process on a thread (a proxy crash takes the launcher down and shares the client's terminal).

## Consequences

One command replaces three setup steps for the clients above and leaves nothing behind. Untested clients are labelled so in the
CLI output and the README, as the Compatibility table does. The README sentence "none of these four has been tested" is corrected
to point at the Works-with rows.

## Review log

Design review by an independent Opus reviewer, two rounds.
Round 1 (*revise*): blocking: the proxy would have died on a user's Ctrl-C on POSIX (same process group), no SIGTERM/SIGHUP handling,
`.env` port ignored, loopback not exempt from `HTTP(S)_PROXY`, `launch.mjs` would run in the package directory with a Python lacking
fastapi, Windows stop sequence, packaging omissions. Non-blocking adopted: dispatch before the `--help` scan, 60 s readiness,
OpenClaude/aider model variables, Anthropic-upstream note, 128+N exit codes, GUI clients listed as steps, `continue` via a temp config,
the `openai` row defined, test isolation (HOME/USERPROFILE/APPDATA, ports, leftover children), README contradiction.
Round 2: one blocking item left: ignoring SIGINT with `SIG_IGN` is inherited by the client on POSIX; a function handler is used instead,
with a test on the client's disposition, and the same in `launch.mjs`. The reviewer accepted both judgement calls for the maintainer to
sign off: print-only for the settings clients (the brief's `--apply` pattern is not followed for `opencode.json`; an `--apply` for it is an
open item) and refusing to start the client when the proxy will not start (opt-in `--direct-if-proxy-fails`).
