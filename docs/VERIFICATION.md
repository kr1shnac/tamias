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
  **UNTESTED live** — the arithmetic and the refusals are exercised against mocks; a live exercise (one capped key, one real request, one refusal) has not been run, so the caveat stands.
- **The savings arithmetic is falsifiable.** The dashboard `saved` and both
  `report` saving lines price the stay model with a warm cache and the switch
  model on the row's real counts, so a constructed cache-miss replay yields
  `saved <= 0` (`tests/test_dashboard.py`, `tests/test_cli.py`).

## Verified with authored rates (`scripts/shadow_rate_check.py`)

The savings *arithmetic* is verified without spending money. The pipeline's own
2026-10-04 run used two `:free` models, so its saving was `$0.00 − $0.00` and
carried no information. This check separates the two questions that hid behind
that zero:

- **The pipeline** — does the proxy log token counts, decide SWITCH, and compute
  a saving from a price sheet? This needs a live model that reports usage; the
  free tier is one.
- **The economy** — are the rates real? This needs a provider invoice, which no
  test can manufacture.

`shadow_rate_check.py` verifies the first, for nothing, by pricing the rows a
free run logged with rate sheets the caller authors. The token counts are real;
the rates are declared `simulated`; the arithmetic is the real `compute_cost`.
It prices one row set under four sheets and checks that the CLI's own printed
realised-saving line equals an independent derivation of the same rows:

| Case | Cheap rate | What it exercises |
| --- | --- | --- |
| `flat-cheap` | 0 | saving is the whole strong cost |
| `tenth` | 1/10 of strong | the realistic ratio |
| `near-parity` | ~= strong | saving must collapse toward zero |
| `cache-hostile` | strong cache-write dear | saving must be allowed **negative** |

What a PASS establishes: the saving the report prints is the saving the rates
imply, for a real logged row set, including the sign; and as the cheap model gets
dearer the saving shrinks (a rate-shape-independent ordering check).

What a PASS does **not** establish: that any cheap model is really cheaper per
task, or that the chosen rates are the provider's. A PASS is "the pipeline
computes the saving these rates imply", never "tamias saves money".

```sh
# offline, no key, no socket (uses the tests' mock upstream)
python scripts/shadow_rate_check.py --mock

# against a proxy already listening on the free tier
python scripts/shadow_rate_check.py \
  --proxy http://localhost:8000 \
  --strong-model nvidia/nemotron-3-ultra-550b-a55b:free \
  --cheap-model  nvidia/nemotron-3.5-lightning:free \
  --api-key-env OPENROUTER_API_KEY \
  --db /tmp/shadow.db
```

The `--mock` run is self-testing: it drives the real proxy in active mode so the
router really rewrites a model, and it fails if no row was rewritten (a saving
cannot exist without one). Exit code is `0` on PASS, `1` on FAIL, `2` for a
missing key or log. The API key is read from the environment and never printed.

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

## Verified with the provider ledger (`scripts/credit_ab_check.py`)

A provider-ledger measurement is the one number that does not come from a price
sheet the caller controls. `credit_ab_check.py` runs the *same* task twice through
the *same* agent -- once with routing off (X) and once with routing active (Y) --
and reads `GET /api/v1/key`'s `data.usage` around each arm, so `saving = X - Y` is
the provider's own record of what was spent. Routing is the only variable.

- **The instrument is confirmed, not assumed.** `GET /api/v1/key` returns
  `data.usage` (and `usage_daily`, `free_model_daily_requests`) with an ordinary
  request key; the parser is exercised in `tests/test_credit_ab_check.py` against
  the documented 200 shape, and a 401 is reported as unreadable rather than
  fabricated. `GET /api/v1/credits` (`total_credits` / `total_usage`) is
  account-level and its own docs say a *management* key is required, so it is read
  only behind `--credits`.
- **The machinery runs offline and passes.** `--dry-run` drives the real proxy in
  both modes against the tests' mock upstream: off rewrites 0, active rewrites 1,
  so the arms differ exactly as the comparison requires, and with no ledger
  movement the comparison claims **no** money saving.
- **The arithmetic is covered, including the cases that must refuse.** Unit tests
  pin a positive saving (`$0.040000`), a **negative** saving (`$-0.010000`, not
  clamped to zero), a non-moving ledger ("below the instrument floor", never
  "$0.00 saved"), an unreadable ledger, unequal request counts (which invalidate
  the comparison -- "the work was not identical"), and the free-model quota being
  reported as **a count, not money**.
- **The live path cannot spend by accident.** It is refusal-gated behind
  `--allow-spend`; without it the script prints the procedure and exits; without a
  key `--read-key` exits 2.
- **How much it costs.** The live A/B sends two identical tasks on paid models:
  roughly **$0.05-$0.30 for both arms** on a strong model, or under 10 cents on a
  cheap *paid* pair.
