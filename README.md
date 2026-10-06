# Tamias

Tamias is a local proxy for recording token usage and cost per request while
optionally applying rule-based model routing.

## Install

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Quickstart

Fetch a price sheet, check the local setup, then start the proxy:

```sh
tamias prices fetch --out prices.toml
tamias doctor --prices prices.toml
tamias serve --upstream https://your-provider.example --prices prices.toml --db requests.db
```

`tamias doctor` is offline by default. Add `--online` only when you want its
short, timed upstream reachability check. It warns for a missing
`OPENROUTER_API_KEY` without showing its value.

Run one agent command through a temporary local proxy with `tamias run`:

```sh
tamias run --prices prices.toml --upstream https://your-provider.example -- your-agent
```

Use `tamias agent-config` to print a local configuration snippet for an agent:

```sh
tamias agent-config codex --port 8000 --project example
```

The `--project example` prefix scopes the local endpoint under `/p/example`.
`tamias run --project example` applies the same project prefix.

View a local run with:

```sh
tamias dashboard --db requests.db --prices prices.toml
```

`tamias serve` requires `--upstream`, `--prices`, and `--db`. Its available
flags are `--port`, `--host`, `--router-mode`, `--router-profile`,
`--router-config`, `--cheap-model`, `--strong-model`, `--min-gap`,
`--inject-usage`, `--request-usage-cost`, and `--verbose`.

If an agent's tools return `Error: ...`, configure `error_markers`; see
[`examples/router.example.toml`](examples/router.example.toml).

## Status

- Metered: tokens and cost are recorded per request; cost is exact or UNKNOWN,
  never guessed.
- Routed: model switching is rule-based only. Effort switching is NOT
  implemented.
- Tested on fixtures only: live provider behaviour and real Codex and Claude
  Code traffic are unverified.
- Evidence: one pair of runs on free models used simulated prices; the routed
  arm cost more than the baseline.
