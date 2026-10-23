# Support

context-guardian is maintained by one person, in the evenings, on a best-effort basis. Here is what that means in practice.

**Bugs and questions:** open an issue using the template. You will get an acknowledgement within a day. Reproducing and fixing takes longer; the issue will say what is happening.
**Security:** use GitHub's private vulnerability reporting (Security tab → Report a vulnerability). Please do not open a public issue for it.
**What helps most:** the output of `context-guardian --version`, your OS and Python version, the exact command, and the full output. The bug template asks for these.
**What this project will not do:** add runtime dependencies beyond the proxy's current ones (FastAPI, Uvicorn, httpx and python-dotenv; the MCP server stays standard-library only, see `docs/adr/0006`), refuse non-loopback upstreams. Feature requests are welcome and may be answered "later" or "not here" with the reason.
**Where things are discussed:** issues on this repository. There is no Discord, mailing list or paid support.
