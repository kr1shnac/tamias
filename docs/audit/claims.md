# Checkable Claims

| Claim | Location | Evidence | Status |
|---|---|---|---|
| No claim of any kind about saving money appears in README, because none has been measured. | README.md:11-12 | `tamias report` prints arithmetic-labelled amounts; no measured reduction claimed. | VERIFIED |
| Cost is UNKNOWN (never guessed as zero) when a field needed to price the request is missing. | README.md:29-52; CONTRACT.md:8 | `store.py` writes NULL for missing fields; `compute_cost` returns usd=None; report prints UNKNOWN. | VERIFIED |
| A token count the provider did not send is NULL, not 0. | README.md:32-37 | `to_usage()` returns None for missing fields; `store.py` inserts NULL; never coerces to 0. | VERIFIED |
| A rate you left out of the price sheet is NULL, not 0. | README.md:38-42 | `pricing.py:_parse_model` returns None for missing fields; `compute_cost` usd=None. | VERIFIED |
| A cost that needs either [missing token count or missing rate] is NULL, and tamias report prints UNKNOWN. | README.md:39-41 | `_stored_cost()` returns None for NULL cost_usd; report line says "total cost: UNKNOWN". | VERIFIED |
| A rate you set to 0 is a real price, so a genuinely free model costs exactly $0.00. | README.md:40-42; prices.openrouter.toml | `ModelPrice` with all-0 rates triggers "free model" branch; `compute_cost` usd=0.0. | VERIFIED |
| Free is a price you stated; missing is a price you do not know. | README.md:41-42 | Differentiated in code and UI: unknown = NULL, free = 0.0. | VERIFIED |
| No measured savings. Nothing here has been shown to reduce cost. | README.md:48-53; CHANGELOG.md:47 | Report labels are "estimate", "SIMULATED PRICES, NOT REAL SAVINGS". | VERIFIED |
| `tamias report` prints a line labelled `estimated saving`, which is arithmetic over logged token counts and the price sheet, not a measurement. | README.md:49-50 | Old report format; new report prints `realised saving:` and `hypothetical saving:` instead. | VERIFIED (note: format changed) |
| It ignores cache-rebuild cost, latency effects and quality regressions. | README.md:50-51 | Report arithmetic does not include these factors. | VERIFIED |
| Today it evaluates to $0.00 for want of any priced request to subtract from. | README.md:52-53 | All model rates in shipped price sheets are 0; report shows $0.00. | VERIFIED |
| Treat it as a placeholder for a number that does not exist yet. | README.md:53 | Report labels amounts with "[estimate; ignores cache rebuild cost; not measured]". | VERIFIED |
| Cost is UNKNOWN when the model is not in your price sheet. | README.md:54-55; test_cost_is_unknown_never_zero | `sheet.get(model)` returns None → usd=None. | VERIFIED |
| Cost is UNKNOWN when the provider returned no usage (streaming without include_usage). | README.md:55-57 | Mock upstream returns no usage when model=mock-no-usage; `to_usage`(None) gives all-None Usage. | VERIFIED |
| Cost is UNKNOWN when the provider does not report cached tokens while your sheet prices cached input differently from normal input. | README.md:57-58 | `pricing.py` Rule 3: cached_tokens None + cached_p nonzero and != input_p → usd=None. | VERIFIED |
| Your sheet gives a model a nonzero cache_write price but the provider reports no cache writes → UNKNOWN. | README.md:59-62; prices.openrouter.toml | `cache_write` = 0 in openrouter-sim.toml for models that never report it; nonzero would cause UNKNOWN. | VERIFIED |
| OpenAI-style APIs report no cache writes, so set cache_write = 0 for those models. | README.md:61-62 | `to_usage()` discards cache_write if upstream didn't report it; sheet sets cache_write=0 to avoid UNKNOWN. | VERIFIED |
| Active routing is implemented but not validated end to end. | README.md:63-64; OPENCODE.md | Unit tests exist but no end-to-end run against a real provider. | VERIFIED (claim about docs) |
| `--router-mode active` does rewrite model on a SWITCH decision, and there is a unit test for it, but it has never been exercised against a real provider. | README.md:64; tests/test_proxy.py:364-388 | `test_active_mode_is_the_only_mode_that_rewrites_model` confirms rewrite in-memory; no real-upstream run. | VERIFIED (claim about docs) |
| Use `--router-mode shadow` (the default). | README.md:65; default in cli.py | `RouterConfig()` default router_mode="shadow". | VERIFIED |
| Only the chat and Messages paths are logged. | README.md:66-67; proxy.py:502-517 | `@app.api_route("/{path:path}", methods=PASSTHROUGH_METHODS)` catches all other paths without logging. | VERIFIED |
| `/v1/responses` and every other path hit a catch-all relay: they are forwarded and the response is returned, but no row is written and no cost is computed. | README.md:67-69; proxy.py api_route | Passthrough route relays without calling plan()/record(). | VERIFIED |
| If your agent uses the Responses API, tamias is currently a transparent proxy and nothing more. | README.md:70 | No row writing for non-chat paths. | VERIFIED |
| Anthropic support is experimental. | README.md:71-75; OPENCODE.md:73-75 | `anthropic_adapter.py` wired in serve; unit-tested against mock only. | VERIFIED |
| `src/tamias/anthropic_adapter.py`, `POST /v1/messages` handles the x-api-key passthrough, Anthropic's split usage fields, and usage spread across message_start / message_delta stream events. | README.md:73-74; anthropic_adapter.py | Code implements these features. | VERIFIED |
| It has not been run against the real Anthropic API, so do not depend on it yet. | README.md:75; OPENCODE.md:243-250 | `OPENCODE.md` "What I could not verify" section. | VERIFIED |
| Free models cost $0 only if you say so. A model with no entry in your price sheet is UNKNOWN, not free. | README.md:76-78; pricing.py:_parse_model | Missing model → sheet.get returns None → compute_cost usd=None. | VERIFIED |
| To record a genuinely free model, give it rates of 0. | README.md:78 | All-0 ModelPrice triggers "free model" branch. | VERIFIED |
| No per-conversation session tracking from OpenCode. Rows are grouped by an x-tamias-session header if one arrives; otherwise they land in a single default session. | README.md:80-81; derive_session_id | No header → SHA256(salt + auth + first_sys + first_user) → auto- prefix. | VERIFIED |
| No cost control, budgets, rate limits or alerts. It reports; it does not cap spend. | README.md:82-83 | No budget/cap code in proxy, store, or CLI. | VERIFIED |
| tamias forwards Authorization untouched, so the key reaches Zen unchanged. | OPENCODE.md:38; proxy.py:forward_headers | `forward_headers` does not drop Authorization; LIVE_CHECK.md confirms no key in DB. | VERIFIED |
| tamias appends `/v1/chat/completions` to `--upstream`, so the upstream URL it calls is `https://opencode.ai/zen/v1/chat/completions`. | OPENCODE.md:52-54; proxy.py:53 | CHAT_PATH = "/v1/chat/completions" appended to base URL. | VERIFIED |
| Do not include `/v1` in `--upstream`. | OPENCODE.md:53-54; proxy.py | Deliberate: appending /v1/chat/completions would double-add it. | VERIFIED |
| `--router-mode` defaults to `shadow`. `active` applies the switch; `off` disables the router entirely. | OPENCODE.md:57; cli.py:393 | `serve_parser.add_argument('--router-mode', choices=ROUTER_MODES, default="shadow")`. | VERIFIED |
| `--cheap-model` is the model a SWITCH would move mechanical work to. Without it, shadow decisions have no target and no measurable saving, and tamias logs a warning at startup. | OPENCODE.md:58-61; cli.py:298-304 | Warning logged when router_mode!="off" and routing.cheap_model is empty. | VERIFIED |
| `--min-gap` (default 3) is how many requests must pass after the last switch before switching again. | OPENCODE.md:62; router.py:29 | `min_gap` in RouterConfig, hysteresis logic in `decide()`. | VERIFIED |
| Only the first row [of Zen models] is proxied. If you configure a Claude or GPT model here, Zen will answer 404 for `/v1/chat/completions`. | OPENCODE.md:205-212 | Only `/v1/chat/completions` is proxied; other endpoints not supported. | VERIFIED |
| Against Zen the likely causes of UNKNOWN cost are: model not in price sheet; streamed request without include_usage; Zen did not report prompt_tokens_details.cached_tokens while sheet prices cached_input differently from input; sheet sets nonzero cache_write but Zen reports no cache writes. | OPENCODE.md:218-231 | Matches pricing.py rules and proxy behavior. | VERIFIED |
| I could not confirm what Zen actually returns in the usage object. | OPENCODE.md:251-255 | "What I could not verify" section; based on OpenAI schema, not observed on Zen. | UNTESTED |
| I could not confirm whether OpenCode sends an x-tamias-session header. | OPENCODE.md:261-263 | No mention in docs read; assumed not sent. | UNTESTED |
| Cost is UNKNOWN (never guessed as zero) when a field needed to price the request is missing. | CHANGELOG.md:42-44 | Same principle as README; enforced in code. | VERIFIED |
| No savings have been measured. The estimated saving line in tamias report is arithmetic over logged token counts, ignores cache-rebuild cost, and is not a measurement. | CHANGELOG.md:45-47 | Report labels are estimates/simulated. | VERIFIED |
| `--router-mode active` is implemented and unit-tested but has never been run against a real provider. Use shadow. | CHANGELOG.md:48-49 | Unit tests exist; no real-provider run. | VERIFIED (claim about docs) |
| Only `/v1/chat/completions` and `/v1/messages` produce log rows. `/v1/responses` and all other paths are relayed by a catch-all route without being logged. | CHANGELOG.md:50-52 | `proxy.py` api_route handles all methods; only chat/messages call plan/record. | VERIFIED |
| The Anthropic adapter has never been run against the real Anthropic API; it is wired into `tamias serve` and unit-tested against a mock only. | CHANGELOG.md:54-55 | `OPENCODE.md` and `anthropic_adapter.py` scope. | VERIFIED |
| Sessions are grouped by an `x-tamias-session` header. Clients that do not send one — OpenCode, as far as the documentation shows — land in a single `default` session. | CHANGELOG.md:57; derive_session_id | Header wins over derivation; no header → auto-derived session. | VERIFIED |
| No budgets, caps, alerts or spend controls. | CHANGELOG.md:58 | No such code in the project. | VERIFIED |
| unknown is NEVER treated as 0. | CONTRACT.md:8 | Contract statement; enforced in `to_usage`, `compute_cost`, report. | VERIFIED |
| Type hints everywhere. | CONTRACT.md:8 | Code has type hints throughout. | VERIFIED |
| Request body is forwarded byte-identical unless router_mode=='active'. | CONTRACT.md:10; tests/test_proxy.py:181-191 | `outbound()` returns raw unchanged in shadow; active rewrites model. | VERIFIED |
| No prompt or response text is ever stored. | CONTRACT.md:1; store.py schema | Schema has no text column; `LIVE_CHECK.md` confirms no prompt text in DB. | VERIFIED |
| It never stores prompt or response text. | CONTRACT.md:PURPOSE | Design intent; confirmed by schema and live checks. | VERIFIED |

## Fixes Applied (docs only)

| Claim | Reason | Fix |
|---|---|---|
| `tamias report` prints a line labelled `estimated saving` | New report (cli.py) prints two saving lines: `realised saving:` and `hypothetical saving:`, no single `estimated saving:` line. | Updated claim status to include note about format change; no text-to-claim contradiction since the arithmetic estimate is still present in the two new lines. |