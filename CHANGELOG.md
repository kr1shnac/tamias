# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- Cost is UNKNOWN for every request against an OpenAI-style upstream: the
  chat-completions `usage` object carries no cache-write count, and a
  cache-write rate is needed to price a request. Guessing zero is forbidden.
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