# Price sheets and cost arithmetic

A price sheet is the only thing tamias prices with. It is a TOML file of USD
rates, one table per model, and `compute_cost` either returns an exact figure
or returns UNKNOWN and names the reason in the formula. Nothing in between:
there is no fallback rate, no last-known price and no rounding that hides a
missing input.

## Units

- **Rates** are USD per **1,000,000 tokens** (`TOKENS_PER_RATE_UNIT`).
- **Token counts** are whole tokens.
- OpenRouter publishes USD **per token**, as strings (`"prompt":
  "0.00000028"`). `tamias.pricing_fetch.parse_models` multiplies each value by
  1,000,000 in `decimal.Decimal` before it reaches the sheet, so `0.00000028`
  lands as exactly `0.28` rather than as float drift.

## Schema

```toml
date = "2026-10-06"          # required: the sheet's date, a string
simulated = false            # true only when the rates were invented

[provenance]                 # optional: where the rates came from
source = "openrouter-models-api"
url = "https://openrouter.ai/api/v1/models"
fetched_at = "2026-10-06T12:00:00Z"

["openai/gpt-4o"]            # quoted: ids carry '/' and ':'
input = 2.5                  # USD per 1M input (prompt) tokens
output = 10.0                # USD per 1M output (completion) tokens
cached_input = 1.25          # USD per 1M cache-read tokens
cache_write = 0.0            # USD per 1M cache-write tokens
cache_write_1h = 0.0         # optional: defaults to cache_write when omitted
```

Field rules:

- **A rate of `0` is a real price.** The model genuinely costs nothing in that
  bucket, and requests price to `$0.00` rather than to UNKNOWN.
- **An omitted rate is UNKNOWN, never zero.** If a request needs it, the cost
  is UNKNOWN.
- **`[provenance]`** accepts only `source`, `url` and `fetched_at`, all
  non-empty strings. Any other key is refused at load time, because a report
  that quotes provenance cannot audit a claim nobody defined.
- **Tier fields.** A model-table key whose name begins with `tier`
  (e.g. `tier_200k_input = 6.0`) marks the entry as tiered long-context
  pricing. Tiered pricing is unsupported, so any model carrying one prices as
  UNKNOWN whatever its base rates say. Any *other* unrecognised key is still a
  load-time error: a typo must fail loudly, not silently become UNKNOWN.
- **`simulated = true`** changes no arithmetic. It only makes derived reports
  stamp `SIMULATED PRICES, NOT REAL SAVINGS` on every amount, so invented
  numbers cannot read as money.

## The formula

For a model the sheet knows, with every rate and count it needs:

```
usd = (uncached_input * input_rate
     + cached_input   * cached_input_rate
     + 5m_writes      * cache_write_rate
     + 1h_writes      * cache_write_1h_rate
     + output         * output_rate) / 1_000_000

uncached_input = input_tokens - cached_input_tokens - cache_write_tokens
5m_writes      = cache_write_tokens - cache_write_1h_tokens
1h_writes      = cache_write_1h_tokens
```

Precedence and the cache rules:

1. **A provider-billed cost wins.** When the upstream reported
   `usage.cost`, that amount is returned as-is: it is what was charged, so no
   sheet arithmetic is applied — not even to a model the sheet has never heard
   of, and not even when the bill is `$0.00`.
2. **Otherwise the sheet sum is used exactly**, with each bucket at its own
   rate. Cache reads price at `cached_input`, writes at `cache_write`, and
   neither is ever silently priced at the plain `input` rate.
3. **Reasoning tokens are inside `output_tokens`.** The upstream reports them
   as part of the completion, so the output term is added once and never again
   for reasoning.
4. **A free model** — every rate the sheet quotes is `0` — costs `0.00`, even
   when the request reported no counts at all.

## When the cost is UNKNOWN, and why

`CostBreakdown.usd` is `None` and the `formula` says which of these applied:

| Reason | Formula names |
| --- | --- |
| The model is not in the sheet | the model id |
| A rate the request needs is missing, and the matching token count is nonzero | `input_rate`, `output_rate`, `cached_input_rate`, `cache_write_rate`, `cache_write_1h_rate` |
| A count the request needs was not reported | `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_write_tokens` |
| The counts cannot all be true (negative, or cache buckets larger than the input) | `inconsistent usage` |
| The entry carries tier fields | the `tier...` field names |
| The cached-token count is unknown while the cache rate is both nonzero and different from the input rate | `cached_input_tokens` |

A missing rate with a **zero** count costs nothing and stays known: the rate
only matters if tokens reached that bucket. An unreported count with a rate of
`0` is likewise known, because zero dollars times any count is zero dollars.

## Refreshing a sheet

```sh
tamias prices fetch
```

The fetcher reads `https://openrouter.ai/api/v1/models` through an injectable
opener, converts each per-token price with `Decimal`, skips any entry it
cannot price (recording the reason), and writes a sheet in this schema with
`simulated = false` and a `[provenance]` block naming the API, the URL and the
fetch time. Model ids render in sorted order, so the same catalogue produces
the same bytes.

`prices.example.toml` is not that. It is a hand-written illustration, headed
`EXAMPLE - NOT CURRENT`, and its rates have not been re-checked against a
provider.

## What was verified

**Fixtures only.** In the window that wrote this document, no network call was
made and no live catalogue was fetched. What the tests pin down is:

- `parse_models` against canned OpenRouter-shaped JSON: free models, paid
  models with and without cache fields, duplicates, and junk (negative,
  non-numeric, missing, boolean) with a reason recorded for each skip;
- `render_sheet` output round-tripping through `load_price_sheet`, including
  `simulated`, `[provenance]`, deterministic ordering, and omitted cache rates
  staying omitted;
- `fetch_models` through a fake opener that answers from a fixture — no test
  touches the network;
- `compute_cost` for each rule above, plus the differential test in
  `tests/test_hardening_pricing.py` that re-derives 1000 fixed-seed cases from
  the contract.

Rates themselves were **not** verified against OpenRouter or any other
provider here. Run `tamias prices fetch` and read the provenance block before
trusting a sheet's numbers.
