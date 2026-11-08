# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

A cross-platform and provider-agnostic proof pass: the Windows quoting holes are
closed, the same gate runs on macOS and Windows, any provider's billing export
can be reconciled, the documented live A/B actually exists, and the pop-up stops
describing an arithmetic estimate as a realised saving.

### Added

- **`tamias reconcile --invoice` reconciles a provider's billing export.** A CSV
  with one identity column and one USD amount column is joined to the log by row
  id (`--invoice-join request_id`) or by the provider's generation id
  (`generation_id`), so a provider with no per-request billing API — OpenAI,
  Anthropic, Google, NVIDIA — can prove the billed side. `--invoice-scope day`
  sums the computed cost of each UTC calendar day and compares it with the
  invoice's daily line, for providers that publish only daily totals. The
  invoice is the **only** billed side: a selected row it does not cover stays
  `UNKNOWN` rather than being backfilled from the response-reported cost.
  Amounts may carry `$` and thousands separators; a blank cell, a negative
  amount, a missing column, or an invoice with no line items is a runtime error
  (exit 1), never a skipped line. Duplicate identity keys are summed (one request
  can be split across SKUs). The JSON summary gains an `invoice` object and the
  artifact an `invoice_cost` field. `--invoice` and `--online` are alternative
  billed sources and cannot be combined.
- **CI runs the Python suite on macOS and Windows.** The `platform` job runs
  `pytest -q` and `ruff check` on `macos-latest` and `windows-latest` on Python
  3.12, alongside the existing Ubuntu matrix (3.11–3.14), so portability is
  checked rather than asserted.

### Changed

- **The documented live A/B in `scripts/credit_ab_check.py` exists.** The
  no-`--candidates` path behind `--allow-spend` now runs the off arm, reads the
  ledger, runs the active arm with `--cheap-model`, reads the ledger again, and
  reports `MEASURED SAVING: X - Y` from the provider's own numbers. It refuses
  before spending when `--upstream`, `--prices`, `--strong-model` or
  `--cheap-model` is missing, stops before the first request when the ledger
  cannot be read, and refuses to call a delta a saving when the off arm rewrote
  something or the active arm routed nothing (that difference is noise, not
  routing). A negative result exits 0 with the loss printed: a measured loss is
  a finding, not a failure to hide. `--max-spend USD` is a hard ceiling enforced
  against the provider's own ledger, not an estimate: the run stops before
  sending the next request once that key's usage has moved by that much.
- **Three live findings are now flags in `credit_ab_check.py`.** An unbounded
  request reserves the model's whole context against the balance, so
  OpenRouter refused a run with `402 ... requested up to 131072 tokens, but can
  only afford 36995`; `--max-tokens N` bounds it. `GET /api/v1/key`'s
  `usage` lags a live run by minutes, so a read taken immediately reports
  `$0.00` for money that was spent, and a single late read books *both* arms'
  spend onto the last arm — which measured a real saving as a **loss** until
  `--settle SECONDS` made the wait apply after each arm. And when the lifetime
  field has not moved but `usage_daily` has, `compare_arms` reads the daily
  field and names it, instead of reporting "no money moved". A falling
  `usage_daily` is treated as a day rollover, never as a negative saving.
- **One live measurement exists: $0.000038.** A paid two-arm A/B on 2026-11-09
  against OpenRouter (strong `deepseek/deepseek-v4.1-flash`, cheap
  `inclusionai/ling-3.0-flash`, `--max-spend 0.50`) measured
  `X $0.000061 - Y $0.000024` from the provider's own ledger for a 2-request
  task, with the arms proven (off rewrote 0, active rewrote 1) and the ledger
  moving `$0.000085` in total — the ceiling held. That is the provider's record
  rather than arithmetic, and it is evidence the pipeline measures at the
  ledger's floor; it is not evidence that any rate is right.
- **The pop-up's savings bar says `est. X% below list` instead of
  `realised`.** Its baseline is the requested model re-priced from the price
  sheet — a counterfactual no provider ever billed. The percentage carries one
  decimal so 99.75% no longer renders as `100%`.
- **The pop-up KPI line is two lines, and they are ordered by what matters.**
  `requests · spent · switched · failed` first, then `free (n/m rated) · cache
  … warm · unpriced`, so a narrow terminal can no longer clip `failed` and
  `unpriced` off the end. The free share now shows its denominator, and rows the
  sheet cannot rate are excluded from it rather than counted as not-free.
