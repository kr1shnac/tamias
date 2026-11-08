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
  `/v1/messages` stores the response id as `generation_id` on both the buffered
  and streaming paths.
- **A provider switch does not forward the client's credential.** The
  catalogue strips `authorization`, `proxy-authorization`, `x-api-key`,
  `api-key` and `x-goog-api-key` before applying the provider's own key; the
  chat route and `/v1/messages` are both tested with a client `x-api-key`
  (`tests/test_providers.py`, `tests/test_multi_provider.py`,
  `tests/test_adapter_failover.py`, `tests/test_anthropic.py`).
- **`tamias opencode uninstall` removes only its own wiring.** A setup followed
  by uninstall restores the other providers, plugins and commands, removes the
  `tamias` / `tamias-report` command files and the staged plugin, and keeps a
  mode-`0600` backup of each rewritten config (`tests/test_opencode_uninstall.py`).
- **Adapter failover is the chat route's failover.** `/v1/messages` retries a
  refused model (429/404) on the next gated model before the first streamed
  byte, remembers the refusal for later requests, and refuses a model no
  configured provider serves; `/v1/responses` records the same refusals without
  rewriting the client's model and refuses an unserved model too
  (`tests/test_adapter_failover.py`).
- **The catch-all passthrough.** It relays other paths and writes one count-only
  row per request (`route="passthrough"`, tokens and cost NULL).
- **Budgets and keys.** Reserve-then-settle, sub-caps, both rate windows, the
  reservation TTL, and 402/429 refusals on every metered route, against mocks.
  **UNTESTED live** — the arithmetic and the refusals are exercised against mocks; a live exercise (one capped key, one real request, one refusal) has not been run, so the caveat stands.
- **The savings arithmetic is falsifiable.** The `report` saving lines price the stay model with a warm cache and the switch
  model on the row's real counts, so a constructed cache-miss replay yields
  `saved <= 0` (`tests/test_cli.py`).
- **Cheap-pool selection is deterministic and fail-closed.** A session keeps one
  pool member across request indexes, different sessions spread across members,
  the requested model is skipped, and every member is validated at active-mode
  startup (`tests/test_router.py`, `tests/test_active_routing.py`). A single
  model remains a one-member pool.
- **The background unit is generated, not guessed.** `service install` writes
  absolute paths, systemd-safe quoting, the routing flags, refuses an overwrite
  without `--force`, validates every active target against the registry and
  price sheet, and `service uninstall` refuses a unit it did not write
  (`tests/test_service.py`). No systemctl call is made unless `--start` is
  passed.
- **`tamias reconcile` compares real numbers and can fail.**
  `tests/test_reconcile.py` pins the arithmetic (match within tolerance,
  mismatch, missing billed vs missing computed), the `--since`/`--until`
  window, the JSON summary, the artifact fields, the documented generation
  endpoint, non-OpenRouter ids being skipped without a request, and that a
  failed lookup stays `INCOMPLETE`; the lookup key is asserted absent from
  stdout and stderr. Offline rows use the stored `provider_cost_usd`;
  `--online` uses `data.total_cost`. A live OpenRouter lookup has been run; a
  real invoice comparison is **not** exercised.

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

## Verified against the local live log (reconcile, 2026-11-08)

`tamias reconcile --db ~/.tamias/requests.db` (offline: no network, no key) read
1459 rows. It excluded 1440 passthrough rows as count-only and left 19 metered
rows. Ten had both numbers: eight matched, two did not.

| Row | Model used | Tokens in/out | Computed | Billed | Difference |
| --- | --- | --- | --- | --- | --- |
| 1313 | `deepseek/deepseek-v4.1-flash` | 32 / 8 | `$0.00000544` | `$0.000008096` | `+$0.000002656` |
| 1317 | `deepseek/deepseek-v4.1-flash` | 89 / 16 | `$0.000012005` | `$0.00001918` | `+$0.000007175` |

The command exited `3` (`FAIL`). The remaining nine metered rows were
incomplete: seven lacked a billed cost, eight lacked a computed cost, and some
were both. The free rows matched at `$0.00`, which proves no saving. This is the
first live evidence that the list-price computation is not always the
provider's bill; the rates were not changed on the strength of two rows.

