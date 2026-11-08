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
| `--online` | no | Read the provider's generation record for every selected row with a `generation_id` and use that `total_cost` as the billed side. |
| `--api-key-env NAME` | no | Environment variable holding the lookup key (default `OPENROUTER_API_KEY`). Only read with `--online`; the value is never printed, logged, or placed in argv. |
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
  existing generation; it never creates a completion. The stored
  `provider_cost_usd` is retained in the artifact as `logged_billed_cost`, and
  the lookup value is `lookup_cost`.

A zero-price row compares `$0.00` with `$0.00` and is a match. That is
arithmetic, not evidence of savings: a free row cannot prove that a paid
alternative would have cost more.

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
row without a `generation_id` keeps its stored `provider_cost_usd` and is not
looked up; `--online` adds a provider record where one can be fetched, it does
not erase a number the provider already reported.

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
`"lookups": {"succeeded": 3, "failed": 0}`. The exit code follows the table
above.

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

`billed_source` is `"logged"`, `"lookup"`, or `null` when neither number exists.
In online mode a row with a successful lookup shows `"lookup"`, with the stored
number still visible as `logged_billed_cost`.

## Offline verification

Without `--online`, everything is computed from the local log. No socket is
opened, no provider is called, and no key variable is read. The arithmetic can
be checked independently by re-pricing the logged tokens against the sheet named
on the row.

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
