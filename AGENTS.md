# Repository Guidelines

## Project Structure & Module Organization

The repository contains a focused Capital IQ browser-automation project. The main implementation is in `capitaliq-automation/capitaliq-automation/`:

- `capitaliq_screener_export.py` is the maintained, configuration-driven batch exporter.
- `recorded.py` is a raw Playwright recording and is useful as selector reference material.
- `capitaliq_profile/` stores persistent Chromium session data. Treat it as local runtime state, not source code.

Root-level `package.json` and `package-lock.json` pin the Playwright tooling. Generated downloads, checkpoints, screenshots, virtual environments, and browser-profile files should not be committed.

## Setup, Test, and Development Commands

- `npm install` installs the repository's Node-based Playwright tooling.
- `python -m pip install playwright` installs the Python API used by the scripts.
- `python -m playwright install chromium` installs the required browser binary.
- `python capitaliq-automation/capitaliq-automation/capitaliq_screener_export.py --config path/to/config.json --dry-run` validates configuration and selects one batch without exporting it.
- `python -m py_compile capitaliq-automation/capitaliq-automation/*.py` performs a quick syntax check.

There is currently no functional `npm test` command; it intentionally exits with an error. Running the exporter requires valid Capital IQ access and an interactive login.

## Coding Style & Naming Conventions

Use Python 3, four-space indentation, type hints, and concise docstrings. Follow PEP 8: `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for module constants. Keep selectors and environment-specific values in `config.json` rather than embedding them in workflow logic. Prefer `pathlib.Path`, structured logging, and explicit timeouts. Preserve resumability by writing state atomically, as `save_state` does.

## Testing Guidelines

No automated test suite or coverage threshold is configured. For each change, run the syntax check and exercise `--dry-run` against a prepared screener. Test pure helpers separately when practical, especially batching, state loading, and result-count parsing. If adding pytest tests, place them under `tests/` and name files `test_*.py`.

## Commit & Pull Request Guidelines

This checkout has no commit history from which to infer a convention. Use short, imperative subjects such as `Handle empty screener results`. Keep commits focused. Pull requests should explain the workflow change, list manual verification performed, and attach screenshots for UI or selector changes. Never include credentials, downloaded proprietary data, cookies, or browser-session artifacts.
