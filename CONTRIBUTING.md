# Contributing

Install the development dependencies in an isolated environment:

```sh
python -m venv .venv
.venv/bin/pip install -e . -r requirements-dev.txt
```

`requirements-dev.txt` pins the direct tools (pytest, pytest-asyncio, ruff,
cryptography) to the versions the current gate passed with, so a contributor
reproduces the same run. Runtime dependencies stay bounded in `pyproject.toml`.
Bump a pin only with the gate green.

Then run the gate:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
```

Keep changes focused, preserve `UNKNOWN`/`NULL` semantics, and do not add
credentials or request content to tests, fixtures, logs, or commits. Network
and live-provider behavior must be tested with injected transports or fixtures.
