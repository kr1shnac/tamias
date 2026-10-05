# The local dashboard

`python -m tamias.dashboard` reads the proxy's sqlite log and serves one local
web page showing which model handled each request, its tokens, its cost, and
what the same request would have cost without routing.

The page is self-contained: no CDN, no web font, no fetched script. It works
offline and talks to nothing but its own two JSON endpoints.

The log is opened **read-only**. The dashboard is meant to be running while the
proxy is writing, and a reader that could write would race it for the file.

## Commands

Live, against a log the proxy is currently writing:

```bash
.venv/bin/python -m tamias.dashboard --db requests.db --prices prices.openrouter-sim.toml --port 8010
```

Then open <http://127.0.0.1:8010>. It polls `/api/rows` every second, so rows
appear as the proxy logs them.

Replay, over a finished session:

```bash
.venv/bin/python -m tamias.dashboard --db session.db --prices prices.openrouter-sim.toml --port 8010
```

Switch the page to **Replay** to reveal the rows one at a time. **Play** and
**Pause** step through it, **Restart** begins again, and the speed buttons
(0.5x, 1x, 2x, 5x) change the interval.

Comparing against a real baseline run:

```bash
.venv/bin/python -m tamias.dashboard \
  --db routed-session.db \
  --prices prices.openrouter-sim.toml \
  --compare-db unrouted-session.db \
  --port 8010
```

`--compare-db` points at a second log, ideally one recorded with routing off.
It is drawn as a dashed third line, "actual baseline run".

The server binds `127.0.0.1` only. The log holds one session's traffic and the
page exposes its cost, so a wider bind has to be asked for by name
(`--host 0.0.0.0`); anything else is refused.

## What the numbers mean

Every amount below is arithmetic over the token counts in the log. None of it is
a measurement of a bill.

**Routed cost** — what the requests actually cost, priced on the model that
answered each one (`model_used`).

**Baseline cost** — what the same requests would have cost on the model that was
asked for (`model_requested`). This is the comparison, not a measurement: it
assumes the stronger model would have handled the identical tokens, which is the
assumption the whole estimate rests on.

**Saving** — baseline minus routed, as an amount and a percentage of baseline.

**On the cheaper model** — the fraction of requests where the model that
answered was not the model that was asked for.

**"not priced"** — the upstream never reported that request's token counts, so
no cost can be computed for it. These rows are counted in the request total and
excluded from every amount. They are not counted as free.

**n/a** — the figure cannot be computed, and is not being reported as zero. A
saving percentage is n/a when nothing is priced, or when the baseline is
legitimately zero (a free model), because that ratio is 0/0. An n/a never means
"nothing happened"; it means the log cannot answer.

The chart's two solid lines are cumulative cost per request: baseline on top,
routed below, with the gap between them being what the routing was worth. The
dashed line is the `--compare-db` run. The lines hold flat across a request
that could not be priced, because no amount is known to add.

If the price sheet declares `simulated = true`, a red banner reading
`SIMULATED PRICES - NOT REAL SAVINGS` appears, because the rates are invented
and the amounts are illustrations rather than money. Use
`prices.openrouter.toml` for what models actually cost.

## Two lines, and what they assume

`baseline` and `routed` are priced on **the same tokens**: each row is priced
twice, once per model, from the one set of counts that row logged. The
comparison is therefore model-only. It assumes:

- **Same tokens assumed.** The baseline assumes the stronger model would have
  produced the same input and output token counts. It usually would not: a
  different model generates different text, so the real baseline is an estimate,
  not a counterfactual that was actually run.
- **Ignores cache rebuild cost.** Switching models means the prompt cache does
  not carry over, so the first requests on a new model pay to re-ingest context
  the baseline would have hit cached. That cost is not in either line, which
  makes the routed line optimistic and the saving smaller in reality than shown.

Both caveats push the same way: the reported saving is an upper bound.

For a figure that does not rest on the counterfactual at all, run the same
session twice -- once routed, once with `--router-mode off` -- and pass the
unrouted log as `--compare-db`. That dashed line is a measurement; the baseline
line is an estimate.

## Reading an old log

A log written before `latency_ms` or the effort columns existed still loads. The
page asks `PRAGMA table_info` which columns are really there, selects only
those, and reports the missing ones as unknown rather than failing or inventing
a zero.

## Endpoints

- `GET /` — the page
- `GET /api/summary` — the headline numbers and the cumulative series
- `GET /api/rows` — one object per logged request

Both JSON endpoints re-read the log on every request, so they always reflect
the newest rows.