An online run on the same log called only the documented generation endpoint for
the 12 distinct `gen-` ids: none failed, and one non-OpenRouter id
(`chatcmpl-…`) was skipped rather than sent. For rows 1313 and 1317 the lookup
returned exactly the stored bill (`$0.000008096` and `$0.00001918`), so the
mismatch is in the computed list price, not in the stored measurement. The
verdict was unchanged and the command still exited `3`. `tamias report` now
uses that stored bill as the actual side of the realised saving and prints how
many actual costs came from the provider bill.

The local `1.0.2` wheel and sdist built on 2026-11-08 contain
`tamias/reconcile.py`; `twine check` passed for both.

## Verified by hand against OpenCode Zen (routing only)
Observed **2026-10-30** on Linux (Python 3.14, opencode 2.0.32), against
`https://opencode.ai/zen` with **no API key**.

- **The model list is live.** `tamias prices zen` fetched 82 ids and priced the
  9 free ones at exactly `$0`; paid models were left out (UNKNOWN), not guessed.
- **The registry is published, not assumed.** `tamias registry zen` joined that
  list to models.dev's `tool_call` field and wrote 8 tool-capable free models;
  `jev-1.13-free` was excluded because models.dev publishes no capability.
- **Active routing fired against the real upstream.** A chat request whose last
  message was a `read` tool result produced
  `decision=SWITCH target=deepseek-v4.1-flash-free reason=easy tool: read`, and
  the rewritten request reached Zen.
- **The pool path is live (routing only).** The same request with a
  two-member pool (`deepseek-v4.1-flash-free,big-pickle`) switched to
  `deepseek-v4.1-flash-free`; the log and `tamias report` both named that switch
  target. Zen still answered `429`, so this proves selection and logging, not a
  completed turn on either member.
- **The generated systemd unit is valid.** A unit written to a temporary
  `XDG_CONFIG_HOME` passed `systemd-analyze verify` (exit 0), and its exact
  `ExecStart` started the proxy and proxied `/v1/models` from Zen (HTTP 200).
  It was not enabled with `systemctl --user`, so login startup is untested.
- **No successful completion.** Zen returned `429` (anonymous request, no key),
  so there are no token counts and no cost for that row. This proves the routing
  decision and the rewrite, **not** that the free model completes a turn or that
  money is saved.
- **The default hysteresis matters.** With the default `--min-gap 3` the same
  request stayed on the requested model (`hysteresis: 0 of 3`); `--min-gap 0`
  produced the switch. The OpenCode guide recommends `0` for that reason.
- **OpenCode config shape.** `opencode debug config` (v2.0.32) normalises the
  documented `provider`/`npm`/`options` shape into `providers`. A provider block
  **without** a `models` map lists nothing in `opencode models`; the installer
  now writes the map from the registry or the free half of the price sheet, and
  `opencode models` listed all 8.

## Verified by hand across two providers

Observed **2026-11-04** on Linux (Python 3.14, opencode 2.2.5), against direct
NVIDIA (`https://integrate.api.nvidia.com/v1`) and OpenRouter at the same time,
served from one `tamias serve` process.

- **Discovery and mapping.** `tamias serve --providers ~/.tamias/providers.toml`
  read both keys, discovered 427 model ids from 2 providers, and reported
  `424 considered, 11 candidates` for the pinned allowlist. Every candidate
  named its provider in `GET /limits`.
- **A planning turn stayed.** A chat request naming the paid
  `deepseek/deepseek-v4.1-flash` was logged `decision=STAY reason=user turn:
  planning` and reached OpenRouter unchanged (HTTP 200).
- **An easy tool turn switched.** The same model with a trailing `read` tool
  result was logged `decision=SWITCH target=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
  reason=easy tool: read`, and the response came from NVIDIA (HTTP 200, 64 input
  / 32 output tokens). The request row recorded `model_used` as the free NVIDIA
  id, not the paid one.
- **Price overlay drove the rank.** The eight pinned NVIDIA ids were priced at
  `0.0` in `prices.local.toml`; the same ids on OpenRouter were not free. The
  pool ranked the direct NVIDIA entries first, and `tamias report` printed
  `tokens on $0-priced models: 64 input / 32 output`.