- **The pop-up counts only applied switches.** A row whose used model differs
  because of a *failover* is not a routing switch, so `n_switched` now requires
  `decision_action = SWITCH` with a changed model and a 200 status.
- **The pop-up's latest-route line no longer shows an arrow for a stay.** A
  STAY row whose model changed through failover renders `model (served by
  other)`; only an applied SWITCH gets `a → b`. A failed latest row says
  `failed <status>`.
- **A missing cache count is unknown, not zero.** `cache_share` is computed only
  over rows that reported both input and cached counts, so a provider that omits
  cache fields no longer turns a cold prompt into `cache 0% warm`.
- **`live=false` before the first `/limits` response is `○ connecting`, not
  `○ log only`.** The poll exposes `attempted()`, and the pool line says
  `live connecting… · showing the log meanwhile` until the first response lands.
- **Windows-safe command quoting.** A provider's `api_key_command` is split with
  POSIX rules on Linux/macOS and handed to `cmd.exe` as written on Windows
  (`shlex` mangles quoted paths such as `"C:\Program Files\op.exe"` and would
  report every provider as unconfigured). Commands `tamias` prints for the user
  are quoted with `subprocess.list2cmdline` on Windows and `shlex.join`
  elsewhere. Tests that assert POSIX file modes and the systemd installer are
  skipped where those concepts do not exist.

### Fixed

- `scripts/credit_ab_check.py` printed a two-arm A/B procedure it could not run:
  `--allow-spend` without `--candidates` exited 2. The docs and the code now
  describe the same path.
- `_opencode_serve_command` emitted `--cheap-model <free-model>` unquoted, where
  the shell would read `<free-model>` as a redirect.

## [1.0.2] - 2026-11-08

A packaging, routing and honesty pass: the pop-up travels with the wheel and
actually loads, its savings bar says what it measured, the two surfaces agree
on every number they both print, CI now fails when the TUI breaks, and
`tamias reconcile` compares real billed costs instead of writing
`difference: null`.

### Changed

- **The pop-up bar says `realised`, not `saved`.** It counts only rows the
  proxy actually rewrote. In shadow mode it stays at zero and the header adds
  `· shadow`; the hypothetical estimate for shadow decisions remains in
  `tamias report` / `/tamias-report`. The two numbers were easy to confuse.
- **`pip install tamias` now carries the OpenCode pop-up.** The wheel and sdist
  ship `tui.tsx`, `src/` and `tsconfig.json` as `tamias/plugin/`; setup
  registers that copy, so no source checkout or `--plugin` is required. The
  `tsconfig.json` is load-bearing: without `jsxImportSource: @opentui/solid`
  the host's Bun loader tries React's JSX runtime and the import fails.
- **The first live reconcile found billed costs above the computed list price.**
  On the operator log (1459 rows, 1440 passthrough excluded), 10 of 19 metered
  rows were comparable: 8 matched (7 free at `$0.00`) and 2 paid DeepSeek rows
  were billed about 49% and 60% above the computed list price, so the command
  exited `3` (`FAIL`). An `--online` lookup returned those stored bills exactly
  and named the served endpoints: InferenceNet at `$0.103/M` prompt and
  `$0.6/M` completion, and DeepInfra at `$0.14/M` and `$0.42/M`, both above the
  sheet's model-level rates. The gap is endpoint pricing, not list-price
  arithmetic (corrected 2026-11-09). No savings claim is proven; the rates were
  not changed on two rows.
- **`tamias report` uses the provider's bill for the actual side.** The
  realised saving is `requested (warm list price) - actual used`; when a row
  carries `provider_cost_usd`, that measured number is the actual side instead
  of list-price arithmetic for the used model. The line says how many actual
  costs came from the provider bill. The requested side remains a list-price
  counterfactual, because a counterfactual has no invoice.

### Added

- **`x-tamias-pin` keeps the model the caller named.** Active routing rewrites
  a model the client chose, and a caller that knows a step is hard had no way to
  say so without turning the router off. Send `x-tamias-pin: 1` (or
  `true`/`yes`/`on`) and the request is logged `STAY` with
  `pinned by client: kept <model>`, in every mode. Unrecognised values are
  ignored, so a stray header cannot disable routing by accident, and the header
  is dropped before the upstream request is sent.
