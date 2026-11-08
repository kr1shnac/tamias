# `tamias reconcile`

Compare, row by row, the list-price cost Tamias computed for a request with the
cost the provider reported for that same request. A row is a **match** only
when both numbers exist and differ by no more than the tolerance. A missing
number is `UNKNOWN`, never `$0.00`.

```sh
tamias reconcile --db requests.db
```

Offline is the default: it reads only the request log, calls no provider, and
reads no credential. `--online` additionally asks the provider for its own
generation record.

## Flags

| Flag | Required | Meaning |
| --- | --- | --- |
| `--db PATH` | yes | Path to the SQLite request log produced by `tamias run` / `tamias serve`. |
| `--since YYYY-MM-DD` | no | Lower bound on the row's timestamp (inclusive, calendar date). |
| `--until YYYY-MM-DD` | no | Upper bound on the row's timestamp (inclusive, calendar date). |
| `--tolerance USD` | no | Absolute USD tolerance for a match. Default `0.000001`. A negative value is a runtime error. |
| `--online` | no | Read the provider's generation record for every selected row whose `generation_id` starts with `gen-` and use that `total_cost` as the billed side. Other providers' ids are skipped, not sent to OpenRouter. |
| `--api-key-env NAME` | no | Environment variable holding the lookup key (default `OPENROUTER_API_KEY`). Only read with `--online`; the value is never printed, logged, or placed in argv. |
| `--invoice PATH` | no | CSV export of provider invoice line items; the invoice becomes the only billed side. Cannot be combined with `--online`. |
| `--invoice-join request_id\|generation_id` | no | Match invoice lines to the Tamias row `id` or the provider's `generation_id` (default `request_id`). |
| `--invoice-scope row\|day` | no | One line per request, or one line per UTC calendar day for providers that publish only daily cost totals (default `row`). |
| `--invoice-key-column NAME` | no | CSV identity column (default: the `--invoice-join` name, or `date` for `--invoice-scope day`). |
| `--invoice-cost-column NAME` | no | CSV USD amount column (default `cost_usd`). |
| `--invoice-allow-unmatched` | no | Invoice lines outside the selected window do not block a PASS. Selected rows the invoice does not cover always stay INCOMPLETE. |
| `--out PATH` | no | Write a JSON artifact with one object per selected row. |
| `--json` | no | Emit a JSON summary on stdout instead of the human-readable report. |

`--prices` is deliberately not accepted. Reconciliation compares the number
Tamias claimed at request time with the provider's number; re-pricing the
tokens against a newer sheet would change the claim being tested.

## What each side is

- **Computed** — `computed_list_cost_usd`, the list-price arithmetic stored when
  the request was logged. A pre-split log that only carries the ambiguous
  `cost_usd` column is `UNKNOWN` here; it is not compared against a provider
  number it may already equal.
- **Billed, offline** — `provider_cost_usd`, the cost the provider reported on
  the response Tamias handled. The command does not fetch an invoice.
- **Billed, online** — `data.total_cost` from
  `GET https://openrouter.ai/api/v1/generation?id=<generation-id>`. This reads an
  existing generation; it never creates a completion. Only ids that start with
  `gen-` are OpenRouter generation ids; a `chatcmpl-…`, `msg_…`, or any other
  provider's id is skipped rather than sent to an endpoint that cannot know it.
  A skipped id is not a failed lookup. The stored `provider_cost_usd` is retained
  in the artifact as `logged_billed_cost`, and the lookup value is `lookup_cost`.
- **Billed, invoice** — `--invoice` reads a CSV export from the provider's own
  billing system and joins it to the log. This is the provider-agnostic path:
  OpenRouter's generation lookup is the only per-request billing API tamias has
  documented, so every other provider proves its billed side from its invoice.

### `--invoice`: the provider-agnostic billed side

```sh
# Per-request line items, joined on the provider's response id
tamias reconcile --db requests.db \
  --invoice openrouter-billing.csv \
  --invoice-join generation_id

# Daily cost totals only: OpenRouter and most providers publish these
tamias reconcile --db requests.db \
  --invoice daily-costs.csv \
  --invoice-scope day
```

The CSV needs one identity column and one USD amount column; both are named with
flags, so any provider's export can be read without a code change:

