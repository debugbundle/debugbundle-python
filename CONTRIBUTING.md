# Contributing

## Development Workflow

1. Create a feature branch from `main`.
2. Implement using Red/Green TDD.
3. Install the package with dev dependencies: `python -m pip install -e .[dev]`.
4. Run local checks:
   - `ruff check src tests`
   - `mypy src`
   - `pytest --cov=src/debugbundle --cov-report=term-missing --cov-report=json:coverage.json -q`
   - `python scripts/check_coverage.py coverage.json`
   - `python -m build`
5. Update docs when behavior or public integration guidance changes.
6. Open a pull request with validation evidence.

## Rules

- Keep the SDK fail-closed around validation and fail-open toward host applications.
- Do not add backwards-compatibility shims during pre-production.
- Keep framework adapters thin over the shared SDK core.