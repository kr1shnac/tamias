# Tamias

Tamias is a local HTTP proxy that logs token usage and cost per request for
OpenAI-compatible agents, and can rewrite model selection in **shadow** or
**active** mode.

[![PyPI](https://img.shields.io/pypi/v/tamias.svg)](https://pypi.org/project/tamias/)
[![Python versions](https://img.shields.io/pypi/pyversions/tamias.svg)](https://pypi.org/project/tamias/)
[![CI](https://github.com/kr1shnac/tamias/actions/workflows/ci.yml/badge.svg)](https://github.com/kr1shnac/tamias/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/kr1shnac/tamias/blob/master/LICENSE)

## What it does

- **Meters every request.** Writes one row per request — model, tokens, and
  computed cost — to a local SQLite file. A cost that cannot be computed is
  recorded as `UNKNOWN`, never guessed as `$0.00`.
- **Shadows routing decisions.** In the default `shadow` mode the router records
  which model it *would* pick, while the original request is forwarded untouched.
- **Can rewrite the model.** In opt-in `active` mode it sends the request to a
  cheaper model when a rule says that is safe. `active` is fail-closed.
- **Stays transparent.** It is a pass-through proxy: prompt and response text is
  never stored, and your provider credentials are forwarded as they are.

## Install

Published on PyPI as `tamias`:

```sh
pip install tamias
```

To confirm the command works in a fresh environment:

```sh
python -m venv /tmp/tamias-check
/tmp/tamias-check/bin/pip install tamias     # Windows: /tmp\tamias-check\Scripts\pip
/tmp/tamias-check/bin/tamias --version       # prints: tamias <version>
```

Requires **Python 3.11 or newer**. There is no service or database to install —
the log is a local SQLite file.

Signing company policy manifests needs the optional `manifest` extra:

```sh
pip install 'tamias[manifest]'
```

## Use it with OpenCode

OpenCode is the first-class client. It speaks `/v1/chat/completions`, the route
tamias meters, routes, and rewrites, and Zen publishes a set of free models you
can route mechanical work to.

```sh
pip install tamias
tamias prices zen --out prices.toml     # free Zen models priced at exactly $0; paid models stay UNKNOWN
tamias registry zen --out models.toml   # tools=true only where models.dev publishes tool_call
tamias serve \
  --provider opencode \
  --prices prices.toml \
  --db requests.db \
  --registry models.toml \
  --router-mode active \
  --min-gap 0 \
  --cheap-model deepseek-v4.1-flash-free
```

`--cheap-model` also takes a comma-separated pool
(`--cheap-model deepseek-v4.1-flash-free,big-pickle`). Each session stays on one
member and different sessions spread across the pool; `active` validates every
member against the registry and price sheet at startup.

Then point OpenCode at `http://127.0.0.1:8000/v1` (the exact `opencode.json`
block is in [`docs/OPENCODE.md`](docs/OPENCODE.md#4-point-opencode-at-the-proxy)),
install the slash command, and use OpenCode normally:

```sh
tamias opencode install --db requests.db --prices prices.toml \
  --registry models.toml --cheap-model deepseek-v4.1-flash-free
# restart OpenCode, then type: /tamias
```

`/tamias` shows requests, tokens, cost (or why it is `UNKNOWN`), how many
requests were routed, and the saving. The proxy runs in the background; the
foreground is just OpenCode. On Linux, `tamias service install ... --start`
writes and starts a systemd user unit, so the proxy comes back after login
without a terminal; see
[`docs/OPENCODE.md`](docs/OPENCODE.md#6-run-it-in-the-background).

**What routing can and cannot save.** A switch only saves money when the
requested model is paid and the target is free or cheaper. If you are already on
a free model, there is nothing to save — tamias will honestly report `$0.00`
saved and show you the token counts. The router moves mechanical turns (file
reads, shell lookups) and leaves planning and error recovery on the model you
chose.

For the paid models you care about, add their per-million-token rates to
`prices.toml` from Zen's pricing page; a rate you leave out stays `UNKNOWN`
rather than becoming a guessed number. `tamias report` always prints exact token
counts, so you have an accurate parameter even when the dollar figure is
unknown.

## Try it without credentials

`tamias demo` runs one request through the proxy against a bundled mock
upstream, writes a local SQLite log, and prints a receipt — zero keys, zero
network, zero config:

```sh
tamias demo
```

Re-run it as often as you like; each run writes `.tamias-demo/demo.db`.

The upstream is a base URL, **without** `/v1/chat/completions`. Then set the
agent's base URL — the one env var your client already respects:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1     # e.g. OpenAI-compatible clients
```

Run the agent, then read the log:

```sh
tamias report --db requests.db --prices prices.toml
```

Add `--inr-rate 85` to `report` to also show each amount in rupees at that fixed
rate — a display conversion, not a billed figure. The dollar amount stays
authoritative.

`--upstream` defaults to `$TAMIAS_UPSTREAM`, else to the `--provider` preset
(`openrouter`, `openai`, `opencode`, `ollama`), else to
`https://openrouter.ai/api`.
`--prices` defaults to `./prices.toml`, then `~/.tamias/prices.toml`, then a
dated OpenRouter snapshot bundled in the package. To run a single command
through a temporary proxy instead:

```sh
tamias run --prices prices.toml -- your-agent
```

To compare what Tamias computed against what your provider actually billed, run
`tamias reconcile` (see [`docs/RECONCILE.md`](docs/RECONCILE.md)).

## How it works

![Component architecture: proxy, router, upstream client, log, and adapters](https://raw.githubusercontent.com/kr1shnac/tamias/master/docs/diagrams/architecture.svg)

One request, end to end: the proxy parses it, the **routing gates** decide which
of the configured providers may serve the named model, the **router** records a
`STAY` or `SWITCH` decision, the **upstream client** forwards it (rewriting the
model only in `active` mode), and one row is written to the **SQLite log**. The
provider's reply is returned to the agent unchanged. The `.mmd` sources and
rendered SVG for every diagram live in
[`docs/diagrams/`](https://github.com/kr1shnac/tamias/tree/master/docs/diagrams).

Tamias sits **on top of** providers (OpenRouter, NVIDIA, OpenAI, Groq, a local
Ollama, anything OpenAI-compatible); it is not a provider itself. Adding one is
a config entry, and credentials stay where they already live — an environment
variable or a secret-manager command, never inside tamias. See
[`docs/PROVIDERS.md`](docs/PROVIDERS.md).

## Modes

| Mode | Behaviour |
| --- | --- |
| `off` | Forward unchanged; record no decision. |
| `shadow` (default) | Record the decision the router *would* make; forward unchanged (a requested model the provider has locked out still switches). |
| `active` | Rewrite the request to a model that passed the routing gates when the rule says `SWITCH`. Opt-in. |

`off` and `shadow` never change what the provider runs for cost reasons.
`active` is **fail-closed**: the process refuses to start unless the gates admit
at least one target that is listed in `--registry`, marked tool-capable there,
priced by the sheet, and served by a configured provider. A switch the caller's
key disallows is suppressed rather than refused. See
[`docs/ROUTING.md`](https://github.com/kr1shnac/tamias/blob/master/docs/ROUTING.md).

## Supported agents and routes

| Client protocol | Route | Streaming | Buffered |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | metered | metered |
| Anthropic Messages | `POST /v1/messages` | metered | metered |
| OpenAI Responses (Codex-style) | `POST /v1/responses` | metered, **no rewriting** | metered, **no rewriting** |
| Anything else | `/{path}` | count-only passthrough | count-only passthrough |

Tamias rewrites model selection only on `/v1/chat/completions` and
`/v1/messages`. `/v1/responses` is meter-only. Any other path is relayed
unpriced but still counted (one `route="passthrough"` row with no tokens and no
cost).

## Status and limitations

Tamias is **alpha** software. Treat every figure it prints with care:

- **Savings are estimates, not measurements.** No measured savings exist; every
  dollar figure is hypothetical. Any percentage you see is an illustration.
- **Live behaviour is only partly verified.** Routing and pricing are tested
  against fixtures; real provider and real Codex/Claude Code traffic are
  unverified.
- **Not multi-user or hardened for remote use.** Single user, loopback by
  default; a wider bind is a deliberate two-key act.
- **Prompt and response text is never stored**, and credentials are never logged.
- **The budget cap cannot be bypassed — and here is exactly what that means.**
  Tamias reserves against a key's budget *before* a request is forwarded, and
  refuses it with `402` when the request would exceed the limit, so the request
  never reaches the provider. Five qualifications travel with that sentence,
  and they matter as much as the sentence:
  1. **One request may overshoot.** The reserve is a flat ceiling, so the
     request that exhausts a limit can push it past by `settled - ceiling`.
  2. **A model the price sheet cannot price is refused** under a money limit,
     because an unknown cost cannot be spent against.
  3. **The cap holds on every metered route, and on the passthrough route for
     any body that names a model** — a budgeted key is refused `403` there —
     and the passthrough stays open only for bodies that name no model.
  4. **This is a guarantee about the cap, not about the proxy being
     unbypassable.** Tamias is a local process you own, its bearer token is
     opt-in, and you can point your agent straight at the provider instead.
  5. **The cap is enforced against the computed cost, not the bill.** The
     settled figure is the price-sheet computation; the provider's own reported
     cost is recorded separately and is never settled against, so a stale or
     missing rate makes the cap and the bill diverge. `tamias report` shows
     that divergence as `difference (billed - computed)` whenever both figures
     exist.
  See [`docs/BUDGET.md`](https://github.com/kr1shnac/tamias/blob/master/docs/BUDGET.md)
  for the reserve-then-settle detail.
- **Effort switching is metering only** unless `--effort-mode apply` is set, and
  the log records what was requested — not proof the provider honoured it.

See
[`docs/VERIFICATION.md`](https://github.com/kr1shnac/tamias/blob/master/docs/VERIFICATION.md)
for what has and has not been verified.

## Documentation

| Topic | Document |
| --- | --- |
| Full command-line reference | [`docs/CLI.md`](https://github.com/kr1shnac/tamias/blob/master/docs/CLI.md) |
| Architecture | [`docs/ARCHITECTURE.md`](https://github.com/kr1shnac/tamias/blob/master/docs/ARCHITECTURE.md) |
| Routing rules and modes | [`docs/ROUTING.md`](https://github.com/kr1shnac/tamias/blob/master/docs/ROUTING.md) |
| Multiple providers, discovery and limits | [`docs/PROVIDERS.md`](https://github.com/kr1shnac/tamias/blob/master/docs/PROVIDERS.md) |
| Pricing and `UNKNOWN` costs | [`docs/PRICING.md`](https://github.com/kr1shnac/tamias/blob/master/docs/PRICING.md) |
| Per-key budgets and rate limits | [`docs/BUDGET.md`](https://github.com/kr1shnac/tamias/blob/master/docs/BUDGET.md) |
| Reconcile computed vs billed cost | [`docs/RECONCILE.md`](https://github.com/kr1shnac/tamias/blob/master/docs/RECONCILE.md) |
| Company keys and model allowlists | [`docs/KEYS.md`](https://github.com/kr1shnac/tamias/blob/master/docs/KEYS.md) |
| Signed policy manifests | [`docs/MANIFEST.md`](https://github.com/kr1shnac/tamias/blob/master/docs/MANIFEST.md) |
| OpenCode integration | [`docs/OPENCODE.md`](https://github.com/kr1shnac/tamias/blob/master/docs/OPENCODE.md) |
| OpenRouter integration | [`docs/OPENROUTER.md`](https://github.com/kr1shnac/tamias/blob/master/docs/OPENROUTER.md) |
| Client libraries | [`docs/CLIENTS.md`](https://github.com/kr1shnac/tamias/blob/master/docs/CLIENTS.md) |
| Verification status | [`docs/VERIFICATION.md`](https://github.com/kr1shnac/tamias/blob/master/docs/VERIFICATION.md) |
| Example configs and price sheets | [`examples/`](https://github.com/kr1shnac/tamias/tree/master/examples) |

## Contributing, security and license

- **Contributing:** [`CONTRIBUTING.md`](https://github.com/kr1shnac/tamias/blob/master/CONTRIBUTING.md)
- **Security:** [`SECURITY.md`](https://github.com/kr1shnac/tamias/blob/master/SECURITY.md)
- **Support:** [`SUPPORT.md`](https://github.com/kr1shnac/tamias/blob/master/.github/SUPPORT.md)
- **Code of conduct:** [`CODE_OF_CONDUCT.md`](https://github.com/kr1shnac/tamias/blob/master/CODE_OF_CONDUCT.md)
- **Changelog:** [`CHANGELOG.md`](https://github.com/kr1shnac/tamias/blob/master/CHANGELOG.md)
- **License:** MIT — [`LICENSE`](https://github.com/kr1shnac/tamias/blob/master/LICENSE)
