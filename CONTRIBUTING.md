# Contributing to claude-auth-shift

Thank you for your interest in contributing!

## Getting started

```bash
# Clone and install in dev mode
git clone https://github.com/AX-Surfers/claude-auth-shift.git
cd claude-auth-shift
uv sync

# Run tests
uv run pytest
```

All tests run against an in-memory keychain by default — no real credentials are touched.

## What to work on

- Check [open issues](https://github.com/AX-Surfers/claude-auth-shift/issues) for bugs and feature requests
- Issues labeled `good first issue` are a good starting point
- Open an issue before starting large changes to align on approach

## Development guidelines

- **Tests required** — add or update tests for any functional change
- **No subprocess wrapping** — `hud.py` reads library internals and files directly; don't add new subprocess calls for things that can be imported
- **Fail-open for hooks** — `cshift` (Stop hook) must never exit non-zero; errors are swallowed silently
- **HUD must be fast** — the hot path in `hud.py` must return in under 100 ms; heavy work belongs in the background `--refresh` subprocess
- **Platform awareness** — credential I/O routes through `credentials.py`; don't hardcode macOS-only paths

## Submitting a PR

1. Fork the repo and create a branch from `main`
2. Make your changes with tests
3. Run `uv run pytest` — all tests must pass
4. Open a PR against `main` and fill in the PR template

## Reporting bugs

Use the [bug report template](https://github.com/AX-Surfers/claude-auth-shift/issues/new?template=bug_report.md). Include your OS, Python version, and the output of `cswap --version`.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
