# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0a8] - 2026-10-20

### Changed

- **The package is published on PyPI.** `pip install tamias` installs
  `0.1.0a8` from <https://pypi.org/project/tamias/>. The first successful
  release was `v0.1.0a7`; the earlier alpha tags failed the publish job because
  the PyPI pending publisher did not yet exist.
- Maintainer contact address is `maintainers@kr1shnac.tk`, and `LICENSE` names
  `kr1shnac` as the copyright holder.

## [0.1.0a7] - 2026-10-20

### Changed

- **The non-loopback bind rule is documented as what it is: two keys.** A
  wider `serve` bind needs *both* authentication (`--auth-token` /
  `$TAMIAS_AUTH_TOKEN` / `--keys-db`) *and* the explicit
  `--allow-non-loopback` acknowledgement. `SECURITY.md` and `README.md`
  previously said "either", which was wrong; `security.py` also printed "this
  service has no authentication" even when a token or keys were configured
  (council run 20261020-1500). The refusal message is now conditional and names
  only what is actually missing.
- `dashboard.py` no longer emits a `SyntaxWarning` at import: the CSV
  formula-injection guard's JS regex had an invalid `\-` escape in a non-raw
  string, which becomes a `SyntaxError` in a future Python.

### Added

- A real OpenRouter price sheet is bundled inside the package. When neither
  `./prices.toml` nor `~/.tamias/prices.toml` exists, `serve` and `run` price
  against it and print a provenance warning on stderr, so a fresh install
  prices a request above `$0.00` instead of looking like a broken proxy (F-20).
  The sheet is a dated snapshot; run `tamias prices fetch` for current rates.
- The catch-all passthrough now writes one **count-only** row per request
  (`route="passthrough"`, token counts and cost NULL, reason `passthrough
  route is not metered`), and `tamias report` prints
  `passthrough requests (count-only, never priced): N`. Off-route traffic is
  still never priced or budget-gated, but it can no longer leave the ledger
  invisible (F-12). The request log gains a `route` column
  (`chat`/`messages`/`responses`/`passthrough`); existing logs get it via the
  usual append-only migration.
- `tests/test_compile_clean.py` compiles every source module with
  `SyntaxWarning` promoted to error, so the invalid-escape class cannot recur.
- Bind-matrix tests pin the two-key rule (authentication and acknowledgement
  are independent) and assert the refusal message never claims "no
  authentication" when authentication is configured.

### Changed

- The saving arithmetic is now **cache-aware and can be negative** (F-1). The
  dashboard's `saved` and both `tamias report` saving lines price the stay model
  with a warm cache and the switch model on the row's real counts, so a switch
  that re-ingests more context than it saves reports a loss instead of being
  clamped to `$0.00`. New primitive `pricing.assume_cache_warm`. The old
  non-negative same-tokens difference is kept as an explicit upper bound
  (`saved_upper_bound` on a row, `saving_upper_total` in the summary). The
  estimate label is now
  `"[estimate; cache-aware, can be negative; not measured]"`, and the dashboard
  Saving card says how many priced rows have no cache-aware saving. A losing
  switch is asserted by
  `tests/test_cli.py::test_report_shows_a_negative_saving_when_a_switch_would_have_lost`
  and
  `tests/test_dashboard.py::test_a_cache_miss_replay_can_show_a_negative_saving`.

## [0.1.0a6] - 2026-10-15

Supersedes the mis-versioned `0.1.0a5` tag, which was cut from `0.1.0a4` content and never uploaded to TestPyPI; that tag was deleted on 2026-10-20 (its only content, `tamias-sql-db.html`, was removed in `a68f7e9`).

### Added

- A capability registry for models (`src/tamias/registry.py`): a strict TOML
  catalogue the router may never choose outside, carrying capabilities only.
- A real reasoning-effort axis, decoupled from model routing: `--effort-mode`
  `off` | `meter` | `apply`, applied only for a model the registry vouches for.
- Company keys (`tamias admin key`, `serve --keys-db`): per-caller identity with
  a model allowlist enforced before forwarding.
- Per-key limits enforced by the gateway itself: a USD budget over a period,
  per-model sub-caps, and per-minute request and token rates. Money uses
  reserve-then-settle; refusals are 402 for money and 429 for rate. See
  `docs/BUDGET.md`.
- Signed project manifests (`tamias admin manifest keygen|sign|verify`,
  `serve --manifest`): a company policy TOML becomes a JSON document with an
  Ed25519 signature, verified at startup and served at `GET /policy`. A missing,
  expired, altered or mis-pinned manifest is refused; the private key never
  reaches the gateway. Needs the optional `cryptography` extra:
  `pip install 'tamias[manifest]'`.

### Changed

- Active request rewriting (`--router-mode active`) is available, opt-in and
  fail-closed: the process refuses to start unless the target is named, listed
  in `--registry`, tool-capable and priced, and a switch the caller's key does
  not allow is suppressed rather than refused. `tamias report` prints
  `requests routed to the cheap model` and names the switches it declined to
  apply. See `docs/ROUTING.md`.

## [0.1.0a4] - 2026-10-10

### Added

- Persistent per-database session salts and safe migration of legacy session ids.
- Shared SSE parser fixtures covering CRLF, byte boundaries, comments, and
  multi-line data events.
- CI checks for tests, Ruff, package builds, metadata, and wheel contents.

### Changed

- Public alpha routing accepts only `shadow` and `off`; active request rewriting
  is disabled.
- Ambiguous legacy `cost_usd` values remain UNKNOWN and are excluded from
  computed-cost and billed-cost reconciliation.
- Dashboard defaults to port 8010 and forwards `--compare-db`.

### Security

- Failure-ledger fields accept only the closed coded vocabulary; raw exception
  text is discarded at the storage boundary.

## [0.1.0a3] - 2026-10-07

### Added

- Optional reasoning-effort routing: `tamias serve --effort-policy easy-low`
  adds high effort to planning and tool-error turns and low effort to easy tool
  turns when active routing is selected. `--effort-style` chooses OpenRouter's
  `reasoning.effort` or OpenAI's `reasoning_effort` request field.
- The request log now records requested effort, forwarded effort, and the
  router's target effort. Existing logs migrate those nullable columns in
  place, and the dashboard displays the request and forwarded values.

### Known limitations

- Effort behavior is verified with fixtures only; whether a provider honours a
  requested effort remains unverified.

[0.1.0a3]: https://github.com/kr1shnac/tamias/releases/tag/v0.1.0a3

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

[0.1.0a2]: https://github.com/kr1shnac/tamias/releases/tag/v0.1.0a2

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

[0.1.0a1]: https://github.com/kr1shnac/tamias/releases/tag/v0.1.0a1
