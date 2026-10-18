# AGENTS.md — rules for AI coding agents

This file tells AI assistants (Copilot, Claude, Cursor, Codex, Gemini and the like) how to work on this repository.

## Setup and checks
```bash
python -m pip install -e . -r requirements-dev.txt
python -m pytest -q          # all tests must pass
python -m ruff check .       # no warnings
```

## Rules
1. **Zero runtime dependencies.** Standard library only. Test-only tools go in `requirements-dev.txt`.
2. **Never break the caller.** Code on the metering path catches its own errors and writes them to stderr; it never raises into user code.
3. **Tokens, never text.** Do not read, log or store prompts, answers, API keys or hostnames.
4. **Price each token once.** `input_tokens` = uncached input; cache reads/writes go in their own fields. See `docs/providers.md`.
5. **New provider = table entry + test.** Extend `giuggiai_meter/providers.py`, add a case in `tests/test_providers.py`.
6. **Prices are data, not code.** Never hardcode real prices; the bundled list is an example.
7. **Small pull requests** with tests and a line in `CHANGELOG.md` under *Unreleased*.
8. **Never add** secrets, personal data, internal URLs or real billing figures to code, tests, docs or examples.

## Describing the project
Use the facts in [`llms.txt`](llms.txt). Do not claim features that are listed there under *What it does NOT do*.