```csv
request_id,cost_usd
7,0.0031
8,0.0009
```

Amounts may carry `$` and thousands separators (`$1,234.56`); a blank cell, a
negative amount, a non-numeric value, a missing column, or a file with no line
items is a **runtime error (exit 1)**, never a skipped line and never a smaller
bill. Duplicate identity keys are summed, because one request can be split
across several SKUs (prompt, completion, cache).

Three rules keep the invoice honest:

1. **The invoice is the only billed side.** A selected row the invoice does not
   cover is `UNKNOWN`, not backfilled from `provider_cost_usd`. Mixing a
   response-reported cost into an invoice proof would hide which number is the
   invoice.
2. **Invoice lines outside the window are visible, not dropped.** They block a
   `PASS` unless `--invoice-allow-unmatched` is passed — the usual case is a
   wider invoice export than the `--since`/`--until` window.
3. **`--invoice-scope day` compares totals, not rows.** Providers that publish
   only a daily cost total cannot be joined per request, so the computed cost of
   every row in that UTC day is summed and compared with the invoice's line. A
   day containing an unpriced row keeps its total `UNKNOWN` rather than
   comparing a partial sum.

In day scope the invoice's identity column defaults to `date` and must hold
`YYYY-MM-DD`. Rows whose timestamp cannot be parsed are counted on their own line
and keep the run `INCOMPLETE`.

A zero-price row compares `$0.00` with `$0.00` and is a match. That is
arithmetic, not evidence of savings: a free row cannot prove that a paid
alternative would have cost more.

## Why a mismatch can be legitimate

The computed side prices the model at the rate in the sheet. On an aggregator
such as OpenRouter, one model id can be served by many upstream endpoints at
different per-token prices, and the gateway chooses the endpoint. The billed
cost is authoritative for the request that actually ran; the computed side is a
model-level estimate.

The live run on 2026-11-08 found two such rows on
`deepseek/deepseek-v4.1-flash`. Reconstructing each bill from the generation
record's `provider_name` and native token counts reproduces it exactly:

| Request | Served by | Prompt | Completion | Reconstructed bill |
| --- | --- | --- | --- | --- |
| 1313 | InferenceNet | 32 × `$0.103/M` | 8 × `$0.6/M` | `$0.000008096` |
| 1317 | DeepInfra | 89 × `$0.14/M` | 16 × `$0.42/M` | `$0.00001918` |

The sheet carried `$0.045/M` and `$0.5/M` for the model. The arithmetic was
right; the model-level rate was not the served endpoint's rate. The generation
records report `native_tokens_reasoning` equal to the completion count, so
reasoning tokens were already included, not missed.

Do not "fix" a mismatch by copying the bill into the sheet. That hides which
endpoint served the request and turns a price into a post-hoc rationalisation.
Use the bill as the actual side of a realised saving, keep the requested-side
counterfactual labelled as an estimate, and treat the model-level sheet rate as
an estimate rather than a quote for every endpoint.

## Status and exit codes

| Status | Exit | Meaning |
| --- | --- | --- |
| `PASS` | `0` | Every selected row has both numbers and every difference is within tolerance. |
| `FAIL` | `3` | At least one row has both numbers and the difference exceeds tolerance. A mismatch wins over any number of incomplete rows. |
| `INCOMPLETE` | `2` | Nothing mismatched, but at least one selected row lacks a computed or billed cost, or no row could be compared. |
| `ZERO_ROWS` | `2` | The database or the `--since`/`--until` window selected no metered rows. Nothing is printed on stdout; the reason is on stderr. |
| Runtime failure | `1` | Unreadable database, invalid date or tolerance, unset key with `--online`, or an unwritable `--out` path. The reason is on stderr. |

With `--online`, a failed lookup leaves that row without a lookup value. If any
lookup failed, the run cannot end in `PASS`, even when every row still carries a
stored provider cost: the independent verification did not happen. A selected
row without a `generation_id`, or with an id that is not an OpenRouter `gen-`
id, keeps its stored `provider_cost_usd` and is not looked up; `--online` adds a
provider record where one can be fetched, it does not erase a number the
provider already reported. Skipped ids are reported separately and do not by
themselves prevent a `PASS`.

