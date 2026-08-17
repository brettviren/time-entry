# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12 CLI package using a `src` layout. The implementation and Click entry point live in `src/time_entry/__init__.py`; keep pure allocation and file-I/O logic separate from the asynchronous Playwright automation in that module. Tests are in `test/test_time_entry.py`. User instructions live in `README.org`, while `docs/time-entry.md` records Workday selectors, SPA behavior, and the browser-automation roadmap. Packaging and dependencies are defined in `pyproject.toml`; lint and formatting policy is in `ruff.toml`.

## Build, Test, and Development Commands

- `uv tool install -e .` installs the `time-entry` command from the working tree.
- `uv run time-entry --help` exercises the local CLI without a separate install.
- `uv run pytest` runs the complete non-browser test suite.
- `uv run pytest -k allocation` runs a focused subset by test-name expression.
- `ruff check .` runs the repository's configured lint checks.
- `ruff format .` formats Python code.
- `time-entry install-browser` installs the Chromium revision matching Playwright; use this instead of a system Playwright command.

## Coding Style & Naming Conventions

Use four spaces, double-quoted strings, and Ruff formatting. Follow standard Python naming: `snake_case` for functions and variables, `PascalCase` for dataclasses, and `UPPER_CASE` for constants. Keep Click command adapters thin and place reusable behavior in testable helpers. Preserve multi-variant Workday selectors and deliberate SPA settle waits unless verified against the live tenant.

## Testing Guidelines

Pytest discovers `test_*.py` files under `test/`; name individual tests `test_<behavior>`. Add regression tests for allocation, date handling, config/record I/O, output, and CLI wiring. Tests touching configuration or state must redirect XDG paths to `tmp_path` so they never write to a developer's real account. Browser flows require manual verification: run `get`, `diff`, `apply` as a dry run, then `apply --yes`, re-run `diff`, and invoke `submit --yes` only after checking the result.

## Commit & Pull Request Guidelines

Recent commits use short, imperative, sentence-case subjects such as `Improve first-run setup errors`. Keep each commit focused and explain non-obvious behavior in the body. Pull requests should summarize the change, list verification commands, and call out config/state migrations. For Workday UI changes, include sanitized selector evidence or screenshots and the tested tenant flow; never commit auth state, real project codes, or captured pages containing sensitive data.
