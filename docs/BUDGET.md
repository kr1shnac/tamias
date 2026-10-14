# Key limits: budgets, sub-caps and rates

`docs/KEYS.md` describes *who* is calling. This document describes *how much they
may spend, on what, and how fast* — the limits the gateway enforces on itself,
before a request reaches a provider.

The limits live in `src/tamias/budget.py` and are wired into the gateway by
`proxy.authorize`. A key carries any combination of four, each optional:

| Limit | Key field | CLI flag | Refusal |
| --- | --- | --- | --- |
| Total spend | `budget_usd`, `period_days` | `--budget-usd`, `--period-days` | 402 |
| Per-model spend | `model_caps` | `--model-cap MODEL=USD` | 402 |
| Requests per minute | `rpm` | `--rpm N` | 429 |
| Tokens per minute | `tpm` | `--tpm N` | 429 |

A key with none of them is completely unaffected: no reservation, no settlement,
no 403 for a model the price sheet cannot price. A key with any of them is
**limited**, and every limited key is metered on the three model-bearing routes
(`/v1/chat/completions`, `/v1/messages`, `/v1/responses`).

## Reserve, then settle

Money is never spent by a guess:

1. **Reserve.** Before the request is forwarded, the gateway reserves a fixed
   ceiling (global default `$0.25`, set with `--request-ceiling-usd`) against the
   key and against the named model. If the settled spend plus the outstanding
   reservations plus this ceiling would exceed a limit, the request is refused
   and never reaches the provider. The test is an inequality, not a rounding:
   `spent + reserved + ceiling <= limit`.
2. **Settle.** The one point where a metered request's real cost is known is its
   single `log_request` call. There the reservation is released and the *actual*
   cost is recorded. A budget is therefore spent by what was used, not by what
   was guessed.

Every settled request writes one row to `budget_events` (key, model, timestamp,
micro-dollars, tokens), so spend, sub-caps and both rate windows are aggregates
over one append-only table. Reservations are rows in `budget_reservations` in the
same SQLite file as the keys. Both tables are created on first use.

Two details are deliberate, and both came from asking what an *unmeasured*
request means:

- **An unknown cost is charged the ceiling.** When the price sheet cannot finish
  the arithmetic for a usage object — a partial usage, or cache rates the sheet
  does not quote — the request settles at the ceiling it reserved rather than at
  zero. A budget that reads an unmeasured request as free is not a ceiling.
- **An unreported token count is charged the estimate.** When the upstream
  reports no usage at all (a stream without `--inject-usage`), the reservation's
  estimate is recorded in place of a zero.

Money is held until the request settles or the reservation's TTL expires
(`DEFAULT_RESERVATION_TTL_SECONDS`, 1800 s). A reservation left behind by a
crashed or unfinished request stops counting after the TTL, and the key's next
request deletes it: the table is bounded and self-healing, and a leak cannot lock
a key out forever.

## What admission checks, in order

The first check that refuses wins:

1. the key's model allowlist (403, `docs/KEYS.md`);
2. for a key whose *money* limits are at stake, a named model that the price
   sheet cannot price (403) and a body that names no model at all (403);
3. the passthrough route, if its body names a model (403) — the gateway will not
   claim to bound what it neither prices nor logs;
4. `rpm`, then `tpm` (429);
5. the model's sub-cap, then the key's total budget (402).

Rate limits come first because they are retryable and distinct from having run
out of money. A 429 carries `Retry-After: 60`, the length of the window; a 402
carries the gateway's own arithmetic — spent, reserved, left, and what one more
request needs — so the caller and the operator can both check it.

## Rate windows

Both windows are rolling 60 seconds (`RATE_WINDOW_SECONDS`).

- **`rpm`** counts requests already logged *plus* requests still in flight, so a
  burst of concurrent requests cannot slip past it.
- **`tpm`** counts tokens already recorded *plus* the estimate held by each
  request in flight. The estimate is `len(body) / 4` rounded up, over the prompt
  bytes only. It is an estimate the reservation is settled against, not a
  measurement; if the upstream reports no usage, the estimate is what gets
  recorded.

## Honest limits

These are the things this design does not promise, stated here rather than
discovered later:

- **One request may overshoot.** The money reserve is a flat ceiling, so the
  request that exhausts a limit can push it past by `actual - ceiling`. The
  operator sets the ceiling to their largest plausible request.
- **A budget smaller than one ceiling admits nothing.** `spent + ceiling > limit`
  is refused, so a `$0.10` budget with a `$0.25` ceiling refuses every request.
  That is the honest reading of "you cannot afford a request this large".
- **Money limits need a priced model.** A model the sheet cannot price produces
  `UNKNOWN`, which cannot be spent against, so a limited key may not reach it.
- **A model-naming passthrough is refused.** The unlogged passthrough route is
  open only when its body names no model.
- **`tpm` can overshoot by output tokens.** Only the prompt is estimated at
  admission; the completion is counted at settlement.
- **An unmeasured cost is charged the ceiling**, so an operator whose sheet lacks
  a rate will see a budget move faster than their true spend until they quote it.
  That is the conservative direction on purpose.
- **Revocation does not cancel in flight.** It applies to new requests.
- **Unverified against a live provider.** The arithmetic and the refusals are
  tested against mocks; that a provider accepts a rewritten effort, or that a
  real price sheet's cache rates are complete, has not been measured. Company
  credits are consumed only through the gateway.

## Operating it

```bash
# a $50 monthly budget, $2 of it at most on the expensive model,
# 60 requests and 200k tokens a minute
tamias admin key issue --keys-db keys.db --label alice \
    --budget-usd 50 --period-days 30 \
    --model-cap 'anthropic/claude-sonnet-4=2.00' \
    --rpm 60 --tpm 200000

tamias admin key list --keys-db keys.db

tamias serve --keys-db keys.db --request-ceiling-usd 0.25
```

`budget_usd` without `period_days` is a lifetime ceiling: it sums every event the
key has ever written. With `period_days` it sums the events inside the window.

## The tests that pin this

- `tests/test_budget.py` — the ledger: parsing, reserve/settle, the flat ceiling,
  sub-caps, both rate windows, the TTL, and the conservative charge.
- `tests/test_gateway_budget.py` — the gateway: 402 and 429 on every metered
  route, refused requests never reaching the provider, settlement, the streaming
  path, the passthrough rules, and that an unlimited key is untouched.
- `tests/test_cli_admin.py` — the flags and what they print.
