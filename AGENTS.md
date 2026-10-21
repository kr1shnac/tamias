# AGENTS.md — Tamias

Tamias is a request-rewriting, OpenAI-compatible HTTP proxy that logs token
usage and cost per request and shadow-routes to a cheaper model by default.

## Setup

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Gate

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts examples
```

## Layout

- `src/tamias/` — package: proxy, pricing, router, store, CLI.
- `tests/` — pytest suite; tests never touch the network.
- `scripts/` — local check and reconciliation helpers.
- `examples/` — example price sheets and synthetic history generator.
- `docs/` — reference documentation; `.github/` — CI and templates.

## Rules

1. Project venv only — there is no bare `python` or `pytest`.
2. Stage explicit paths, never `git add -A`.
3. Never force-push, and never push tags (`git push --tags` / `--follow-tags`).
