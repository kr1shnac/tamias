# Verification

This page records what has actually been checked. It uses one plain vocabulary:
*tests* (offline, against fixtures and mocks), *live* (against a real provider),
and *not verified*.

## Verified by automated tests (offline, fixtures and mocks)

The test suite (`tests/`) never touches the network; every upstream is a fixture
or an in-process mock. What it pins down:

- **Routing.** `router.decide` inspects only the last message
  (`tests/test_router.py`); decisions are asserted on action, target and reason,
  never on prompt text; the body is never mutated; error markers are
  case-sensitive and `Error:` (capital E) is deliberately not a default marker.
- **Byte-identical forwarding.** In shadow mode the upstream receives exactly the
  bytes the client sent, and shadow never changes a request byte.
- **No prompt or response text reaches the database.** No column holds body
  content, and no page of the database file holds it either.
- **NULL is never 0.** An unreported token count or cost is stored as SQL NULL,
  and an unknown cost is pinned as not zero.
- **Price-sheet parsing, rendering and arithmetic.** Free models, paid models
  with and without cache fields, duplicates and junk inputs; `render_sheet`
  output round-trips through `load_price_sheet`; `fetch_models` runs through a
  fake opener, so no test fetches a real catalogue. `compute_cost` is checked
  against hand-computed expected values, plus a differential test that
  re-derives a thousand fixed-seed cases from the pricing contract
  (`tests/test_hardening_pricing.py`).
- **The three adapter routes.** `/v1/chat/completions`, `/v1/messages` and
  `/v1/responses` are exercised against fixtures (buffered, completed-stream and
  truncated-stream cases), including Anthropic and Responses usage merging.
- **The catch-all passthrough.** It relays other paths and writes one count-only
  row per request (`route="passthrough"`, tokens and cost NULL).
- **Budgets and keys.** Reserve-then-settle, sub-caps, both rate windows, the
  reservation TTL, and 402/429 refusals on every metered route, against mocks.
- **The savings arithmetic is falsifiable.** The dashboard `saved` and both
  `report` saving lines price the stay model with a warm cache and the switch
  model on the row's real counts, so a constructed cache-miss replay yields
  `saved <= 0` (`tests/test_dashboard.py`, `tests/test_cli.py`).

## Verified by hand against a live provider

Observed **2026-10-04** on Linux (Python 3.14.4, opencode 1.18.34), against
OpenRouter's live API (`https://openrouter.ai/api`), served by NVIDIA.

