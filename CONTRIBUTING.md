# Contributing

Install the development dependencies in an isolated environment, then run:

```sh
python -m pytest -q
ruff check src tests scripts
```

Keep changes focused, preserve `UNKNOWN`/`NULL` semantics, and do not add
credentials or request content to tests, fixtures, logs, or commits. Network
and live-provider behavior must be tested with injected transports or fixtures.
