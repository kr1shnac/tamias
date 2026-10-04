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
deterministic. `inject_usage` is off by default, `with_usage_included` runs only
under that flag, and no `serve` flag sets it, so tamias injected nothing.

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

## What could not be verified

- **Real per-token arithmetic.** The agent's rows *do* carry token counts — 8 of 8
  in shadow mode, 7 of 8 in active mode, the first being a request that logged
  none. What has never been checked is those counts priced against a **non-zero**
  rate: every rate in `prices.openrouter.toml` is a real 0, so only the free path
  is verified against a live upstream. The invented rates in
  `prices.openrouter-sim.toml` have never been checked against a real bill.
- **The stage 3 saving figure.** `$0.069811` is arithmetic over invented prices
  applied to real token counts. It is a rehearsal of the report's shape, not a
  measurement, and it should never be quoted as money saved.
- **Attribution of the successful edit.** Active mode switched 2 of 8 requests,
  the task passed, but a metadata-only log cannot attribute the passing edit to
  the strong model or the cheap one.
- **Rate limiting.** 19 requests in total across the three stages, no 429. The
  free-tier limit, the backoff path and the 30-second retry were not exercised.
- **OpenRouter's usage-without-`include_usage` behaviour over time.** Observed
  both ways in five direct streaming requests, so the stage 2 informational line
  can read `no` or `yes` on different days. Neither value is a failure, but the
  behaviour is not characterised.
- **Multi-session continuity.** Sessions were derived as salted hashes every
  time; resuming an existing session, and any `opencode run --continue`
  behaviour, was not exercised.
- **The `/v1/responses` path.** Only OpenAI-style `/v1/chat/completions` was
  proxied, as the caveats in [OPENROUTER.md](OPENROUTER.md) describe.
