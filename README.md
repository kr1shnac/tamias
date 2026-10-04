# tamias

A local HTTP proxy that sits between a coding agent and an OpenAI-compatible
chat API, and **logs the token counts and the cost of every request**.

It answers on `http://localhost:8000`, forwards what it receives, and appends one
row per request to a local SQLite file. It never stores prompt or response text
— the log schema has no column that could hold any.

**Status: `0.1.0a1`, pre-alpha.** See [What it does NOT do yet](#what-it-does-not-do-yet)
before you rely on it. No claim of any kind about saving money appears in this
README, because none has been measured.

## What it does

- Proxies `POST /v1/chat/completions` to an OpenAI-compatible upstream, byte for
  byte, including streaming responses relayed chunk by chunk.
- Logs one row per request: timestamp, session id, model requested, model used,
  input / output / cached-input / cache-write token counts, cost in USD, latency
  in ms, upstream status, and the routing decision.
- Costs each request from a TOML price sheet you supply, in USD per 1,000,000
  tokens, and records the arithmetic that produced the number.
- Runs a rule-based **shadow router** by default: it decides whether a request
  looks mechanical enough for a cheaper model, records that decision, and then
  forwards the request unchanged. Shadow mode cannot change what the upstream
  receives.
- Prints a summary with `tamias report`.

### The one rule that shapes everything: UNKNOWN is never zero

If the provider does not report a token count, or the model is missing from your
price sheet, tamias records **UNKNOWN** (SQL `NULL`). It does not record `0`.

This is deliberate. A provider that omits `usage` has not told you the request
was free, and writing `0` would silently understate your bill. So:

- a token count the provider did not send is `NULL`, not `0`;
- a rate you left out of the price sheet is `NULL`, not `0`;
- a cost that needs either is `NULL`, and `tamias report` prints `UNKNOWN`;
- a rate you set to `0` **is** a real price, so a genuinely free model costs
  exactly `$0.00`. Free is a price you stated; missing is a price you do not
  know. tamias keeps those two apart.

## What it does NOT do yet

Read this section before trusting any number tamias prints.

- **No measured savings. None.** Nothing here has been shown to reduce cost.
  `tamias report` prints a line labelled `estimated saving`, which is arithmetic
  over logged token counts and the price sheet, not a measurement. It ignores
  cache-rebuild cost, latency effects and quality regressions, and today it
  evaluates to `$0.00` for want of any priced request to subtract from (see the
  next point). Treat it as a placeholder for a number that does not exist yet.
- **Cost is UNKNOWN (never guessed as zero)** when a field needed to price the
  request is missing. Typical cases: the model is not in your price sheet; the
  provider returned no usage (for streaming requests the client must ask for it
  with `stream_options.include_usage`); the provider does not report cached
  tokens while your sheet prices cached input differently from normal input; or
  your sheet gives a model a nonzero cache-write price but the provider reports
  no cache writes. OpenAI-style APIs report no cache writes, so set
  `cache_write = 0` for those models in your price sheet and their requests are
  priced normally.
- **Active routing is implemented but not validated end to end.**
  `--router-mode active` does rewrite `model` on a SWITCH decision, and there is
  a unit test for it, but it has never been exercised against a real provider.
  **Use `--router-mode shadow` (the default).** Treat active mode as untested.
- **Only the chat and Messages paths are logged.** `/v1/responses` and every other
  path hit a catch-all relay: they are forwarded and the response is returned, but
  **no row is written and no cost is computed**. If your agent uses the
  Responses API, tamias is currently a transparent proxy and nothing more.
- **Anthropic support is experimental.** A Messages-API adapter
  (`src/tamias/anthropic_adapter.py`, `POST /v1/messages`) handles the
  `x-api-key` header and Anthropic's split `usage` fields, and `tamias serve`
  registers it alongside `/v1/chat/completions`. It has not been run against the
  real Anthropic API, so do not depend on it yet.
- **Free models cost `$0` only if you say so.** A model with no entry in your
  price sheet is UNKNOWN, not free. To record a genuinely free model, give it
  rates of `0`.
- **No per-conversation session tracking from OpenCode.** Rows are grouped by an
  `x-tamias-session` header if one arrives; otherwise they land in a single
  `default` bucket.
- **No cost control, budgets, rate limits or alerts.** It reports; it does not
  cap spend.

## Install

Requires Python 3.11 or newer. tamias is not on PyPI yet, so install from a
checkout:

```sh
git clone <this repository> && cd tamias
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

That provides the `tamias` command.

## Quickstart

### 1. Write a price sheet

Rates are **USD per 1,000,000 tokens**, with a top-level `date` recording when
those rates were published. Copy `prices.example.toml` as a starting point:

```toml
date = "2026-10-04"

[deepseek-v4-pro]
input = 1.74
output = 3.48
cached_input = 0.145
cache_write = 3.75

[deepseek-v4-flash]
input = 0.14
output = 0.28
cached_input = 0.028
cache_write = 3.75

# A model that really is free: every rate is a real 0.
[free-model]
input = 0.0
output = 0.0
cached_input = 0.0
cache_write = 0.0
```

Valid rate names are `input`, `output`, `cached_input`, `cache_write` and the
optional `cache_write_1h`. Anything else is rejected at startup, and a rate you
omit is left UNKNOWN on purpose.

### 2. Start the proxy

```sh
tamias serve \
  --upstream https://opencode.ai/zen \
  --prices  prices.toml \
  --db      requests.db \
  --port    8000 \
  --cheap-model deepseek-v4-flash \
  --strong-model deepseek-v4-pro
```

`--upstream` is the provider root; tamias appends `/v1/chat/completions` itself.
`--router-mode` defaults to `shadow`. Check it is listening:

```sh
curl -s localhost:8000/v1/models | head -c 200
```

### 3. Point your agent at it

Set your agent's OpenAI-compatible base URL to `http://localhost:8000/v1`.

**For OpenCode specifically, see [docs/OPENCODE.md](docs/OPENCODE.md)** — the
`opencode.json` provider block, the API key setup, which Zen models are
reachable through `/v1/chat/completions` and which are not, and a list of the
things in that guide that could not be verified without a real API key.

### 4. Read the log

```sh
tamias report --db requests.db --prices prices.toml
```

Against an OpenAI-style upstream, expect this shape — note `UNKNOWN`:

```
requests: 412
total cost: UNKNOWN (known part: $0.00; 412 of 412 requests have unknown cost)
requests shadow would have switched: 96
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured; 96 of 96 switched requests not priced)
cheap model assumed: deepseek-v4-flash
price sheet: prices.toml (2026-10-04)
```

The number worth watching in shadow mode is **requests shadow would have
switched** — how much of your traffic the router considers mechanical. It is
independent of the money arithmetic above.

## Commands

| Command | Purpose |
| --- | --- |
| `tamias serve` | Run the proxy in front of an upstream. |
| `tamias report` | Summarise request count, cost, shadow decisions and the estimate. |

Useful `serve` flags: `--router-mode {shadow,active,off}`, `--cheap-model`,
`--strong-model`, `--min-gap`, `--host`, `--port`, `--verbose`.

## Privacy

No prompt or response text is ever written to disk. The SQLite schema
(`src/tamias/store.py`) has no column that could hold any, and only metadata is
passed to it. Requests and responses are otherwise forwarded untouched.

## Development

```sh
pip install -e '.[dev]'
pytest
ruff check .
```

`tests/` never touches the network; upstream behaviour is exercised through
in-process ASGI transports.

## Further reading

- [CONTRACT.md](CONTRACT.md) — the behavioural contract this implementation is
  held to.
- [docs/OPENCODE.md](docs/OPENCODE.md) — OpenCode setup in full.
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE).