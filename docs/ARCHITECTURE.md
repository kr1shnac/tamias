# Architecture

Tamias is a local, single-user, OpenAI-compatible HTTP proxy. An agent points
its base URL at tamias; tamias forwards each request to the upstream, logs what
the request cost, and (optionally) rewrites the model selection before
forwarding. The unit of intervention is **one HTTP request**: tamias sees only
that request's messages, keeps no turn orchestration, and never asks the agent's
harness to change.

```text
Agent / client  ──HTTP POST──▶  tamias :8000  ──same request──▶  upstream
                                  │   (FastAPI app)
                                  ├─ Router    decide → STAY / SWITCH
                                  ├─ Adapters  chat / messages / responses
                                  ├─ Store     one row per request (SQLite)
                                  └─ Relay     SSE chunk-by-chunk, no buffering
```

## Module responsibilities

| Module | Key entry points | What it does |
| --- | --- | --- |
| `router` | `decide(body, state, config)` | Pure function: reads the body and session state, returns a `Decision(action, target_model, reason)`. Never mutates the body. |
| `router_config` | `load_router_config(path)` | Parses the router TOML into a `RouterConfig`. Strict validation; a typo fails loudly. |
| `proxy` | `create_app(upstream, store, sheet, mode)` | Builds the FastAPI app, registers the three adapter routes plus the catch-all, and applies the routing mode. |
| `anthropic_adapter` | route handler | Serves `/v1/messages`: meters Anthropic-style usage, merges stream usage, and can rewrite the model. |
| `responses_adapter` | route handler | Serves `/v1/responses`: meters only, never rewrites a model. |
| `forward` / relay | `forward_headers`, `outbound` | Forwards headers hop-by-hop-free; shadow returns the raw bytes unchanged, active mode rewrites `model`, `--inject-usage` adds `stream_options.include_usage`. |
| `store` | `log_request(...)` | Appends one row per request to SQLite. |
| `pricing` | `compute_cost(model, usage, sheet)` | Arithmetic over the sheet; returns an exact figure or UNKNOWN, never a guess. |
| `effort` | `apply_effort` | Rewrites `reasoning.effort` / `reasoning_effort` on chat-completions bodies only. |
| `session` | `derive_session_id(headers, body, salt)` | Hashes `authorization` + first system/developer message + first user message with a per-process salt. Explicit `x-tamias-session` wins. Only the digest is stored. |
| `security` | `host_is_allowed`, `origin_is_allowed`, loopback policy | Refuses a wider bind without an explicit acknowledgement; cross-site guard against DNS rebinding. |
| `cli` | `serve`, `report`, `dashboard`, `doctor`, ... | Command line entry point. |

## Routes, wire protocols and coverage

`tamias serve --upstream` takes the provider **root**; the proxy appends the
route path. `TAMIAS_UPSTREAM_TIMEOUT_SECONDS` bounds every upstream request and
defaults to 600 seconds; invalid or non-positive values fall back to that
default.

| Route | Routed | Metered |
| --- | --- | --- |
| `POST /v1/chat/completions` | OpenAI-compatible upstream; shadow mode preserves the request body, active mode may rewrite its `model` | OpenAI `prompt_tokens`, `completion_tokens`, and cached prompt tokens. Stream usage is logged when the upstream sends it; absent usage is NULL. |
| `POST /v1/messages` | Anthropic Messages upstream; shadow mode preserves the request body, active mode may rewrite its `model` | `input_tokens`, `output_tokens`, cache creation, and cache read usage. Stream `message_start` and `message_delta` usage are merged. Cache-read/write cost is UNKNOWN unless the sheet quotes that rate. |
| `POST /v1/responses` | Always forwarded unchanged; this route never rewrites a model (meter-only) | Non-stream `usage.input_tokens`, `output_tokens`, and `input_tokens_details.cached_tokens`. Stream usage comes only from the final `response.completed` event. Reasoning tokens are already inside output tokens and are never added again. |
| Other paths | Catch-all byte relay | Count-only: **one `route="passthrough"` row per request**, token counts and cost NULL, never priced. |

Every upstream response retains `content-type`, `Retry-After` and `x-ratelimit-*`
headers; hop-by-hop headers are removed. A disconnected stream still writes one
row using the usage seen before the disconnect, with unseen usage NULL rather
than zero.

### Agent base URLs

These are route-derived proxy targets, not confirmed agent setup instructions.
This repository contains no Codex or Claude Code configuration that was
exercised against tamias.

| Agent | Proxy base URL | Status |
| --- | --- | --- |
| Codex using the OpenAI Responses API | `http://localhost:8000/v1` so the client reaches `/v1/responses` | Not verified: the route is fixture-tested, but no Codex traffic is confirmed here. |
| Claude Code using the Anthropic Messages API | `http://localhost:8000` so the client reaches `/v1/messages` | Not verified: the route is fixture-tested, but no Claude Code traffic is confirmed here. |

