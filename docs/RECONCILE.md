# `tamias reconcile`

Reconcile logged rows against the provider's billed cost per request — the
difference between what Tamias computed from list prices and what the provider
actually billed. The reconciled receipt is the product; this command emits one
report per log.

```sh
tamias reconcile --db requests.db --prices prices.toml
```

## Flags

| Flag | Required | Meaning |
| --- | --- | --- |
| `--db PATH` | yes | Path to the SQLite request log produced by `tamias run` / `tamias serve`. |
| `--prices PATH` | no | TOML price sheet used to price the log. Defaults to the same resolution as `report`. |
| `--api-key-env NAME` | no | Environment variable holding the provider API key (default `OPENROUTER_API_KEY`). Only the name is read; the secret never enters argv. |
| `--out PATH` | no | Write a text-free JSON artifact to this path: one row per logged request with request id, timestamp, models, tokens, computed cost, billed cost, and difference. |
| `--since YYYY-MM-DD` | no | Lower bound on row timestamp (inclusive). |
| `--until YYYY-MM-DD` | no | Upper bound on row timestamp (inclusive). |
| `--tolerance USD` | no | Absolute USD tolerance used when comparing computed vs billed cost. Rows within tolerance are treated as matching. |
| `--json` | no | Emit a compact JSON summary on stdout instead of the human-readable report. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Reconciliation complete (rows printed or artifact written). |
| `1` | Runtime failure — unreadable database, bad price sheet, etc.; reason on stderr. |
| `2` | `ZERO_ROWS` — no rows with a `generation_id` were found. Run `tamias reconcile` after requests that recorded a `generation_id` (each metered row carries one). |

## Report columns

For each reconciled row the report shows:

- `request_id` / `id` — the row id in the log
- `ts` — request timestamp
- `model_requested` — model the agent asked for
- `model_used` — model the proxy actually sent to the provider
- `tokens` — prompt / completion token counts
- `computed_cost` — cost Tamias computed from the price sheet (USD)
- `billed_cost` — provider-reported cost, when present (`provider_cost_usd`)
- `difference` — billed minus computed (USD)

Rows without tokens or without a priced model report `UNKNOWN` cost rather than
guessing `$0.00`.

## Artifact (`--out`)

`--out` writes a text-free JSON array so third parties can verify it without
parsing human output. Each entry:

```json
{
  "request_id": 7,
  "ts": "2026-10-26T06:44:19.694+00:00",
  "model_requested": "gpt-4o",
  "model_used": "cohere/north-mini-code:free",
  "tokens": {"prompt_tokens": 11, "completion_tokens": 7},
  "computed_cost": 0.003,
  "billed_cost": 0.004,
  "difference": 0.001,
  "session_id_truncated": "auto-1a78"
}
```

## Offline verification

Everything this command prints is computed from the local log and the price
sheet you supply. No provider is called, no API key is transmitted, and the
`--api-key-env` flag only records *which* variable holds the key — the value is
never read by Tamias. You can verify the arithmetic yourself by re-pricing the
logged tokens against the same sheet.

`tamias report --db ... --prices ...` already prints `difference (billed -
computed)` whenever both figures exist; `tamias reconcile` makes that comparison
explicit, per-row, and machine-readable via `--out`.

## Third-party check notes

A reviewer reconciling Tamias against a provider invoice should:

1. Use the same price sheet Tamias used at run time (the sheet path is recorded
   on each row as `price_sheet`, and `price_sheet_date` is stored too).
2. Treat `$0.00` rows with care: free models bill `$0`, which is *inconclusive*
   about savings — never claim savings from a free row alone.
3. Compare the **dollar** figure against the invoice, not the rupee display.
   `--inr-rate` is a display conversion only; the dollar amount is authoritative
   and is the one that reconciles against a provider invoice.
4. Expect `UNKNOWN` cost for rows the sheet cannot price — that is honest
   metering, not a missing bill.

See [`docs/PRICING.md`](PRICING.md) for the price-sheet schema and how
`UNKNOWN` costs are handled.