- **A 404 was learned and absorbed.** A deliberately bogus model id pinned into
  the NVIDIA catalogue (`nvidia/nemotron-3-ghost-99b`, priced at `0`) was chosen
  first, answered `404 Not Found`, and was recorded
  `limit provider=nvidia model=… status=404 cooldown=43199s strikes=1` with the
  reason `not offered for this account (404)`. The same request was retried on
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` and returned HTTP 200;
  `decision_reason` reads `upstream 404 on …: retried on …`. The next `/limits`
  reported `gates: {"health": 1}` and that model in `cooling_down` for 12 hours.
- **What this does not prove.** One short session on two providers, not a
  long-running agent; the NVIDIA tier is free for this account, so the
  "saving" is the difference between two list-price computations, not an
  invoice. Long agent sessions, Claude Code and Codex traffic, and every other
  provider remain unverified.

## Verified against direct NVIDIA with capabilities and prices

Observed **2026-11-06** on Linux (Python 3.14, opencode 2.2.5) on the same
`tamias serve --providers` process, after registering the direct-NIM ids in
`~/.tamias/models.toml` with capabilities and `0` rates in
`~/.tamias/prices.local.toml`.

- **Capability facts were probed, not assumed.** Every id in the catalogue's
  `discover = "listed"` list was sent one small chat completion. Seven of eight
  answered; tool support was decided by a forced `tool_choice` returning
  `finish_reason: tool_calls`, not by a model card. Two recorded findings:
  `nvidia/nemotron-3.5-bolt-e4b` emits `<|tool_call>{}` inside `content` and is
  registered `tools = false`; `z-ai/glm-5.3-flash` timed out twice at 5+ minutes
  and is deliberately absent from the registry.
- **A request ran on a direct model.** `POST /v1/chat/completions` naming
  `nvidia/nemotron-3-ultra-550b-a55b` returned HTTP 200 and was logged
  `decision=STAY`, `computed_list_cost_usd=0.0`, `cost_source=computed`, with
  `price_sheet = …/prices.toml + …/prices.local.toml` — so the operator overlay
  was in force for that row, not just the generated sheet.
- **The gates admit what was registered.** With the seven ids plus the three
  OpenRouter free models in an allowlist, a tools-bearing need admits 8
  candidates and refuses one
  (`nvidia/nemotron-3.5-bolt-e4b [capability: no tool calls]`); a plain chat need
  admits all 9. The allowlist is honoured by design, so the running service's
  `/limits` pool stays the three models its `--cheap-model` names until the
  operator widens it.

## Verified on a live streaming turn (2026-11-08)

One streaming chat request through the running service, recorded as row 1459 in
`~/.tamias/requests.db`:

- **The route and mode.** `route=chat`, `router_mode=active`, decision `STAY`
  (`user turn: planning`), model `nvidia/nemotron-3.5-lightning:free`.
- **Usage arrived without an opt-in.** The provider reported it on the stream
  although `include_usage` was not requested: `input_tokens=23`,
  `output_tokens=16`, `cached_input_tokens=0`.
- **Provenance was stored.** `generation_id=gen-1794140924-u4C0NyYhRy50D9nXcyz2`,
  `provider_cost_usd=0.0`, `computed_list_cost_usd=0.0`, `cost_source=computed`,
  priced from `prices.toml + prices.local.toml`.
- **What the zeros mean.** This model is free on the account's declared tier.
  The row proves streamed usage capture and provenance; it is not an invoice and
  not a savings measurement.

## Verified inside a real OpenCode TUI

The plugin's `scripts/verify.ts` harness reproduces the host contract, but it
cannot prove host module resolution. So the pop-up was also driven inside **the
real OpenCode 2.2.5 TUI**, in a pty, against an isolated `HOME`/`XDG_CONFIG_HOME`
so no operator config was touched:

- **`pip install tamias` alone used to fail.** With a clean venv and no source
  checkout reachable, `setup` registered `site-packages/tamias/plugin`, the host
  logged `plugin operation failed … error="Cannot find package 'solid-js' imported
  from …/tamias/plugin/src/hooks/useLimits.ts"`, and the status bar showed
  `1 plugin failed`. OpenCode resolves a plugin's imports beside the plugin
  directory, and `~/.opencode/node_modules` carries no `solid-js`/`@opentui`
  (they are compiled into its binary), so the wheel's TypeScript could not load.