- **The cache share is on the KPI line.** The pop-up now reports what share of
  input tokens the provider served from cache, because that is the number that
  makes a switch legible: a warm conversation is exactly the one where moving
  re-ingests context. It is context for the savings bar, not a saving, and it is
  `null` — shown as nothing, never as `0%` — when no row reported input tokens.
- **Provider quota reaches `/limits` and the pop-up.** A provider that publishes
  its own remaining requests (`x-ratelimit-remaining-requests` or
  `ratelimit-remaining`, resets in seconds, a duration like `1m30s`, or an
  HTTP-date) is recorded on a *successful* answer and shown as one `quota`
  line. Nothing is invented: no headers means no row, not "unlimited", and a row
  older than 10 minutes is dropped. A quota is never a cooldown and never
  changes a routing decision.

- **The plugin gate is real.** CI runs `bun install --frozen-lockfile`,
  `tsc --noEmit`, and the 48-check host harness. The stale `package-lock.json`
  was replaced with `bun.lock`; `npm ci` could not install it.
- **Packaged plugin imports and renders.** A clean-venv wheel install now
  passes the full harness from `site-packages/tamias/plugin`.
- **`service install` names the provider environment the unit will not have.**
  A `systemd --user` unit inherits no shell, so an `api_key_env` provider is
  silently unconfigured under it. The command now reads the catalogue and prints
  the variables (and the `api_key_command` alternative) on stderr.
- **`tamias reconcile --online` reads the provider's own record.** It calls only
  `GET /api/v1/generation?id=…`, uses `data.total_cost` as the billed side, and
  keeps the stored `provider_cost_usd` in the artifact for comparison. Only
  `gen-` ids are OpenRouter generations; other providers' ids are skipped, not
  counted as failed lookups. The key comes from `--api-key-env` and is never
  printed. A failed lookup leaves the row unknown and prevents a `PASS`. A live
  run looked up 12 generations with zero failures and one skipped non-OpenRouter
  id.

- **A `pip install tamias` pop-up now actually loads.** The wheel shipped the
  TypeScript only, and OpenCode resolves a plugin's imports *beside* the plugin
  directory — so a clean install registered `site-packages/tamias/plugin`, which
  has no `node_modules`, and OpenCode refused it with `Cannot find package
  'solid-js'` and a permanent `1 plugin failed` toast. `opencode setup` now
  stages the bundle at `~/.tamias/plugin`, runs `bun install` there and
  registers that path. Verified in a real OpenCode 2.2.5 TUI against a clean
  venv with no source checkout reachable. Bun must be on `PATH`; without it
  setup says so instead of writing a broken registration.
- **`tamias opencode uninstall` undoes the wiring.** It removes only
  `provider.tamias`, plugin entries that name this project's plugin, the
  `tamias` / `tamias-report` command files and the staged `~/.tamias/plugin`
  directory (`--keep-plugin` leaves it). Every other provider, plugin and
  command is preserved; each rewritten config is backed up first, and
  `--dry-run` reports without changing anything.

### Docs

- **The credential-bridge contract is written down.** What a helper must print
  (the key and nothing else), what it must do on failure (nothing, non-zero),
  and a read-only OpenCode-store example. `providers.toml` and the systemd unit
  both link to it.

### Fixed

- **A client credential can no longer follow a provider switch.** The provider
  catalogue dropped only `authorization`; an Anthropic client sends `x-api-key`,
  so that header reached whichever provider the model was routed to. Every
  known client credential spelling (`authorization`, `proxy-authorization`,
  `x-api-key`, `api-key`, `x-goog-api-key`) is now stripped before the
  provider's own credential is applied. Covered on the chat route and on
  `/v1/messages`.
- **`/v1/messages` rows now carry the provider's response id.** The buffered
  path reads the response `id`; the streaming path reads `message.id` from
  `message_start`. Both are stored as `generation_id`, so
  `tamias reconcile --online` can verify an Anthropic response the same way it
  verifies a chat one.
- **`tamias reconcile` was a stub that could not fail.** It listed rows with a
  `generation_id`, ignored `--since`/`--until`/`--tolerance`, never read a
  billed cost, and wrote `difference: null`. It now compares each selected
  row's `computed_list_cost_usd` with the provider-reported
  `provider_cost_usd` (or the online lookup), prints matched/mismatched counts
  and a real difference, and exits `0` PASS, `3` FAIL, `2` INCOMPLETE or
  `ZERO_ROWS`, `1` runtime error. `--prices` was removed: the command compares
  the claim made at request time, not a re-pricing with a newer sheet.
