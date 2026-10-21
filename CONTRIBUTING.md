# Contributing

Install the development dependencies in an isolated environment:

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The `dev` extra in `pyproject.toml` pins the tool versions (pytest,
pytest-asyncio, ruff, cryptography), so a contributor reproduces the same run.
Runtime dependencies stay bounded in `pyproject.toml`. Bump a pin only with the
gate green.

Then run the gate:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts examples
```

## Rules

- **Tests never use the network.** Network and live-provider behavior is tested
  with injected transports or fixtures.
- **Unknown is never zero.** Preserve `UNKNOWN`/`NULL` semantics; never
  substitute a placeholder for a value that was not reported.
- **Never store prompt or response text** — and do not add credentials or
  request content to tests, fixtures, logs, or commits.
- **Forward requests byte-identical** unless active routing mode rewrites them.
- **Keep type hints and ruff clean.**

`.gitattributes` and `.editorconfig` fix line endings and editor defaults so a
change looks the same on every platform; nothing in them is optional style.