- **Models.** `nvidia/nemotron-3-ultra-550b-a55b:free` (the requested model) and
  `nvidia/nemotron-3.5-lightning:free` (the router's switch target), both free.
- **Scope.** `opencode run` was driven through the proxy by an OpenAI-compatible
  `opencode.json` provider, on a two-file Python project, repairing a
  one-character typo so the project's own test suite passes (2 passed).
- **What was observed.** Shadow and active routing ran to completion with all
  rows HTTP 200 and the task fixed in both. `tamias report` priced real logs
  (3-probe live check; 8-row and 11-row agent sessions). No prompt or response
  text and no credential appeared in any database.
- **Shadow mode** recorded the decisions it would have made (3 of 8 rows SWITCH
  in one session) while forwarding every request on the strong model
  (`model_used == model_requested` on all rows).
- **Active mode** produced 2 rows where `model_requested != model_used`, both
  SWITCH to `nvidia/nemotron-3.5-lightning:free`, with 0 4xx and 0 5xx.
- **One row reported real cache reads:** 6528 cached input tokens against 1906
  uncached.
- **Streaming.** With `--inject-usage` on, OpenCode ran both sessions to
  completion with no parse error and no retry; the usage chunk is appended last,
  so coverage was 8 of 11 rows in shadow (a client that stops reading before the
  last chunk leaves nothing to log) and 8 of 8 in active.
- **Provider usage without an opt-in.** OpenRouter sent a usage chunk on streams
  that did not request one, and omitted it on one direct request — the behaviour
  is not deterministic. The live check therefore judges probe 2 on delivery, not
  on token counts.

## Not verified

- **Savings measurement.** No measured savings exist. Every saving figure the
  report or dashboard prints is arithmetic over invented or listed rates applied
  to logged token counts; none has been reconciled against a provider invoice, and
  no paid, non-zero-price run has been authorised.
- **Paid-model behaviour.** Every live rate used was a real `0`. Real per-token
  arithmetic against a **non-zero** rate has never been checked.
- **Anthropic, Codex and Claude Code live traffic.** The `/v1/messages` and
  `/v1/responses` routes are fixture-tested only; no live Anthropic, Codex or
  Claude Code traffic has been observed. Only `/v1/chat/completions` was
  exercised live.
- **Cache effects.** Whether switching models destroys a warm prompt cache, and
  what that costs per task, is not measured. Effort changes may also invalidate a
  provider's cache; this has not been measured either.
- **Attribution.** A metadata-only log cannot say whether the passing edit came
  from the strong model or the cheap one.
- **Rate limiting.** 42 requests across the live runs produced no 429; the
  free-tier limit, the backoff path and the retry were not exercised.
- **Multi-session continuity.** Resuming an existing session, and any
  `opencode run --continue` behaviour, was not exercised.

## Running the live checks

### `scripts/live_check.py`

Sends three requests through a running tamias proxy, then reads the sqlite log
back and prints what tamias recorded. The same one-line prompt
(`"Reply with the word ok"`) is sent three ways:

| # | request | what the upstream is asked for |
| --- | --- | --- |
| 1 | buffered | usage in the JSON body |
| 2 | streaming, **no** `stream_options` | nothing |
| 3 | streaming, `stream_options.include_usage = true` | a trailing usage chunk |

Probe 2 is judged only on what the proxy owes you — HTTP 200, a stream ending in
`[DONE]`, and one row logged — and the script *reports* whether the upstream
volunteered usage. Requests 1 and 3 must carry token counts, because both asked.

**Offline first** (no key, no socket, no money):

```sh
python scripts/live_check.py --mock
```

`--mock` wires the real proxy app to the tests' mock upstream in-process; the
database is a temporary file deleted afterwards.

**Against a real upstream:** point `--api-key-env` at a different variable and
run against a proxy already listening:

```sh
python scripts/live_check.py \
  --proxy http://localhost:8000 \
  --model nvidia/nemotron-3-ultra-550b-a55b:free \
  --api-key-env OPENROUTER_API_KEY \
  --db /tmp/or.db
```

- The API key is read from the environment and never printed: every output line
  passes through a redactor, replacing the key with `<redacted>`. Only the
  variable's *name* is printed.
- Exit code is `0` on `PASS` and `1` on `FAIL`, so it drops into CI or a
  Makefile. A missing key or missing log exits `2`.
- The script sends real requests and they cost money: three one-token requests.
  It never sends more than three.
- The prompt is deliberately identifiable, so a search for it in the log is a
  meaningful check.

### `scripts/reconcile_live.py`
Compares each logged OpenRouter row against the provider's documented generation
lookup (`GET https://openrouter.ai/api/v1/generation?id=<generation-id>`,
`data.total_cost`). It performs only that GET for existing log rows — it never
creates a completion. Where a stored generation id is missing or malformed, the
row is reported rather than inferred. Requires `httpx` and an `OPENROUTER_API_KEY`
(override with `--api-key-env`); a USD tolerance is set with `--tolerance`.

## Release status

Public alpha — **`0.1.0a8` on PyPI** (<https://pypi.org/project/tamias/>),
published from the annotated tag `v0.1.0a8` (2026-10-20) by the tag-triggered
`publish.yml` job via PyPI Trusted Publishing (OIDC). The first successful upload
was `v0.1.0a7`; earlier alpha tags failed with `invalid-publisher` before the PyPI
pending publisher existed. TestPyPI's newest is `0.1.0a3`.

| Area | Status | Evidence or limitation |
| --- | --- | --- |
| Local proxy metering | Implemented | Chat, Responses and Anthropic routes share one log; offline client-matrix test |
| Shadow routing | Implemented | Decisions recorded without rewriting upstream requests |
| Active routing | Opt-in | `--router-mode active` rewrites the model to `--cheap-model`; refused at startup unless the target is in `--registry`, tool-capable, and priced; a key-forbidden switch is suppressed, not refused |
| Cost provenance | Implemented | Provider billed cost and list-price computation remain separate |
| Legacy cost rows | Unknown | Ambiguous `cost_usd` is excluded from computed/reconciliation totals |
| Session privacy | Implemented | Per-database salt and legacy session migration |
| SSE parsing | Implemented | Shared assembler and adversarial parser fixtures; live providers unverified |
| Authentication | Opt-in | `--auth-token` / `$TAMIAS_AUTH_TOKEN`; constant-time bearer check; required for a non-loopback bind |
| Listener hardening | Implemented | Body cap (413) and concurrency limit; optional TLS via `--tls-cert`/`--tls-key` |
| Signed policy manifest | Opt-in | `serve --manifest` verifies an Ed25519-signed policy at startup and serves it at `GET /policy`; expired/altered/mis-pinned manifests are refused (404/401/403/503). Needs `tamias[manifest]`; agent-side application is not enforced |
| Configuration | Implemented | `~/.tamias/config.toml` (or `$TAMIAS_CONFIG`); provider presets; `tamias config` |
| Log maintenance | Implemented | `tamias prune`; `tamias doctor` size warning |
| Supply chain | CI-gated | `pip-audit --strict`, CycloneDX SBOM artifact, `pip-licenses` report with a copyleft denylist |
| Benchmark | Implemented | `scripts/benchmark_overhead.py`, offline, no socket |
| Savings measurement | Not attempted | No paid, non-zero-price run has been authorised; no savings claim |
| Live provider behavior | Unverified | CI uses offline/injected transports only |
| Package build | CI-gated | Build, `twine check`, and a fresh-venv wheel smoke run in GitHub Actions |
| Publish | Released | `0.1.0a8` is on PyPI, published from tag `v0.1.0a8` via Trusted Publishing (OIDC) |

This page describes implemented repository behaviour and the runs recorded above.
It does not claim provider compatibility, savings, latency, or live-agent success
beyond the 2026-10-04 observations.