- **The adapter wires never learned a refusal.** `/v1/messages` and
  `/v1/responses` relayed a `429`/`404` straight to the client: the same
  request the chat route failed over was a failed turn on the Anthropic wire,
  and the refusal was not recorded for later requests. Both adapters now learn
  it; `/v1/messages` retries it on the next gated model before the first
  streamed byte, and both refuse a model no configured provider serves instead
  of relaying it to whichever provider happened to be last.
- **The pop-up invented a price where the proxy reports UNKNOWN.** Cached
  tokens against a model that never quoted a cached rate were charged at the
  input rate, so `/tamias` showed a number `tamias report` refuses to print.
  A bucket with tokens in it and no quoted rate is unknown there too.
- **An uncounted cache-write bucket was priced as zero.** A model that quotes a
  nonzero cache-write rate, and a request that never reported how much was
  written, is UNKNOWN rather than free -- the rule the proxy already applied.
- **The two surfaces rendered the same amount differently.** `report` and
  `/tamias` are read side by side, but one printed six decimals under a dollar
  and two above while the other printed four and two, so `$4.9000` and `$4.90`
  both appeared for one saving. Both now share one rule set, with a test that
  renders a table of amounts through both formatters and asserts equality.

### Added

- **An accuracy and precision suite, checked three ways.**
  `tests/test_accuracy_precision.py` runs the same rows through the proxy's
  arithmetic, the pop-up's TypeScript (via Bun) and an independent Decimal
  model written from the documented rules, and asserts agreement to 1e-15.
  Cases cover the boundaries: an unquoted bucket, writes that overrun the
  prompt, a model the sheet never quoted, micro rates, no counts at all, and a
  switch whose baseline is dearer than what ran. A seeded fuzz over the same
  boundaries runs every time, and the seed is fixed so a failure repeats. The
  suite found all three fixes above.

## [1.0.1] - 2026-11-05

A correctness and experience pass over the 1.0.0 routing release.

### Changed

- **`/tamias` is a compact pop-up, not a full-screen panel.** The TUI plugin
  opens a centred `large` dialog with the savings bar, KPI line, latest route
  and reason, eligible pool, cooling models and a footer. Escape closes it; the
  first open shows a one-line hint. The unused dashboard, request-table and
  session-sidebar views were removed.
- **The report command is renamed when the pop-up owns `/tamias`.** With the
  plugin installed, `tamias opencode install` writes `tamias-report.md`, so
  `/tamias` belongs to the pop-up and `/tamias-report` runs the report. A
  standalone install keeps `/tamias`. `opencode setup` warns when a legacy
  `tamias.md` may shadow the pop-up.
- **`tamias service install --providers`.** A systemd unit can route across a
  provider catalogue; `--providers` wins over `--provider`.

### Fixed

- **Active-mode savings were invisible.** A row the proxy actually rewrote
  compared its used model with the same target and reported `$0.00`. The report
  now prices applied rows as `requested → used` and shadow/declined rows as
  `used → target`, so estimated and realised savings are distinct and a real
  run shows the money it saved.
- **A switch to an equal or dearer model is refused.** Rule 4 now takes only a
  target cheaper than the requested model, using the effective price sheet.
  This stops switching that bought no saving and discarded the warm prompt
  cache.
- **Hysteresis follows the chosen target.** Being on *some* pool member no
  longer counts as already cheap; a conversation on a dearer member moves to
  the cheaper target the gates picked.
- **The offline pop-up keeps its claim readable.** `unavailable · showing the
  log alone` precedes the raw transport error, so a narrow terminal clips the
  detail rather than the explanation.

## [1.0.0] - 2026-11-04

The first public release: one OpenAI-compatible endpoint in front of every
provider you have, choosing the cheapest model that can do the work, failing
over when one refuses, and metering the result.

### Added

- **Multi-provider routing.** `providers.toml` lists each provider's base URL
  and where its key comes from (`api_key_env` or a command that prints it).
  `models.toml` maps a model id to the provider that serves it, so the same
  request can move between direct NVIDIA, OpenRouter, OpenAI, Ollama and any
  other OpenAI-compatible endpoint. Credentials are read at start-up and never
  written down.