With `--invoice`, the run cannot end in `PASS` unless every selected row is
covered by an invoice line and every invoice line matched a selected row (or
`--invoice-allow-unmatched` was passed). A run that used the invoice is
`INCOMPLETE` — never `PASS` — if any line is unattributed or any selected row
is not priced by it.

## Human-readable output

```text
rows: 3 selected of 3 metered in log
compared: 3 of 3
matched: 3
mismatched: 0
0 without a billed cost
0 without a computed cost
computed: $0.003200
billed: $0.003200
difference (billed - computed): $0.00
PASS
```

Passthrough rows (`route == "passthrough"`) are excluded before this summary:
the proxy writes them as count-only, never prices them, and counting them as
unpriced requests would swamp the rows that can be reconciled. When any were
excluded, the report says so:

```text
passthrough rows excluded: 1440 (count-only, never priced)
```

With `--invoice`, two more lines name the coverage before the totals:

```text
invoice: 12 line(s) by generation_id (row scope); 12 matched
invoice lines matching no selected row: 3
```

`computed`, `billed`, and `difference` are sums over the compared rows only.
Rows that are `UNKNOWN` are counted on their own lines and contribute nothing to
the totals.

## JSON summary (`--json`)

```json
{
  "billed_usd": 0.0032,
  "compared": 3,
  "computed_usd": 0.0032,
  "difference_usd": 0.0,
  "matched": 3,
  "mismatched": 0,
  "passthrough_excluded": 0,
  "rows": 3,
  "status": "PASS",
  "tolerance_usd": 1e-06,
  "without_billed": 0,
  "without_computed": 0
}
```

With `--online`, the object also carries
`"lookups": {"succeeded": 3, "failed": 0, "skipped": 0}`. `skipped` counts
selected rows whose `generation_id` is present but is not an OpenRouter `gen-`
id. The exit code follows the table above.

With `--invoice`, the object instead carries the invoice's coverage:

```json
"invoice": {
  "scope": "row",
  "join": "generation_id",
  "lines": 12,
  "duplicates": 0,
  "matched": 12,
  "unmatched": 3,
  "undated_rows": 0
}
```

`duplicates` counts invoice lines that were summed into another line under the
same identity. `undated_rows` is zero except in `day` scope, where it counts
rows the aggregate could not place on a UTC date.

## Artifact (`--out`)

`--out` writes a JSON array, one object per selected row:

```json
{
  "request_id": 7,
  "ts": "2026-10-26T06:44:19.694+00:00",
  "session_id_truncated": "auto-1a78",
  "model_requested": "gpt-4o",
  "model_used": "cohere/north-mini-code:free",
  "generation_id": "gen-abc",
  "tokens": {"prompt_tokens": 11, "completion_tokens": 7},
  "computed_cost": 0.003,
  "billed_cost": 0.004,
  "difference": 0.001,
  "matches": false,
  "billed_source": "logged",
  "logged_billed_cost": 0.004,
  "lookup_cost": null
}
```

`billed_source` is `"logged"`, `"lookup"`, `"invoice"`, or `null` when neither
number exists. In online mode a row with a successful lookup shows `"lookup"`,
with the stored number still visible as `logged_billed_cost`. With `--invoice`,
a matched row shows `"invoice"` and carries the invoice amount under
`"invoice_cost"`; a row the invoice does not cover shows `null` for both
`billed_cost` and `billed_source`, with the stored number still visible under
`logged_billed_cost` for inspection.

## Offline verification

Without `--online`, everything is computed from the local log. No socket is
opened, no provider is called, and no key variable is read. The arithmetic can
be checked independently by re-pricing the logged tokens against the sheet named
on the row. `--invoice` adds one local file read and still opens no socket and
reads no credential: the amounts come from the provider's export, not from an
API call tamias makes.

## Third-party check notes

1. Use the same price sheet Tamias used at run time. Each row records
   `price_sheet` and `price_sheet_date`.
2. Treat `$0.00` rows with care: a free model bills `$0`, which is
   inconclusive about savings.
3. Compare the **dollar** figure against the invoice, not the rupee display.
   `--inr-rate` on `tamias report` is a display conversion only.
4. Expect `UNKNOWN` cost for rows the sheet cannot price — that is honest
   metering, not a missing bill.

See [`docs/PRICING.md`](PRICING.md) for the price-sheet schema and how `UNKNOWN`
costs are handled.
