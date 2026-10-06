# Tamias — Demo script

**Captured:** 2026-10-06 at `940fd64` + `61701d9`, on this machine, offline.
Every command below was run and its output pasted verbatim. Nothing here is
expected output written from memory.

**Guaranteed:** all three acts need no network and no API key. They read the
committed logs in `demo/` and `demo/history/`. If the suite is green
(`.venv/bin/python -m pytest -q` → 590 passed), these pass.

Run everything from the repo root:

```sh
cd /home/krish/Desktop/tamias
```

---

## Act 1 — Metering, and refusing to invent a number

Two arms of the same task: `baseline` runs everything on the strong model in
shadow mode, `routed` actually rewrites mechanical tool work to a cheap model.

```sh
.venv/bin/tamias report --db demo/runs/routed-1.db   --prices prices.openrouter-sim.toml
.venv/bin/tamias report --db demo/runs/baseline-1.db --prices prices.openrouter-sim.toml
```

Captured output, `routed-1`:

```text
requests: 12
total cost (computed, not billed; price provenance unknown): $0.001202
free-model rows: 12 of 12; real billed cost for those is $0, so dollar figures above are hypothetical
requests shadow would have switched: 3
billed cost: UNKNOWN
price provenance: provenance unknown (12 of 12 priced rows)
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
realised saving (rows the proxy actually rewrote): $0.000357 over 3 rows (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: nvidia/nemotron-3.5-lightning:free
sheet passed on the command line (used only for savings estimates): prices.openrouter-sim.toml (2026-10-04)
```

Captured output, `baseline-1`:

```text
requests: 12
total cost (computed, not billed; price provenance unknown): $0.001559
free-model rows: 12 of 12; real billed cost for those is $0, so dollar figures above are hypothetical
requests shadow would have switched: 5
billed cost: UNKNOWN
price provenance: provenance unknown (12 of 12 priced rows)
estimated saving: $0.000595 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]

cheap model assumed: nvidia/nemotron-3.5-lightning:free
sheet passed on the command line (used only for savings estimates): prices.openrouter-sim.toml (2026-10-04)
```

**What to point at:**

- `billed cost: UNKNOWN` — the proxy records usage, not the bill. It does not
  guess.
- Every dollar figure carries `[SIMULATED PRICES, NOT REAL SAVINGS]` and
  `estimate; ignores cache rebuild cost; not measured`.
- `free-model rows: 12 of 12` — these models cost `$0` for real, so the
  figures are arithmetic over a simulated sheet, not money saved.
- **Do not compare these two totals.** The arms wrote different row counts
  across the three pairs, so a dollar gap measures how much work the agent did.
  The comparable evidence is `decision_action` and `model_used` vs
  `model_requested`. See `demo/RUNBOOK.md` §"known limits".

---

## Act 2 — Three weeks of routed history

The proxy's own log, run through the same reporting path. No special case: it
is a normal request log, just stamped simulated.

```sh
.venv/bin/tamias report --db demo/history/agent-3weeks.db --prices prices.simulated.toml
```

Captured output:

```text
requests: 1165
total cost (computed, not billed; SIMULATED prices): $7.5707
requests shadow would have switched: 428
billed cost: UNKNOWN
price provenance: prices.simulated.toml
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
realised saving (rows the proxy actually rewrote): $3.8563 over 428 rows (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: cheap
sheet passed on the command line (used only for savings estimates): prices.simulated.toml (2026-10-04)
```

The sheet matters: this log's rows are stamped `price_simulated` against
`prices.simulated.toml`. Hand it `prices.openrouter-sim.toml` instead and every
row reads unpriced — the output goes to `n_unpriced: 1165`, `actual_total: 0.0`
rather than failing. Use the sheet the log names.

---

## Act 3 — The dashboard

```sh
.venv/bin/tamias dashboard --db demo/history/agent-3weeks.db \
  --prices prices.simulated.toml --port 8012
```

Then open `http://127.0.0.1:8012/`.

Captured server log:

```text
tamias dashboard: http://127.0.0.1:8012
tamias dashboard: request log demo/history/agent-3weeks.db, prices prices.simulated.toml (2026-10-04)
tamias dashboard: this price sheet declares itself simulated, so every amount on the page is labelled: SIMULATED PRICES - NOT REAL SAVINGS
```

The page is served with HTTP 200 and `<title>Tamias live routing</title>`. It is
a shell that reads `/api/*`, so show the data rather than the empty frame:

```sh
curl -s http://127.0.0.1:8012/api/summary
curl -s http://127.0.0.1:8012/api/nav
curl -s http://127.0.0.1:8012/api/rows | python3 -c 'import sys,json; print(len(json.load(sys.stdin)),"rows")'
```

Captured values:

| fact | value |
|---|---|
| `n_requests` | 1165 |
| `n_priced` / `n_unpriced` | 1165 / 0 |
| `n_switched` | 428 (36.7% share of cheap) |
| `actual_total` | $7.5707 |
| `baseline_total` | $11.4270 |
| `saving_total` / `saving_pct` | $3.8563 / 33.7% |
| projects / sessions | 36 / 36 |
| `/api/rows` | 1165 rows |

Stop it with `Ctrl-C`.

**Honesty line about these totals:** $7.57 against $11.43 is the simulated
sheet's arithmetic over invented sessions, labelled as such on the page by the
banner the server prints at startup. It is a demonstration of measurement and
presentation, not a saving anyone banked.

---

## What was not demonstrated, and why

**A live agent calling a real provider.** It needs a working key, costs money,
and free models return HTTP 429 unpredictably — so it is shown only after two
consecutive rehearsals pass, and dropped for the offline acts if it does not.
Everything above was produced without a network.
