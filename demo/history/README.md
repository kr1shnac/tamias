# demo/history — three weeks of synthetic agent history

**SYNTHETIC. NOT A RECORD OF REAL WORK.**

This folder exists only for the demo.  It lives on the `swaroop` branch and is
removed by deleting that branch, so nothing here needs to survive the demo.

It tells the "the agent coded intensively for three weeks" story behind the
numbers the demo shows: which tasks the agent attempted, how many requests each
session took, how many the router moved to the cheap model, and what the same
traffic would have cost on the strong model alone.

| file | what it is |
| --- | --- |
| `agent-3weeks.db` | a real tamias request log, same schema `tamias serve` writes |
| `agent-3weeks.json` | per-session, per-day and per-week summary |
| `SUMMARY.md` | the same summary as a table, for reading on screen |
| `../make_history.py` | the generator; not a fake table but a script |

The row closest to hand is `agent-3weeks.db`.  Because it is written by the real
`tamias.store.Store`, every existing reader works on it with no special case:

```sh
# the numbers, priced at list prices
.venv/bin/python -m tamias.cli report --db demo/history/agent-3weeks.db --prices prices.simulated.toml

# a live dashboard over the whole three weeks
.venv/bin/python -m tamias.dashboard --db demo/history/agent-3weeks.db --prices prices.simulated.toml
```

Regenerate (same seed reproduces the same numbers):

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
total cost: $7.5707  [SIMULATED PRICES, NOT REAL SAVINGS]
requests shadow would have switched: 428
billed cost: UNKNOWN
...
estimated saving: $0.00 ...
realised saving (rows the proxy actually rewrote): $3.8563 over 428 rows ...
```

The two saving lines are not contradictory:

- **estimated saving** prices the switch whose decision was recorded *but not
  applied* (shadow mode, `model_used == model_requested`).  The generated run is
  all active mode, where every recorded switch was applied, so that line is
  `$0.00` and carries no news.
- **realised saving** prices the rows the proxy actually rewrote
  (`model_used != model_requested`): `$3.8563` — the headline number.

## Honest limits (say these out loud)

- **Synthetic.**  No request here was ever sent to a model.  The tasks, the
  token counts and the `passed` outcomes are invented by `make_history.py`.
- **Simulated prices.**  `prices.simulated.toml` declares `simulated = true`;
  the rates are made up.  It would be wrong to call any amount here money that
  was spent.
- **List prices, not a bill.**  The saving is the gap between pricing the same
  tokens on the strong model and on the model that answered.
- **No quality claim.**  A 40-request session "passing 14/14 tests" is a field
  in the script, not a test run.  The demo shows the cost arithmetic working,
  not that the cheap model did the work well.