- **After the fix it loads.** `setup` stages the bundle at `~/.tamias/plugin`,
  runs `bun install` there and registers that path. Same clean-venv run, real
  TUI: **zero** `plugin operation failed` entries, the palette shows
  `/tamias Open the tamias routing pop-up`, and the command opens the pop-up —
  header `tamias · all recent`, `○ log only`, `no request log`, and the
  `esc close · log 2s · live 5s` footer, i.e. the real component rendered, not a
  stub.
- **Reproduce it.** Isolate a HOME, run setup with `--config`/`--db`/`--prices`
  pointed into that HOME, then start `opencode --standalone` with
  `HOME`/`XDG_CONFIG_HOME`/`XDG_DATA_HOME` set to it, type `/tamias`, press
  Enter. `opencode.log` under `$XDG_DATA_HOME/opencode/log/` is where a plugin
  load failure appears; `1 plugin failed` in the status bar is the symptom.
- **What this does not prove.** Bun is on `PATH` for the install step. Windows
  and macOS were not exercised, and 2.2.5 is the only host version tested —
  plugin resolution is undocumented host behaviour.



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
  report or the OpenCode pop-up prints is arithmetic over invented or listed rates applied
  to logged token counts; none has been reconciled against a provider invoice, and
  no paid, non-zero-price run has been authorised. The *arithmetic* is now
  verified against authored rates (`scripts/shadow_rate_check.py`, above,
  including the negative case) and the *measurement across two arms* is verified
  end to end offline (`scripts/credit_ab_check.py`, above); what remains
  unverified is that the rates are real and that any cheap model is genuinely
  cheaper per task. `tamias reconcile` now makes the comparison possible
  row by row, offline from stored provider costs or online from the provider's
  generation record; a live generation lookup has been run, but no provider
  invoice has been compared.
- **A provider invoice.** `tamias reconcile --online` has read real generation
  records (12 ids, 0 failed), but no invoice from any provider has been compared
  with the log.
- **Paid-model behaviour.** Every live rate used was a real `0`. Real per-token
  arithmetic against a **non-zero** rate has never been checked.
- **Pool members beyond the routing-only target.** Only
  `deepseek-v4.1-flash-free` was observed receiving a rewritten request from the
  Zen pool (routing only; the upstream answered 429 without a key). The
  two-provider run on 2026-11-04 did complete turns on NVIDIA, and a `404` was
  retried on another member inside the same request — but no **429** on the
  chosen member has been observed being retried.
- **The free tier is declared, not invoiced.** The eight direct NVIDIA ids are
  priced at `0.0` in the operator's overlay because that is the account's
  development tier. No invoice confirms it, and the same weights cost money on
  OpenRouter.
- **The systemd unit runs, but only this session.** The generated unit is
  `enabled`, `active (running)` and serving `/limits` on `127.0.0.1:8000`
  (observed 2026-11-08), and it runs the code at `HEAD`. What has *not* been
  observed is a cold `reboot`-style start — the unit starting from nothing at
  login, with no shell environment and no `DISPLAY` — and the credential
  bridge (`api_key_command`) being resolved inside that cold start rather than
  inherited from a session that had already run it once.
- **Anthropic, Codex and Claude Code live traffic.** The `/v1/messages` and
  `/v1/responses` routes are fixture-tested only; no live Anthropic, Codex or
  Claude Code traffic has been observed. Only `/v1/chat/completions` was
  exercised live. The adapter failover added on 2026-11-08 (retry, limit
  learning, unserved-model refusal) is likewise fixture-tested only; no live
  `429` has reached either adapter.
- **Cache effects.** Whether switching models destroys a warm prompt cache, and
  what that costs per task, is not measured. Effort changes may also invalidate a
  provider's cache; this has not been measured either.
- **Attribution.** A metadata-only log cannot say whether the passing edit came
  from the strong model or the cheap one.
- **Rate limiting.** The live runs produced no `429`; the free-tier limit, the
  backoff path and the `429` retry were not exercised. The `404` path was
  exercised on 2026-11-04 (learned, cooled for 12 h, retried in-request).
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