Keep credentials in the agent's normal configuration. Tamias forwards client
credentials upstream and provides no credential-setting flag.

## The request lifecycle

1. **Meter** — the adapter records tokens, cache read/write, latency and the
   routing decision into the per-key ledger. All three adapter routes meter;
   every other path is relayed by the catch-all, which writes one count-only row
   (`route="passthrough"`, tokens and cost NULL) so off-route traffic is visible
   but never priced or budget-gated.
2. **Classify** — rule-based heuristics on the **last message only** (trailing
   role, tool name class, error markers) produce a task class and a STAY/SWITCH
   decision. Hysteresis (`min_gap`) prevents ping-pong.
3. **Route** — when the mode is active, `router.applied_target` turns a decision
   into the model to run, subject to the fail-closed guards (see
   [`ROUTING.md`](ROUTING.md)).
4. **Forward** — the body goes to the provider; the SSE response is relayed
   chunk-by-chunk without buffering; upstream status codes pass through;
   `Authorization` (or `x-api-key`) is forwarded untouched.
5. **Report** — `tamias report` and the dashboard price the log cache-aware and
   can report a negative saving. This is arithmetic over logged counts, not a
   measurement of a bill.

## Database schema

```text
table: requests
  id INTEGER PRIMARY KEY AUTOINCREMENT
  ts TEXT NOT NULL
  session_id TEXT NOT NULL
  model_requested TEXT NOT NULL
  model_used TEXT NOT NULL
  input_tokens INTEGER         -- NULL = upstream did not report
  output_tokens INTEGER        -- NULL = upstream did not report
  cached_input_tokens INTEGER  -- NULL = upstream did not report
  cache_write_tokens INTEGER   -- NULL = upstream did not report
  cost_usd REAL                -- NULL = cost is UNKNOWN
  price_sheet_date TEXT NOT NULL
  latency_ms INTEGER
  status TEXT NOT NULL         -- e.g. "200", "500"
  decision_action TEXT NOT NULL -- "STAY" or "SWITCH"
  decision_target_model TEXT   -- NULL for STAY, model id for SWITCH
  decision_reason TEXT NOT NULL
  price_sheet TEXT              -- path of the sheet that priced the row
  price_simulated INTEGER       -- 1 when the sheet is a simulated one
  provider_cost_usd REAL        -- NULL when the provider reported no cost
  generation_id TEXT            -- provider generation, NULL when not reported
  project TEXT                  -- NULL when the run had no --project
```

`NULL` always means UNKNOWN, never zero. The only free-text column is
`decision_reason`, which holds a tool name (`bash`, `glob`, `read`), a
hysteresis counter, or a model id — never a request body, message, completion or
header.

## Platforms

Tamias is a local, single-user, loopback proxy, which is what makes the platform
matrix small: it only needs to run on the machine that runs your agent.

- **Python 3.11 or newer.** The wheel is pure Python (`py3-none-any`), so there
  is nothing to compile.
- Linux, macOS, WSL2 and (with caveats) Windows are supported; Linux is the
  primary CI target and macOS uses the same code path.

### Windows caveats

- **Database permissions are not enforced.** Tamias tightens the log database to
  owner-only with `os.chmod(0o600)`, which is a no-op on Windows. To keep the
  database private on Windows, place it in a directory whose NTFS ACLs restrict
  other users, or on an encrypted volume.
- **Loopback binding requires no administrator rights.** Binding to a
  non-loopback host requires `--auth-token` on every platform.
- Paths are handled with `pathlib`; no POSIX-only path logic is used.

### Networking

- The proxy binds `127.0.0.1` by default. Exposing it wider requires an explicit
  `--host` **and** `--auth-token`; `serve` refuses a non-loopback bind without a
  token.
- TLS is **not** provided by tamias natively except through the optional
  `--tls-cert`/`--tls-key` flags; otherwise terminate TLS in front of tamias (for
  example with a local reverse proxy).

## Scope, trust model and failure behaviour

**Non-goals.** Tamias does not change the agent harness, does not decompose or
merge turns (that would be a harness change), and does not compete with
provider-native routing. Subscription-billed agents (flat-fee plans) have no
per-call cost to rewrite; the addressable market is metered-API usage.
Guaranteeing output quality from inside the proxy is structurally impossible,
because the counterfactual — what a different model would have produced — is not
observable from inside the proxy.