- **The candidate sweep is verified offline, including its refusals.** Its
  arithmetic (median, range overlap, pass/fail gating, winner selection) is pure
  and covered by tests: a clear winner, the cheapest arm failing the task and so
  being excluded, overlapping ranges reported as inconclusive, too-few-runs
  ineligible, and the oracle reading an exit code.

What this does **not** verify: a `:free` model bills `0`, so on free models the
ledger moves by `$0.00` in both arms and the saving is `$0.00` -- quota is
consumed, not credit. No money measurement is possible without a paid model pair,
and none has been run. The ledger's cent-scale resolution also means a saving
smaller than the floor is reported as below the floor, not as a measurement.

## Not verified

- **Savings measurement.** No measured savings exist. Every saving figure the
  report or dashboard prints is arithmetic over invented or listed rates applied
  to logged token counts; none has been reconciled against a provider invoice, and
  no paid, non-zero-price run has been authorised. The *arithmetic* is now
  verified against authored rates (`scripts/shadow_rate_check.py`, above,
  including the negative case) and the *measurement across two arms* is verified
  end to end offline (`scripts/credit_ab_check.py`, above); what remains
  unverified is that the rates are real and that any cheap model is genuinely
  cheaper per task.
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

### `scripts/credit_ab_check.py`
Measures the saving from the provider's own ledger instead of a price sheet. The
same task runs twice through the same agent — once with routing off (`X`) and once
with routing active (`Y`) — and `GET /api/v1/key`'s `data.usage` is read around
each arm, so `saving = X - Y`. Routing is the only variable; the ledger is the
provider's record, not arithmetic the caller controls.

**Offline first** (no key, no socket, no money):

```sh
python scripts/credit_ab_check.py --dry-run
```

`--dry-run` drives the real proxy in both modes against the tests' mock upstream
and checks that the arms differ (off rewrites 0, active rewrites ≥1) and that no
money saving is claimed without a ledger movement.

**Prove the instrument** (needs a key; two GETs, no completion):

```sh
python scripts/credit_ab_check.py --read-key
```

Prints `usage`, `usage_daily` and the free-model request quota. Add `--credits`
to also read `GET /api/v1/credits`, which its docs say needs a *management* key.

**The live A/B** is refusal-gated behind `--allow-spend`; without it the script
prints the procedure and exits. It sends two paid completions and costs roughly
**$0.05–$0.30 for both arms** on a strong model. A `:free` model bills `0`, so on
free models the ledger does not move and no money saving can be read — only the
free-model request quota. A saving below `--min-detectable` is reported as below
the instrument floor, never as a measurement.

**Candidate sweep — "which cheap model saves the most?"** The router is binary
(strong vs *one* cheap target), so comparing several candidates is a comparison
across *runs*, not within one. `--candidates a,b,c --repeats 3` sweeps each cheap
model (plus an `off` baseline), reads the ledger around every run, and alternates
the arm order each round so provider drift hits every arm equally.

```sh
python scripts/credit_ab_check.py \
  --candidates model_a,model_b,model_c \
  --repeats 3 \
  --oracle 'pytest -q' --oracle-cwd /path/to/fixture \
  --strong-model <strong> \
  --allow-spend
```

- **A saved cent that breaks the task is not a saving.** `--oracle '<command>'` is
  required for a sweep: its exit code decides pass/fail after each run, and an arm
  whose runs did not all pass is dropped from the ranking. The sweep refuses to
  run without an oracle and exits 2.
- **A winner needs separated ranges.** The verdict is the cheapest *passing* arm;
  if the two cheapest arms' observed cost ranges overlap, the result is
  **inconclusive** and the script says to increase `--repeats` rather than crowning
  a model on noise. Medians are reported with the full range, never as a single
  number.
- **The sweep arithmetic is verified offline.** `tests/test_credit_ab_check.py`
  pins the clear winner, the case where the *cheapest* arm fails the task and is
  excluded, overlapping ranges reporting inconclusive, too-few-runs being
  ineligible, the median surviving an outlier, and the oracle reading the exit
  code (including a timeout).
- **Cost.** Each run is one request; `(candidates + 1) × repeats` runs. Three
  candidates at three repeats is 12 runs — cents on a cheap paid pair, more on a
  strong one.



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
| Savings measurement | Not attempted | No paid, non-zero-price run has been authorised; no savings claim. The arithmetic is verified against authored rates (`shadow_rate_check.py`, including a negative case) and the two-arm ledger measurement is verified offline (`credit_ab_check.py`); no live ledger measurement has been taken |
| Live provider behavior | Unverified | CI uses offline/injected transports only |
| Package build | CI-gated | Build, `twine check`, and a fresh-venv wheel smoke run in GitHub Actions |
| Publish | Released | `0.1.0a8` is on PyPI, published from tag `v0.1.0a8` via Trusted Publishing (OIDC) |

This page describes implemented repository behaviour and the runs recorded above.
It does not claim provider compatibility, savings, latency, or live-agent success
beyond the 2026-10-04 observations.
