# Tamias — Demo script

**Captured:** 2026-10-10 on this machine, offline, at the commit this file
ships with. Every command below was run and its output pasted verbatim. Nothing
here is expected output written from memory.

**Guaranteed:** all three acts need no network and no API key. They read the
committed logs in `demo/` and `demo/history/`. If the suite is green
(`.venv/bin/python -m pytest -q`), these pass.

**Alpha boundary:** in 0.1.0a4 tamias does **not** rewrite provider requests.
`active` routing is rejected (`tamias serve --router-mode active` →
`invalid choice: 'active'`). Act 1 shows only shadow decisions, and Act 2's
rewritten rows are **synthetic** — no current command can produce them. Every
dollar figure below is arithmetic over a simulated price sheet, labelled as
such in the output itself.

Run everything from the repo root:

```sh
cd /home/krish/Desktop/tamias
```

---

## Act 1 — Metering, and refusing to invent a number

Two arms of the same task. Under the alpha **both** run `--router-mode shadow`:
the `baseline` arm configures no cheap model, the `routed` arm configures one.
Neither rewrites anything, so both run the strong model — the difference is only
the switch *target* each records.

```sh
.venv/bin/python -m tamias.cli report --db demo/runs/routed-1.db   --prices prices.openrouter-sim.toml
.venv/bin/python -m tamias.cli report --db demo/runs/baseline-1.db --prices prices.openrouter-sim.toml
```

Captured output — the two reads are identical, because neither arm rewrote a row:

```text
requests: 12
total cost (computed, not billed; SIMULATED prices): $0.001559
free-model rows: 12 of 12; real billed cost for those is $0, so dollar figures above are hypothetical
requests shadow mode would have switched: 5
billed cost: UNKNOWN
price provenance: prices.openrouter-sim.toml
estimated saving: $0.000595 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]

cheap model assumed: nvidia/nemotron-3.5-lightning:free
sheet passed on the command line (used only for savings estimates): prices.openrouter-sim.toml (2026-10-04)
```

The arms differ where it counts — the recorded decision, not the bill:

```sh
.venv/bin/python - <<'PY'
import sqlite3
for db in ("baseline-1", "routed-1"):
    c = sqlite3.connect(f"demo/runs/{db}.db")
    rows = list(c.execute("SELECT decision_action, decision_target_model FROM requests"))
    sw = [r for r in rows if r[0] == "SWITCH"]
    print(db, "SWITCH", len(sw), "targets", sorted({r[1] for r in sw}))
PY
```

Captured:

```text
baseline-1 SWITCH 5 targets ['']                                  # no cheap model configured
routed-1   SWITCH 5 targets ['nvidia/nemotron-3.5-lightning:free']
```

**What to point at:**

- `billed cost: UNKNOWN` — the proxy records usage, not the bill. It does not
  guess.
- `0` rows rewritten in both arms: the alpha does not change which model the
  upstream runs. The only difference is the recorded target.
- Every dollar figure carries `[SIMULATED PRICES, NOT REAL SAVINGS]` and
  `estimate; ignores cache rebuild cost; not measured`.
- `free-model rows: 12 of 12` — these models cost `$0` for real, so the
  figures are arithmetic over a simulated sheet, not money saved.

---

## Act 2 — Three weeks of synthetic history

This log is **synthetic**; see [`demo/history/README.md`](demo/history/README.md).
It is a real tamias request log on the real schema, but a script wrote it, and
its 428 "rewritten" rows are fabricated to show what a routed history would look
like — the current alpha cannot produce them. It exercises the same reporting
path with no special case.

```sh
.venv/bin/python -m tamias.cli report --db demo/history/agent-3weeks.db --prices prices.simulated.toml
```

Captured output:

```text
requests: 1165
total cost (computed, not billed; SIMULATED prices): $7.5707
requests shadow mode would have switched: 428
billed cost: UNKNOWN
price provenance: prices.simulated.toml
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
realised saving (rows the proxy actually rewrote): $3.8563 over 428 rows (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: cheap
sheet passed on the command line (used only for savings estimates): prices.simulated.toml (2026-10-04)
```

The sheet matters: this log's rows are stamped against `prices.simulated.toml`.
Hand it `prices.openrouter-sim.toml` instead and the rows go unpriced — the
output then reports unknown cost rather than failing. Use the sheet the log
names.

Regenerate it (same seed reproduces the same numbers):

```sh
.venv/bin/python demo/make_history.py --end 2026-10-06 --days 21 --seed 7
```

---

## Act 3 — The dashboard

```sh
.venv/bin/python -m tamias.cli dashboard --db demo/history/agent-3weeks.db \
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
| `/api/rows` | 1165 rows |

Stop it with `Ctrl-C`.

**Honesty line about these totals:** $7.57 against $11.43 is a simulated
sheet's arithmetic over invented sessions, labelled as such on the page by the
banner the server prints at startup. It is a demonstration of measurement and
presentation, not a saving anyone banked.

---

## What was not demonstrated, and why

**A live agent calling a real provider.** It needs a working key, costs money,
and free models return HTTP 429 unpredictably — so it is shown only after two
consecutive rehearsals pass, and dropped for the offline acts if it does not.
Everything above was produced without a network.

**Request rewriting.** The alpha rejects `active` routing, so nothing here
rewrites a provider request. Act 1 records shadow decisions; Act 2's rewritten
rows are synthetic and labelled.
