# demo/history — three weeks of synthetic agent history

**SYNTHETIC. NOT A RECORD OF REAL WORK.**

This folder exists only for the demo. Its rewritten rows are fabricated: the
never-published `v0.1.0a4`/`v0.1.0a5` alpha tags rejected `active` routing, so
those tags could not produce this log. Current source (`0.1.0a6`) re-adds
`active` fail-closed, and the published TestPyPI `0.1.0a3` accepts it, but this
log is still **not** a capture of any real run — it shows what a routed history
would look like and lets the reporting and dashboard paths be exercised offline.
Nothing here is evidence that tamias routes traffic; it is evidence that the
*reporters* handle a log of that shape.

It tells the "the agent coded intensively for three weeks" story behind the
numbers the demo shows: which tasks the agent attempted, how many requests each
session took, how many the router moved to the cheap model, and what the same
traffic would have cost on the strong model alone.

| file | what it is |
| --- | --- |
| `agent-3weeks.db` | a real tamias request log, same schema `tamias serve` writes — **not tracked; regenerate it first** |
| `agent-3weeks.json` | per-session, per-day and per-week summary — not tracked; regenerate it first |
| `SUMMARY.md` | the same summary as a table, for reading on screen (tracked) |
| `../make_history.py` | the generator; not a fake table but a script |

The row closest to hand is `agent-3weeks.db`. It is **not committed** (it is
machine-local, and a binary log does not belong in git); generate it before
running the commands below:

```sh
# generate the log and its summary for the window the table above was built for
.venv/bin/python demo/make_history.py --end 2026-10-06 --days 21 --seed 7 --out demo/history
```

Because it is written by the real `tamias.store.Store`, every existing reader
works on it with no special case:

```sh
# the numbers, priced at list prices
.venv/bin/python -m tamias.cli report --db demo/history/agent-3weeks.db --prices prices.simulated.toml

# a live dashboard over the whole three weeks
.venv/bin/python -m tamias.cli dashboard --db demo/history/agent-3weeks.db --prices prices.simulated.toml
```

Regenerate (same seed reproduces the same numbers; `SUMMARY.md` is the tracked
table):

```sh
.venv/bin/python demo/make_history.py --end 2026-10-06 --days 21 --seed 7
```

## What the numbers depend on

- **The router decided.**  The SWITCH/STAY decisions in the log are the output
  of the real `tamias.router.decide`, fed the same message shapes an agent
  session produces.  That is why the timeline alternates strong/cheap/strong the
  way a real routed run does, complete with the `min_gap` hysteresis.
- **Token counts are derived, not invented.**  `input` is the actual size of the
  messages the script builds (≈ chars / 4), `cached` is the prefix turn N−1 had
  seen, and `output` is sized by the assistant message.  The three counts agree
  with each other instead of being three unrelated random numbers.
- **the arithmetic is real.**  Every `cost_usd` comes from
  `tamias.pricing.compute_cost` against `prices.simulated.toml`.  Each row is
  stamped `price_simulated = 1` and `price_sheet = prices.simulated.toml`, so
  `tamias report` prints `SIMULATED PRICES, NOT REAL SAVINGS` on every line
  that quotes an amount.

## How to read the report

```text
requests: 1165
total cost (computed, not billed; SIMULATED prices): $7.5707
requests shadow mode would have switched: 428
billed cost: UNKNOWN
...
estimated saving: $0.00 ...
realised saving (rows the proxy actually rewrote): $3.4258 over 428 rows ...
```

The two saving lines are not contradictory:

- **estimated saving** prices the switch whose decision was recorded *but not
  applied* (shadow mode, `model_used == model_requested`). This synthetic log was
  generated as if routing had been applied, so it has no shadow-only rows and
  that line is `$0.00`.
- **realised saving** prices the rows the log marks as rewritten
  (`model_used != model_requested`): `$3.4258` — the report's number. It is
  **cache-aware**: the stay model is priced with a warm cache and the switch
  model with the row's real counts, so the switch is debited for re-ingesting
  context the stay model would have read from cache. Those rows are fabricated
  for the demo; the 0.1.0a4 alpha cannot produce them.

`SUMMARY.md`'s headline saving (`$3.8563`, 33.8%) is **not** the report's
figure. It is the older same-tokens gap with no cache-rebuild charge, so it is an
**upper bound** on the simulated routing gap, not a measured saving. The
cache-aware `realised saving` is lower precisely because it debits the switch.

## Honest limits (say these out loud)

- **Synthetic.**  No request here was ever sent to a model.  The tasks, the
  token counts and the `passed` outcomes are invented by `make_history.py`.
- **Simulated prices.**  `prices.simulated.toml` declares `simulated = true`;
  the rates are made up.  It would be wrong to call any amount here money that
  was spent.
- **List prices, not a bill, and an upper bound.**  `SUMMARY.md`'s saving is the
  gap between pricing the same tokens on the strong model and on the model that
  answered.  It does not charge a real switch for the cold cache it would start
  with, so the report's cache-aware `realised saving` is smaller.
- **No quality claim.**  A 40-request session "passing 14/14 tests" is a field
  in the script, not a test run.  The demo shows the cost arithmetic working,
  not that the cheap model did the work well.