- **Three gates and cheapest-first ranking.** Capability (tools, context window,
  modality, reasoning), supply (a configured provider serves it, the key was
  read, the sheet can price it when required) and health (not cooling down) run
  independently, each recording its own refusal. Survivors rank by blended price
  (75% input / 25% output), then quality, then id, behind a per-step quality
  floor. `GET /limits` returns the pool, the gates' counts and every refusal.
- **Failover that learns.** `429` and `5xx` cool a model down for the window the
  provider states; transport failures are remembered too; `404` is recorded as
  `not offered for this account (404)` for 12 hours, with no escalating strike
  ladder. Both the streaming and buffered paths retry on the next gated
  candidate, preferring one at least as capable as the model that refused.
- **`discover = "listed"`.** A provider whose `/v1/models` over-advertises can
  be pinned to an explicit list; discovery then returns exactly the models the
  operator named, in both directions.
- **A price overlay that survives re-fetching.** `prices.local.toml` sits beside
  the generated sheet and wins per model id, so `tamias prices fetch` cannot
  erase a hand-written rate. `tamias prices set MODEL --input 0 --output 0`
  writes one entry.
- **`tamias opencode setup`.** Merges the `tamias` provider into OpenCode's
  config, backs the file up first, registers the TUI plugin in `cli.json`, warns
  when JSONC comments are dropped, and prints the matching `tamias serve`
  command. `--dry-run` writes nothing; `install` still only prints the block.
- **An OpenCode TUI panel.** `tamias-opencode-plugin` 0.3.0 adds a
  `session.panel` dashboard beside the chat: requests, tokens, cost, the routed
  model, and free-versus-paid tiers read from the price sheet.
- **The benchmark oracle (F-35 / §18 Phase 3).** `docs/benchmark/tasks.md`
  specifies five benchmark tasks and the exact pass/fail predicate for each;
  `tests/benchmark/tasks_fixture.py` generates them deterministically;
  `tests/benchmark/oracle.py` scores them; `tests/benchmark/test_oracles.py`
  proves each oracle fails the buggy fixture and passes the fixed one. T5
  carries an anti-gaming clause (F-14): the oracle rejects any edit under the
  task's `tests/`, because the completion signal must not be authored by the
  graded agent. This is the apparatus `SSOT:160` calls "the entire gate". It
  makes the benchmark *runnable*; power, sample size and the runner remain open.
- **The three benchmark arms, including the F-18 null hypothesis.**
  `tests/benchmark/arms.py` defines arm A (routing off), arm B (routing active)
  and arm **C** (`static_cheap`): the cheap model pinned in the agent's own
  configuration with **no proxy**. `tests/benchmark/test_arms.py` asserts arm C
  uses no proxy and pins the cheap model, and that arms A and B are
  byte-identical configs differing only in the serve-time routing mode. Without
  arm C, a positive A-vs-B delta proves only that swapping models is cheaper —
  which one config line already achieves.

### Changed

- Repository cleanup: removed internal design and demo material and repaired
  references to files that left the public tree.

### Fixed

- **`404` no longer looks like a client error.** An upstream `404` means the
  provider does not serve that model for this account; it is recorded and the
  request fails over instead of being returned to the agent.
- **`tamias report` no longer warns about its own overlay.** The stored sheet
  identity is compared against the effective `base + overlay` sheet, not the
  generated file alone.
- **Prune tests no longer depend on the calendar.** They anchor to
  `datetime.now(UTC)`, so a date rollover cannot fail the gate.
- **The publish-state guard skipped `docs/audit/claims.md`
  (`tests/test_docs_integrity.py`).** The code-truth ledger is the file that
  *outranks* the SSOT, yet it was absent from the guard's file list, so it kept
  asserting the package was unpublished for a full release after `0.1.0a8`
  shipped. The file is now scanned: reintroducing the stale wording fails the
  gate.
- **Two stale rows in `docs/audit/claims.md`.** The "nothing is on real PyPI"
  row is corrected to PyPI `0.1.0a8`; the "type hints everywhere" row moves from
  `VERIFIED` to `UNVERIFIED`, because no type checker runs in the gate.

### Verified

- **The published `0.1.0a8` artifact matches source.** `pip download
  tamias==0.1.0a8` (wheel and sdist); `diff -rq` against `src/tamias` is empty
  but `__pycache__`; the wheel carries `__version__ = "0.1.0a8"`, the F-1
  `assume_cache_warm` saving, and the F-12 passthrough counter.

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
