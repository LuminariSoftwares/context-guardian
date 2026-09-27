# Contributing to context-guardian

## Welcome

This project exists to make local models usable in long coding sessions by compacting conversation history before the context window fills. The most valuable contributions are bug reports with real numbers—your model name, backend, context size, and token counts before and after context compaction.

## Ways to Help

- **Bug reports** - Include logs showing the issue with real data
- **Benchmark results** - Share performance numbers from your hardware and model setup
- **Documentation fixes** - Improve README, docstrings, or examples
- **Code improvements** - Bug fixes and feature enhancements with tests

## Development Setup

Create and activate a virtual environment, then install dependencies:

**Windows (cmd):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements-dev.txt
npm install
```

**macOS/Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm install
```

## Running the Tests

Run the Python test suite:
```
python -m pytest -q
```

Run the Node.js linting and checks:
```
npm run check
```

Run the Node.js tests:
```
npm run test:node
```

Test the Python-to-Node bridge:
```
python modules/cg_bridge.py --selftest
```

All tests must pass before submitting a pull request.

## Pull Request Checklist

Before submitting, ensure:
- All tests pass (pytest and npm commands above)
- Add an entry under `## Unreleased` in CHANGELOG.md
- No secrets, API tokens, or personal file paths in code, tests, or logs
- README is updated if behavior changed
- For dual-engine changes: both `context_guardian.py` and `engine.js` are updated, or explain why in the PR

## Coding Style

- Prefer the Python and Node standard libraries
- Avoid new runtime dependencies without discussion
- Fail open: if the guardian breaks, pass traffic through untouched rather than break the user's session
- Keep original inputs recoverable—every lossy step must preserve what was removed
- Clear variable names and comments for complex logic

## License

context-guardian is released under the MIT License. Any contributions you make will be licensed under the same license.
