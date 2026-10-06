# How tamias Works (HOW-IT-WORKS)

## Request Lifecycle (ASCII diagram)

```text
+---------------------+          +---------------------+          +---------------------+
|  Agent / Client     |          |  tamias Proxy       |  Upstream|  OpenAI-compatible  |
|  (HTTP POST)        |--------->|  (FastAPI app)      |--------->|  Chat API           |
|  body: JSON         |          |                     |          |  usage, model, ...  |
|  headers: auth, ...|          +--------+----------+          +--------+--------------+
                                    |  ^  |                                  |
                                    |  |  |  for byte-for-byte forward    |
                                    v  |  v                                  v
+---------------------+          +---------------------+          +---------------------+
|  Router (decide)    |          |  Store (SQLite)     |          |  Response (relayed) |
|  decides STAY/SWITCH|          |  one row per req:   |          |  token counts, ...  |
|  target_model=None  |          |  session_id, model_ |          +-----------------------+
|  reason: string     |          |  requested, model_  |
+---------------------+          |  used, input, out,  |
                                  |  cached, cache_w,   |
                                  |  cost_usd, status,  |
                                  |  decision_action,   |
                                  |  decision_target_mo,|
                                  |  decision_reason)   |
                                  +---------------------+

                                  
## Module Responsibilities (real function names)

| Module                  | Key Function(s)                          | What It Does                                                                                                                                 |
|-------------------------|------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| **Router**              | `router.decide(body, state, config)`     | Pure function: reads body + session state, returns `Decision(action, target_model, reason)`. Never mutates body. Rules: user turn → STAY, tool error → STAY, easy tool after hysteresis → SWITCH, default → STAY. |
| **Proxy**               | `proxy.create_app(upstream, store, sheet, mode)` | Creates FastAPI app. Registers `/v1/chat/completions` and `/v1/matches` routes. In shadow mode: records decision, forwards body unchanged. In active mode: rewrites `model` on SWITCH. In off mode: disables router. |
| **Forward / Relay**     | `forward_headers(request)`, `outbound(raw, body, decision)` | Forwards headers hop-by-hop-free. `outbound()`: shadow returns raw bytes unchanged; active mode rewrites `model`; `--inject-usage` adds `stream_options.include_usage=true`. |
| **Store**               | `store.log_request(ts, session_id, ...)`   | Appends one row per request to SQLite. Columns: `ts, session_id, model_requested, model_used, input_tokens, output_tokens, cached_input_tokens, cache_write_tokens, cost_usd, price_sheet_date, latency_ms, status, decision_action, decision_target_model, decision_reason, price_sheet, price_simulated, provider_cost_usd, generation_id, project`. NULL = UNKNOWN, never 0. |
| **Pricing**             | `pricing.compute_cost(model, usage, sheet)` | Arithmetic: (uncached*input + cached*cached_input + 5m_writes*cache_write + 1h_writes*cache_write_1h + output*output) / 1_000_000. usd=None if model missing or needed field is None. A rate of 0 is free; omitted is UNKNOWN. |
| **CLI: `tamias serve`** | `cli.serve(args)`                         | Starts uvicorn on `--host` (default 127.0.0.1): `--upstream`, `--prices`, `--db`, `--port`, `--router-mode {shadow,active,off}`, `--cheap-model`, `--strong-model`, `--min-gap`, `--inject-usage`, `--verbose`. |
| **CLI: `tamias report`**| `cli.report(db_path, prices_path)`        | Reads SQLite log read-only. Prints: request count, total cost (KNOWN/UNKNOWN), realised saving (active SWITCH rows), hypothetical saving (shadow SWITCH rows), switch count, cheap model assumed, price sheet path/date. |
| **Session Derivation**  | `derive_session_id(headers, body, salt)`  | Hashes `authorization` + first system/developer message + first user message with per-process salt. Explicit `x-tamias-session` header wins. Only digest stored, never conversation text. |

## DB Columns Stored (exact schema)

```
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

## CLI Commands

| Command                   | Purpose                                                                                                    |
|---------------------------|------------------------------------------------------------------------------------------------------------|
| `tamias serve`            | Runs the proxy HTTP server. Required: `--upstream`, `--prices`, `--db`. Defaults: `--port 8000`, `--host 127.0.0.1`, `--router-mode shadow`, `--min-gap 3`. |
| `tamias report`           | Summarises a request log. Required: `--db`, `--prices`. Prints cost total, switch counts, saving estimates. |
| `tamias serve --help`     | Shows all serve flags.                                                                                     |
| `tamias report --help`    | Shows report usage.                                                                                        |

## Verified vs Not Verified

| Aspect                        | Status     | Notes                                                                                                                                     |
|-------------------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------|
| Body forwarded byte-identical | Verified   | `tests/test_proxy.py:test_body_forwarded_byte_identical` confirms raw body matches what upstream echoes back.                              |
| No prompt/response in DB      | Verified   | `tests/test_proxy.py:test_no_prompt_or_response_text_reaches_the_database_file` confirms no text column contains body content.             |
| UNKNOWN is never 0             | Verified   | `tests/test_e2e.py:test_cost_is_unknown_never_zero` and `CONTRACT.md` enforce NULL ≠ 0.                                                   |
| Anthropic adapter             | UNTESTED   | Wired into `tamias serve` but never run against real Anthropic API (mock-only in `tests/test_anthropic.py`).                               |
| Active mode end-to-end        | UNTESTED   | Unit-tested (`tests/test_proxy.py:test_active_mode_is_the_only_mode_that_rewrites_model`) but never against a real upstream.               |
| `--inject-usage` streaming    | PARTIAL    | Unit-tested with mock upstream; live behavior against OpenRouter noted in `docs/live-evidence.md` as volunteering usage without the flag. |
| Pricing arithmetic            | VERIFIED   | Unit-tested with hand-computed expected values (`tests/test_cli.py`).                                                                      |
| Router never mutates body     | Verified   | `tests/test_router.py:test_decide_does_not_mutate_body` and `test_decide_does_not_mutate_body_on_every_branch`.                            |
| Default bind host is 127.0.0.1| Verified   | `cli.py:389`: `default="127.0.0.1"`                                                                                                       |
| Authorization header never logged/stored | Verified | `LIVE_CHECK.md` check: "no page of the database contains the `ZEN_API_KEY` value"; `derive_session_id` drops it into session digest only. |
| Only `/v1/chat/completions` logged | Verified | `passthrough` catch-all relays other paths without writing rows (`proxy.py:502-517`).                                                     |
| Shadow mode never rewrites body | Verified | `tests/test_proxy.py:test_shadow_mode_never_changes_a_request_byte`                                                                        |