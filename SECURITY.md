# Security Policy

## Supported Versions

Security updates are provided for:
- The latest release on npm (`dsh-context-guardian`)
- The latest release on PyPI (`context-guardian`)

Older versions are not supported. Please update to the latest release to receive security fixes.

## Reporting a Vulnerability

Do not open a public issue to report a security vulnerability.

Use GitHub's private vulnerability reporting at: https://github.com/LuminariSoftwares/context-guardian/security/advisories/new

When reporting, please include:
- The version you are running (npm and/or PyPI version)
- Your installation path and setup
- Steps to reproduce the vulnerability
- Expected vs. actual behavior
- The security impact (data exposure, session hijacking, etc.)

## Response Timeline

As a volunteer-maintained project, we make a best-effort commitment to:
- Acknowledge receipt of your report within 7 days
- Assess the vulnerability and work on a fix
- Release a patched version as soon as feasible

## Things to Know

context-guardian sits in the path of every prompt, so its logs, checkpoints, and memory.json may contain conversation content. Before sharing logs or diagnostic output publicly:
- Redact any sensitive conversation content
- Remove API keys, tokens, and personal information
- Consider whether the data reveals anything about your project

If you need to share diagnostic output for a bug report, review it carefully first.