Release **`1.0.2`** — the version in this tree. The tag-triggered `publish.yml`
job publishes a `vX.Y.Z` tag through PyPI Trusted Publishing (OIDC); `0.1.0a8`
(2026-10-20) was the artifact on PyPI before it, built from tag `v0.1.0a8`. The
first successful upload was `v0.1.0a7`; earlier alpha tags failed with
`invalid-publisher` before the PyPI pending publisher existed. TestPyPI's newest
is `0.1.0a3`.

| Area | Status | Evidence or limitation |
| --- | --- | --- |
| Local proxy metering | Implemented | Chat, Responses and Anthropic routes share one log; offline client-matrix test |
| Shadow routing | Implemented | Decisions recorded without rewriting upstream requests |
| Active routing | Opt-in | `--router-mode active` rewrites the model to `--cheap-model`; refused at startup unless the target is in `--registry`, tool-capable, and priced; a key-forbidden switch is suppressed, not refused |
| Multi-provider routing | Implemented, live | Two providers served from one process on 2026-11-04; per-model provider, credential, and cheapest-first ranking. Provider coverage beyond NVIDIA, OpenRouter and Zen is config-tested, not live |
| Failover | Implemented | `404` learned for 12 h and retried in-request live; `429`/`5xx` cooldowns are fixture-tested only. `/v1/messages` now uses the same retry with an unserved-model guard; `/v1/responses` learns refusals without rewriting. Adapter paths are fixture-tested, not live |
| Cost provenance | Implemented | Provider billed cost and list-price computation remain separate; `tamias reconcile` compares them row by row and exits non-zero on a mismatch; `tamias report` uses the billed cost as the actual side of the realised saving where recorded |
| Legacy cost rows | Unknown | Ambiguous `cost_usd` is excluded from computed/reconciliation totals |
| Session privacy | Implemented | Per-database salt and legacy session migration |
| SSE parsing | Implemented | Shared assembler and adversarial parser fixtures; live providers unverified |
| Authentication | Opt-in | `--auth-token` / `$TAMIAS_AUTH_TOKEN`; constant-time bearer check; required for a non-loopback bind |
| Listener hardening | Implemented | Body cap (413) and concurrency limit; optional TLS via `--tls-cert`/`--tls-key` |
| Signed policy manifest | Opt-in | `serve --manifest` verifies an Ed25519-signed policy at startup and serves it at `GET /policy`; expired/altered/mis-pinned manifests are refused (404/401/403/503). Needs `tamias[manifest]`; agent-side application is not enforced |
| Configuration | Implemented | `~/.tamias/config.toml` (or `$TAMIAS_CONFIG`); provider presets; `tamias config` |
| OpenCode integration | Implemented, live | `opencode setup` writes the provider block, `opencode models` lists it on 2.2.5; the pop-up loads in the real 2.2.5 TUI from a clean venv after `setup` installs its peers with Bun (see the TUI section above); without Bun it registers nothing and says so |
| Log maintenance | Implemented | `tamias prune`; `tamias doctor` size warning |
| Supply chain | CI-gated | `pip-audit --strict`, CycloneDX SBOM artifact, `pip-licenses` report with a copyleft denylist |
| Benchmark | Implemented | `scripts/benchmark_overhead.py`, offline, no socket |
| Savings measurement | Not attempted | No paid, non-zero-price run has been authorised; no savings claim. `tamias reconcile` compares stored computed cost with stored or looked-up provider cost per row; the live generation lookup succeeded (12 ids, 0 failed), but no invoice has been reconciled. The arithmetic is verified against authored rates (`shadow_rate_check.py`, including a negative case) and the two-arm ledger measurement is verified offline (`credit_ab_check.py`) |
| Live provider behavior | Partly verified | CI uses offline/injected transports only. Live runs: OpenRouter + NVIDIA on 2026-10-04 and 2026-11-04, direct NVIDIA capabilities and a 200 request on 2026-11-06, and OpenCode Zen on 2026-10-30 (routing only; it answered 429 without a key) |
| Package build | CI-gated | Build, `twine check`, and a fresh-venv wheel smoke run in GitHub Actions; the wheel carries `tamias/plugin` and CI fails if it does not |
| Publish | Tag-triggered | `v1.0.2` publishes through Trusted Publishing (OIDC); `0.1.0a8` was the last artifact on PyPI |

This page describes implemented repository behaviour and the runs recorded above.
It does not claim provider compatibility, savings, latency, or live-agent success
beyond the observations dated above.
