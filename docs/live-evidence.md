# Live evidence: OpenRouter free models through tamias

Observed **2026-10-04** on Linux, Python 3.14.4, opencode 1.18.34. Every number
below was produced by the runs described here, against OpenRouter's live API.

No prompt text, no response text and no credential appears in this file or in any
database it refers to. That is itself one of the results, recorded in
[What could not be verified](#what-could-not-be-verified) and in the checks
section.

## Models

| role | id | real price | simulated price (`prices.openrouter-sim.toml`) |
| --- | --- | --- | --- |
| strong — requested by the agent, the session's starting model | `nvidia/nemotron-3-ultra-550b-a55b:free` | all rates 0 | 3.0 / 15.0 / 0.30, cache_write 0.0 |
| cheap — the router's switch target | `nvidia/nemotron-3.5-lightning:free` | all rates 0 | 0.25 / 1.25 / 0.03, cache_write 0.0 |

Upstream `https://openrouter.ai/api`, served by NVIDIA. Price sheets:
`prices.openrouter.toml` (real, every rate a real 0) and
`prices.openrouter-sim.toml` (`simulated = true`, invented rates). The simulated
sheet is keyed by the real model ids, so it can price a real log; that is what
made the stage 3 report below possible.

Requested and response model never differed. A buffered response through the
proxy reported `model` equal to the requested id, `provider: Nvidia`, and
`usage.cost: 0`.

## Stage 2 — live check, `/tmp/or.db`

Proxy in shadow mode with no cheap model configured, so every decision is STAY.
`scripts/live_check.py`, three probes, `RESULT: PASS`.

| # | request | model_requested | model_used | input | output | cached | cache_write | cost_usd | status | decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | buffered | nemotron-3-ultra-550b-a55b:free | same | 21 | 16 | 0 | `?` | **0.0** | 200 | STAY |
| 2 | stream, no `stream_options` | nemotron-3-ultra-550b-a55b:free | same | 21 | 15 | 0 | `?` | **0.0** | 200 | STAY |
| 3 | stream, `include_usage=true` | nemotron-3-ultra-550b-a55b:free | same | 21 | 15 | 0 | `?` | **0.0** | 200 | STAY |

| count | value |
| --- | --- |
| rows logged | 3 |
| distinct session ids | 1 |
| rows with `cost_usd` = 0.0 | 3 of 3 |
| rows with `cost_usd` NULL | 0 |
| non-200 statuses | 0 |
| HTTP 429 | 0 |
| provider volunteered usage on a stream that did not ask for it | yes |

`cache_write` is `?` on every row, as expected on this OpenAI-style upstream,
whose `usage` object carries no cache-write count. The `cache_write = 0.0` rate
is what stops that from making the cost UNKNOWN.

**Probe 2 is judged on delivery, not on token counts.** The provider volunteered a
usage chunk on a stream that carried no `stream_options`, so the earlier rule
("this row must be NULL") failed against OpenRouter while the proxy was
forwarding the body byte-for-byte. Four back-to-back streaming requests with
identical bodies, sent direct to OpenRouter and through the proxy, returned a
usage chunk in every case; one earlier direct request returned none. OpenRouter
therefore does not honour OpenAI's opt-in, and the observation is not
deterministic. `inject_usage` is off by default and `with_usage_included` runs
only under that flag, and stages 2 to 5 ran without it, so tamias injected
nothing in those runs. `--inject-usage` now exists on `tamias serve`; stages 6
and 7 below are the runs that turned it on.

## Stage 3 — shadow mode with a real agent, `/tmp/or-shadow.db`

`opencode run` against a two-file Python project, driven through the proxy by an
`opencode.json` OpenAI-compatible provider. Task: repair a one-character typo so
the project's own test suite passes. The router recorded what it would have done
and forwarded every request on the strong model.

| count | value |
| --- | --- |
| task completed, project test suite passes | yes (2 passed) |
| rows logged | 8 |
| distinct session ids | 2 |
| requests shadow would have switched (SWITCH) | 3 |
| STAY | 5 |
| rows where `model_used` != `model_requested` | 0 |
| rows with token counts | 8 of 8 |
| rows with `cost_usd` = 0.0 | 8 of 8 |
| non-200 statuses | 0 |

Session ids are salted hashes derived from the `Authorization` header, so they
are stable per key and carry nothing about the conversation.

`tamias report --db /tmp/or-shadow.db --prices prices.openrouter-sim.toml`:

```
requests: 8
total cost: $0.00  [SIMULATED PRICES, NOT REAL SAVINGS]
requests shadow would have switched: 3
estimated saving: $0.069811 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: nvidia/nemotron-3.5-lightning:free
price sheet: prices.openrouter-sim.toml (2026-10-04)
```

The total is `$0.00` because the real sheet prices these models at 0; the saving
figure comes from the invented rates and is stamped as simulated. That the report
priced anything at all is a direct result of the simulated sheet being keyed by
the real model ids.

## Stage 4 — active mode, `/tmp/or-active.db`

Same project reset to its broken state, same task, same agent, proxy restarted
with `--router-mode active` and both model flags.

| count | value |
| --- | --- |
| task completed, project test suite passes | yes (2 passed) |
| rows logged | 8 |
| distinct session ids | 2 |
| rows where `model_requested` != `model_used` | **2** (both SWITCH, both to nemotron-3.5-lightning:free) |
| rows still on the requested model | 6 |
| 4xx statuses | 0 |
| 5xx statuses | 0 |
| rows with `cost_usd` = 0.0 | 8 of 8 |

The switch is visible in the log as a divergence between what the agent asked
for and what the upstream was actually called with, and in active mode the
divergence count (2) is lower than the shadow count would suggest for the same
trajectory (3 in stage 3) because the two runs took different tool paths. Nothing
in the log says which model produced which edit, and that is the design: the log
holds counts, ids and decisions, never content.

## Stage 5 — checks

| check | result |
| --- | --- |
| background processes started (3 proxies) still running | 0 |
| files scanned for the API key (repo excluding `.venv`, the run databases, the scratch project) | 192 |
| files containing the API key | 0 |
| total occurrences of the API key | 0 |
| databases scanned for prompt and response text | 3 |
| occurrences of any prompt or response marker | 0 |

The only free text in any database is a routing reason naming a tool (`bash`,
`glob`, `read`) or a hysteresis counter, plus model ids and salted session ids.
The single text column is `decision_reason`; there is no column for a request
body, a message, a completion or a header.

## Stages 6 and 7 — `--inject-usage` on, `/tmp/or-shadow-inject.db` and `/tmp/or-active-inject.db`

Same project, same task, same two models, as stages 3 and 4, with one difference:
`tamias serve --inject-usage`, so streaming bodies were re-encoded with
`stream_options.include_usage = true`.

**The gate passed: OpenCode ran both sessions to completion with the flag on.**
Neither run reported a parse error, a protocol error or a retry, every row logged
HTTP 200, and the task was fixed in both.

### Stage 6 — shadow, `/tmp/or-shadow-inject.db`

| id | input | output | cached | cache_write | cost_usd | status | decision | latency_ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 7609 | 67 | 0 | `?` | 0.0 | 200 | STAY | 3251 |
| 2 | 561 | 134 | 0 | `?` | 0.0 | 200 | STAY | 4069 |
| 3 | 7702 | 50 | 0 | `?` | 0.0 | 200 | STAY | 3562 |
| 4 | `?` | `?` | `?` | `?` | 0.0 | 200 | STAY | 525 |
| 5 | `?` | `?` | `?` | `?` | 0.0 | 200 | SWITCH | 432 |
| 6 | 7875 | 49 | 0 | `?` | 0.0 | 200 | SWITCH | 2467 |
| 7 | 7949 | 49 | 0 | `?` | 0.0 | 200 | SWITCH | 13420 |
| 8 | `?` | `?` | `?` | `?` | 0.0 | 200 | SWITCH | 485 |
| 9 | 8070 | 103 | 0 | `?` | 0.0 | 200 | SWITCH | 5757 |
| 10 | 8193 | 45 | 0 | `?` | 0.0 | 200 | STAY | 2322 |
| 11 | 8437 | 56 | 0 | `?` | 0.0 | 200 | SWITCH | 8065 |

| count | value |
| --- | --- |
| task completed, project test suite passes | yes (2 passed) |
| rows logged | 11 |
| rows with token counts | **8 of 11** |
| rows still NULL | **3** (ids 4, 5, 8) |
| requests shadow would have switched (SWITCH) | 6 |
| rows where `model_used` != `model_requested` | 0 |
| non-200 statuses | 0 |

`tamias report --db /tmp/or-shadow-inject.db --prices prices.openrouter-sim.toml`:

```
requests: 11
total cost: $0.00  [SIMULATED PRICES, NOT REAL SAVINGS]
requests shadow would have switched: 6
estimated saving: $0.092444 (estimate; ignores cache rebuild cost; not measured; based on 8 of 11 requests with token counts; 2 of 6 switched requests not priced)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: nvidia/nemotron-3.5-lightning:free
price sheet: prices.openrouter-sim.toml (2026-10-04)
```

**Why 3 rows are still NULL with the flag on: the client stopped reading before
the usage chunk arrived.** The split is not random. Every NULL row completed in
432–525 ms; every counted row took 2322–13420 ms. The usage chunk is the *last*
thing on the stream, so a client that already has the tool call it needs closes
the connection first, and there is nothing left to log.

A controlled probe through the same proxy, same flag, same model, identical
prompt, differing only in when the client stopped reading:

| probe | client behaviour | frames read | usage chunk seen by client | row logged |
| --- | --- | --- | --- | --- |
| drain | read to `[DONE]` | 53 | yes | 27 in / 194 out, 6433 ms |
| abandon | broke after 2 frames | 2 | no | `?` / `?` / `?`, 2587 ms |

So `--inject-usage` raises coverage but cannot make it total: **the last chunk is
only loggable by a client that stays long enough to receive it.** This is a
property of the protocol, not a proxy defect. The probe rows were written to the
first shadow database, which was then deleted and the whole stage re-run from
scratch so the 11-row log above contains agent traffic only.

### Stage 7 — active, `/tmp/or-active-inject.db`

| id | input | output | cached | cache_write | cost_usd | status | decision | latency_ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 7609 | 64 | 0 | `?` | 0.0 | 200 | STAY | 8174 |
| 2 | 561 | 45 | 0 | `?` | 0.0 | 200 | STAY | 8939 |
| 3 | 7699 | 47 | 0 | `?` | 0.0 | 200 | STAY | 1703 |
| 4 | 7869 | 52 | 0 | `?` | 0.0 | 200 | STAY | 1749 |
| 5 | 7946 | 46 | 0 | `?` | 0.0 | 200 | SWITCH | 3012 |
| 6 | 8064 | 107 | 0 | `?` | 0.0 | 200 | STAY | 5471 |
| 7 | 8191 | 45 | 0 | `?` | 0.0 | 200 | STAY | 6257 |
| 8 | 8434 | 53 | 6528 | `?` | 0.0 | 200 | SWITCH | 1810 |

| count | value |
| --- | --- |
| task completed, project test suite passes | yes (2 passed) |
| rows logged | 8 |
| rows with token counts | **8 of 8** |
| rows still NULL | **0** |
| rows where `model_requested` != `model_used` | **2** (both SWITCH, both to nemotron-3.5-lightning:free) |
| rows with `cost_usd` = 0.0 | 8 of 8 |
| 4xx / 5xx statuses | 0 / 0 |

Row 8 is the first row in any run to report real cache reads: 6528 cached input
tokens against 1906 uncached, priced by the simulated sheet as
`(1906 * 3.0 + 6528 * 0.3 + 0 * 0.0 + 0 * 0.0 + 53 * 15.0) / 1000000 = 0.0084714`.

`tamias report --db /tmp/or-active-inject.db --prices prices.openrouter-sim.toml`:

```
requests: 8
total cost: $0.00  [SIMULATED PRICES, NOT REAL SAVINGS]
requests shadow would have switched: 2
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured)  [SIMULATED PRICES, NOT REAL SAVINGS]
cheap model assumed: nvidia/nemotron-3.5-lightning:free
price sheet: prices.openrouter-sim.toml (2026-10-04)
```

**That `$0.00` is the report's definition, not a measurement, and it is
misleading in active mode.** The saving line compares the model each request
*actually used* against the sheet's cheapest model. In active mode the two
switched requests already used the cheapest model, so each delta is zero by
construction and the line cancels itself. The saving this run actually realised,
computed from the logged counts with the project's own `compute_cost`, was:

| row | would have cost on strong | actually cost on cheap | delta |
| --- | --- | --- | --- |
| 5 | 0.024528 | 0.002044 | 0.022484 |
| 8 | 0.008471 | 0.000739 | 0.007733 |
| | | **total** | **0.030217** |

`$0.030217` against a printed `$0.00`. The report has no "spent versus always
strong" line, so an active-mode run cannot report its own benefit; this is a
known gap in the tool, not a defect introduced by `--inject-usage`, and it is
left for a separate change.

### Stages 6 and 7 — checks

| check | result |
| --- | --- |
| proxies started for these stages (2) still running | 0 |
| port 8000 after shutdown | free |
| files scanned for the API key (repo excluding `.venv`, 5 run databases, scratch project) | 210 |
| files containing the API key | 0 |
| total occurrences of the API key | 0 |
| new databases scanned for prompt and response text | 2 |
| occurrences of any prompt or response marker | 0 |

Markers searched in both new databases, byte for byte: `fix the typo`,
`test_greet`, `greet.py`, `Hello`, `nmae`, `You are`, `prompt`, `content`,
`Bearer`, `sk-or-`. All zero. Both files are 12288 bytes and contain 16 columns;
the only free-text column is `decision_reason`, holding `easy tool: read`,
`easy tool: glob`, `easy tool: bash` or a hysteresis counter. The two columns
beyond the usual set are `price_sheet_date` (always `2026-10-04`) and
`decision_target_model` (a model id or NULL).

## What could not be verified

- **Real per-token arithmetic.** The agent's rows *do* carry token counts — 8 of 8
  in shadow mode, 7 of 8 in active mode, the first being a request that logged
  none, and with `--inject-usage` on, 8 of 11 in shadow and 8 of 8 in active.
  What has never been checked is those counts priced against a **non-zero**
  rate: every rate in `prices.openrouter.toml` is a real 0, so only the free path
  is verified against a live upstream. The invented rates in
  `prices.openrouter-sim.toml` have never been checked against a real bill.
- **The saving figures.** `$0.069811` (stage 3) and `$0.092444` (stage 6) are
  arithmetic over invented prices applied to real token counts. They are
  rehearsals of the report's shape, not measurements, and they should never be
  quoted as money saved.
- **How often a client abandons a stream.** The cause of the NULL rows in stage 6
  is established by a controlled probe, but the rate is not: one session gave 3
  NULL of 11, another 0 of 8. Nothing predicts which requests get cut short, so
  the share of priced rows will vary run to run.
- **Whether an active-mode run can report its own saving.** It cannot: the
  saving line cancelled itself to `$0.00` against a realised `$0.030217`. The
  report needs a "spent versus always strong" line before an active run can be
  summarised; that change has not been made.
- **Attribution of the successful edit.** Active mode switched 2 of 8 requests,
  the task passed, but a metadata-only log cannot attribute the passing edit to
  the strong model or the cheap one.
- **Rate limiting.** 40 requests in total across the five stages and two probe
  requests, no 429. The free-tier limit, the backoff path and the 30-second retry
  were not exercised.
- **Whether OpenRouter honours an injected `include_usage`.** Unproven, and now
  hard to tell apart from the volunteering behaviour above: stage 6 shows usage
  arriving on streams that asked for it, but stage 2 shows it arriving on streams
  that did not. The flag is not observably what makes the difference.
- **OpenRouter's usage-without-`include_usage` behaviour over time.** Observed
  both ways in five direct streaming requests, so the stage 2 informational line
  can read `no` or `yes` on different days. Neither value is a failure, but the
  behaviour is not characterised.
- **Multi-session continuity.** Sessions were derived as salted hashes every
  time; resuming an existing session, and any `opencode run --continue`
  behaviour, was not exercised.
- **The `/v1/responses` path.** Only OpenAI-style `/v1/chat/completions` was
  proxied, as the caveats in [OPENROUTER.md](OPENROUTER.md) describe.
