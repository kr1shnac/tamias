# OpenRouter billed-cost accounting

This note records only what the linked OpenRouter documentation confirms. It
does not make a generation request.

## Per-generation lookup

- URL: `GET https://openrouter.ai/api/v1/generation?id=<generation-id>` ([Get
  request & usage metadata for a generation](https://openrouter.ai/docs/api/api-reference/generations/get-generation)).
- Authentication: `Authorization: Bearer <token>`.
- Lookup response: the documented payload is `data`; its identifier is
  `data.id`, and its documented billed total is `data.total_cost` (USD). The
  example also contains `data.usage`, with the same numeric value in that
  example. The documentation does **not** call either field "billed";
  Tamias therefore calls this value *reported by OpenRouter*, not an estimate.
- The lookup requires the generation ID as query parameter `id`.

## Credits and current-key accounting

- Credits URL: `GET https://openrouter.ai/api/v1/credits` ([Get remaining
  credits](https://openrouter.ai/docs/api/api-reference/credits/get-credits)).
  Its documented response fields are `data.total_credits` and
  `data.total_usage`; the page says a management key is required.
- Current-key URL: `GET https://openrouter.ai/api/v1/key` ([Get current API
  key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key)).
  Its documented response fields include `data.usage`, `data.usage_daily`,
  `data.usage_weekly`, `data.usage_monthly`, and corresponding BYOK fields.
- Both endpoints document bearer-token authentication. Neither document says
  that a normal request key is sufficient for the credits endpoint.

## Chat-completions response and streams

- Whether text chat/completions returns `usage.cost` by default: **UNVERIFIED**
  by the OpenRouter pages reviewed here.
- Whether a request must specify `usage.include`: **UNVERIFIED**. The reviewed
  pages do not document such a parameter for text chat/completions.
- Whether the final text-stream chunk carries a cost field: **UNVERIFIED**.
  (The separate image-generation guide documents `usage.cost` in its completed
  SSE event; that does not establish the text-chat behavior.)
- Where a text-chat generation ID appears: **UNVERIFIED**. The reviewed
  generation lookup page confirms the ID format/value needed for lookup but
  does not document where a text completion exposes it. A separate OpenRouter
  TTS guide documents an `X-Generation-Id` response header; this is not
  evidence for chat-completions.

## Implementation consequence

When a stored OpenRouter generation ID exists, Tamias can reconcile its stored
provider-reported cost against `data.total_cost` from the documented lookup
endpoint. Until the UNVERIFIED chat response details are confirmed, Tamias must
not claim that absent per-response cost is zero or substitute a list-price
calculation for billed cost.
