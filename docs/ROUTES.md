# Routes and agent configuration

`tamias serve --upstream` takes the provider root.  The proxy appends the route
path below.  `TAMIAS_UPSTREAM_TIMEOUT_SECONDS` bounds every upstream request;
it defaults to 600 seconds.  Invalid or non-positive values use that default.

| Route | Routed | Metered | Verification |
| --- | --- | --- | --- |
| `POST /v1/chat/completions` | OpenAI-compatible upstream; shadow mode preserves the request body, active mode may rewrite its model | OpenAI `prompt_tokens`, `completion_tokens`, and cached prompt tokens. Stream usage is logged when the upstream sends it; absent usage is NULL. | Real OpenRouter traffic is recorded in this repository's live evidence; the changes in this branch were fixture-tested only. |
| `POST /v1/responses` | OpenAI Responses upstream; the body is always forwarded unchanged and this route never rewrites a model | Non-stream `usage.input_tokens`, `output_tokens`, and `input_tokens_details.cached_tokens`. Stream usage comes only from the final `response.completed` event. Reasoning tokens are already included in output tokens and are never added again. | Fixtures only; buffered, completed-stream, and truncated-stream cases. |
| `POST /v1/messages` | Anthropic Messages upstream; shadow mode preserves the request body, active mode may rewrite its model | `input_tokens`, `output_tokens`, cache creation, and cache read usage. Stream `message_start` and `message_delta` usage are merged. Cache-read/write cost is UNKNOWN unless the sheet quotes that rate. | Fixtures only; no real Anthropic traffic is confirmed in this repository. |
| Other paths | Catch-all byte relay | Not metered | Fixture-tested as a relay only. |

Every upstream response retains `content-type`, `Retry-After`, and
`x-ratelimit-*` headers.  Hop-by-hop headers are removed.  A disconnected stream
still writes one row using the usage seen before the disconnect; unseen usage is
NULL rather than zero.

## Agent base URLs

The following are route-derived proxy targets, not confirmed agent setup
instructions.  This repository does not contain a Codex or Claude Code
configuration that was exercised against tamias.

| Agent | Proxy base URL to configure | Status |
| --- | --- | --- |
| Codex using the OpenAI Responses API | `http://localhost:8000/v1` so the client reaches `/v1/responses` | **UNVERIFIED** — the route is fixture-tested, but no Codex traffic is confirmed here. |
| Claude Code using the Anthropic Messages API | `http://localhost:8000` so the client reaches `/v1/messages` | **UNVERIFIED** — the route is fixture-tested, but no Claude Code traffic is confirmed here. |

Keep credentials in the agent's normal configuration.  Tamias forwards client
credentials upstream and does not provide a credential-setting flag.