**Trust model.** The real guarantee is privacy and availability, not savings or
quality: no prompt or response text is stored; the shared bearer token is
compared with `hmac.compare_digest`; loopback binding is **enforced, not merely
defaulted** (a wider bind is refused unless the operator passes an explicit
acknowledgement); upstream credentials are forwarded untouched so tamias never
persists them. The shared token carries no per-client identity — for that, use
company keys ([`KEYS.md`](KEYS.md)). Registry and price sheets are a
fleet-wide remote-config surface with no signing or pinning: a stale sheet
silently changes what a request costs and what the router does. A silent model
rewrite sends the transcript to a vendor the user did not name (privacy / ToS
consequence).

**Fail-closed and fail-open.** Routing to a cheaper model is fail-closed:
`serve` refuses to start in active mode unless the target is named by
`--cheap-model`, listed in `--registry`, marked tool-capable there, and priced
by the sheet — and a switch the caller's key does not allow is suppressed, not
applied. Every other internal error is **fail-open**: the original request is
forwarded unchanged and a warning is logged, so the agent never breaks. The
honest consequence of fail-open is that a fault converts into silently zero
savings; fail-open events should therefore be watched, not assumed absent.

## Cache semantics

Cache state is modelled and priced, never assumed.

### Event flags and normalized causes

Observed context invalidation is recorded as raw event flags, then optionally
normalised to a single primary cause — or `UNKNOWN` when the evidence does not
support one cause. Known raw categories include:

```text
MODEL_CHANGE  EFFORT_CHANGE  IDLE_EXPIRY  COMPACTION  TOOLSET_OR_MCP_CHANGE
PLUGIN_CHANGE  FAST_MODE_CHANGE  IMAGE_OR_CONTEXT_TRIM  RESUME_OR_SESSION_RESTORE
CLIENT_UPGRADE  AUTOMATIC_FALLBACK  SKILL_OR_COMMAND_MODEL_OVERRIDE
PROVIDER_CACHE_ANOMALY  UNKNOWN
```

Provider-specific causes may be added under `raw_cause_labels[]` without forcing
them into a false universal taxonomy. When multiple causes occur at once, retain
all observed flags; set a primary cause only when the evidence supports it,
otherwise `UNKNOWN`.

### No universal TTL model

Tamias must never assume that "previous request wrote 1h → current request has
1h TTL". Cache lifetime varies by provider, model, surface, client version,
effort mechanism, thinking configuration, feature flags, cache bucket,
compaction behaviour, and tool/system changes. Per-message effort can preserve
cache on some models while a top-level effort change can invalidate it.

### State sources (in order of preference)

1. observed token fields;
2. provider/client diagnostics;
3. versioned semantics registry;
4. controlled probe;
5. heuristic inference.

### First-class cache states

```text
WARM  COLD  REBUILT  UNKNOWN
```

`UNKNOWN` is returned whenever warm/cold/rebuilt status cannot be established
without unsupported assumptions.

### Rebuild-size estimation

Never equate `cache_write_tokens` with the rebuilt-prefix size unless the
provider's schema guarantees that for the exact event. Prefer reconstructing the
rebuilt prefix from prior cacheable context and observed cache reads; use the
cache-write field when the provider guarantees it means the rebuilt prefix;
otherwise the value is `UNKNOWN`. The product labels these explicitly as
*observed cache-write exposure*, *estimated rebuilt-prefix exposure*, or
*unknown*.

### Cache pricing

- When 5m and 1h cache-write fields are exposed, price them separately.
- Tamias separates `C_realized_request` (actual billed request cost) from
  `E_cache` (estimated incremental cache exposure relative to a warm reference):

  ```text
  E_cache = Σ_b [ R_b × (p_write_b − p_read_b) ]
  ```

  where `b` is a provider cache bucket, `R_b` the estimated rebuilt prefix in
  that bucket, and `p_write_b`/`p_read_b` the applicable write/read prices. If
  `R_b` is not identifiable, `E_cache = UNKNOWN`. This is accounting exposure,
  **not** a causal effect.

## What has and has not been verified

See [`VERIFICATION.md`](VERIFICATION.md) for what the test suite pins down, what
has been observed live, and what remains unverified.

## Where to read what

| Question | Read |
| --- | --- |
| Routing decisions, modes, profiles | [`ROUTING.md`](ROUTING.md) |
| Price sheets, cost arithmetic, provider cost | [`PRICING.md`](PRICING.md) |
| CLI, config precedence, env vars | [`CLI.md`](CLI.md) |
| Keys, budgets, manifests | [`KEYS.md`](KEYS.md), [`BUDGET.md`](BUDGET.md), [`MANIFEST.md`](MANIFEST.md) |
| Agent clients | [`CLIENTS.md`](CLIENTS.md), [`OPENCODE.md`](OPENCODE.md), [`OPENROUTER.md`](OPENROUTER.md) |
| Verification state | [`VERIFICATION.md`](VERIFICATION.md) |
