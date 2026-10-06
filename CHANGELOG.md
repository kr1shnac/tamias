# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0a2] - 2026-10-06

Pre-alpha, prepared for TestPyPI. Merges four branches that had been developed
apart: `feat/cli`, `feat/pricing`, `feat/router` and `ui/restyle`.

### Added

- `tamias prices fetch --out FILE`: pull an OpenRouter model catalogue and render
  it as a TOML sheet that carries its own provenance, with free models marked
  `0` rather than omitted.
- `tamias run -- COMMAND`: run one agent command through a short-lived proxy.
  `--prices` falls back to `./prices.toml` then `~/.tamias/prices.toml`, and
  `--upstream` to `$TAMIAS_UPSTREAM` then `https://openrouter.ai/api`; a missing
  sheet stops with exit code 2 and the command that would fix it.
- `tamias doctor`: offline readiness checks — venv, price sheet, ports, log
  writability — with `--online` for one short timed upstream probe. It warns for
  a missing `OPENROUTER_API_KEY` without ever printing the value.
- `tamias dashboard --db … --prices …`: local dashboard over a request log, with
  optional `--compare-db`, serving `/api/nav`, `/api/summary` and `/api/rows`.
- `tamias agent-config`: print a ready-to-paste agent configuration snippet.
- Project scoping: `--project` on `serve`, `run` and `agent-config`, a `project`
  column in the request log, and project/session grouping in the dashboard.
- Router configuration: `--router-profile generic|legacy`, `--router-config FILE`
  reading a flat TOML file with strict unknown-key, type and range validation,
  `--cheap-model` / `--strong-model` / `--min-gap`, generic classification of
  read/edit/shell tool names, shell error markers, and a big-output cap so long
  shell results are never called easy.
- Billed-cost accounting: `provider_cost_usd`, `generation_id`, and price
  provenance columns in the log; `tamias report` qualifies every dollar figure
  with its sheet, its simulated flag, or `UNKNOWN`.
- `demo/`: task prompt, two-arm runner, model picker and RUNBOOK, plus three
  weeks of synthetic agent history for an offline demo.
- `docs/ARCHITECTURE.md` (the frozen v1.2 spec, previously untracked),
  `docs/ROUTING.md`, `docs/PRICING.md`, and `DEMO.md`.

### Changed

- `tamias report` prints `estimated saving` and `realised saving` separately; the
  second covers only rows the proxy actually rewrote.
- `load_router_config` accepts a `profile` argument; the profile stays rejected
  as a TOML key, so a config file cannot pick the rules used to judge its own
  results.
- The dashboard was restyled.

### Fixed

- `realised saving` now always prints its "not priced" caveat. It was suppressed
  whenever unpriced rows were not a strict minority, so a log that priced 1 of 10
  rewritten rows reported a dollar figure over 1 row with nothing to say the
  other 9 went unmeasured.
- Anthropic cache costs stay `UNKNOWN` rather than being priced without a rate.
- OpenAI Responses usage is metered, and only once it has completed.
- Upstream proxy requests are bounded, and upstream rate-limit headers pass
  through.
- The `SIMULATED PRICES, NOT REAL SAVINGS` label can no longer be dropped from a
  saving line when the price bases differ.

### Known limitations

- Tested on fixtures only. Live provider behaviour and real Codex and Claude
  Code traffic are unverified.
- The two demo arms wrote different row counts, so their cost totals are not
  comparable; the comparable evidence is `decision_action` and `model_used` vs
  `model_requested`.
- Reasoning-effort switching is not in this release.
- No budgets, caps, alerts or spend controls.

[0.1.0a2]: https://pypi.org/project/tamias/0.1.0a2/

## [0.1.0a1] - 2026-10-04

First pre-alpha. Not published to PyPI. Expect breakage.

### Added

- `tamias serve`: local HTTP proxy in front of an OpenAI-compatible
  `/v1/chat/completions` upstream. Request bodies and headers are forwarded
  byte for byte; streaming responses are relayed chunk by chunk; upstream status
  codes pass through.
- `tamias report`: read-only summary of a request log — request count, cost
  total, how many requests shadow mode would have switched, and a labelled
  saving estimate.
- SQLite request log, one row per request: timestamp, session id, model
  requested, model used, token counts, cost, price sheet date, latency, status,
  and the routing decision. No prompt or response text is stored, and the schema
  has no column that could hold any.
- TOML price sheets: USD per 1,000,000 tokens, with a required top-level `date`.
  A rate of `0` means the model really is free; a rate omitted means UNKNOWN.
- Cost arithmetic that records the formula used, and never substitutes zero for
  an unknown token count or rate. Unknown propagates to an unknown cost.
- Rule-based shadow router: records the routing decision it *would* make —
  `STAY` or `SWITCH` to a cheap model for mechanical tool work — and forwards
  the request unchanged.
- `--router-mode` with `shadow` (default), `active` and `off`.
- Anthropic Messages API adapter (`src/tamias/anthropic_adapter.py`) covering
  `x-api-key` passthrough, Anthropic's split `usage` fields, and usage spread
  across `message_start` / `message_delta` stream events. **Experimental.**
- `docs/OPENCODE.md`: pointing OpenCode at the proxy, including the verified
  `opencode.json` provider shape and which Zen models are reachable through
  `/v1/chat/completions`.

### Known limitations

- Cost is UNKNOWN (never guessed as zero) when a field needed to price the
  request is missing; OpenAI-style APIs report no cache writes, so give those
  models `cache_write = 0` in the price sheet and their requests are priced.
- No savings have been measured. The `estimated saving` line in `tamias report`
  is arithmetic over logged token counts, ignores cache-rebuild cost, and is not
  a measurement.
- `--router-mode active` is implemented and unit-tested but has never been run
  against a real provider. Use `shadow`.
- Only `/v1/chat/completions` and `/v1/messages` produce log rows.
  `/v1/responses` and all other paths are relayed by a catch-all route without
  being logged.
- The Anthropic adapter has never been run against the real Anthropic API; it is
  wired into `tamias serve` and unit-tested against a mock only.
- Sessions are grouped by an `x-tamias-session` header. Clients that do not send
  one — OpenCode, as far as the documentation shows — land in a single
  `default` session.
- No budgets, caps, alerts or spend controls.

[0.1.0a1]: https://pypi.org/project/tamias/0.1.0a1/