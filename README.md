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

## 60-second quickstart

Start the proxy in front of your provider, in the default **shadow** mode, then
point your agent's base URL at it.

```sh
tamias prices fetch --out prices.toml
tamias doctor --prices prices.toml
tamias serve --upstream https://your-provider.example \
             --prices prices.toml --db requests.db
```

The upstream is a base URL, **without** `/v1/chat/completions`. Then set the
agent's base URL — the one env var your client already respects:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1     # e.g. OpenAI-compatible clients
```

Run the agent, then read the log:

```sh
tamias report --db requests.db --prices prices.toml
tamias dashboard --db requests.db --prices prices.toml
```

`--upstream` defaults to `$TAMIAS_UPSTREAM`, else to the `--provider` preset
(`openrouter`, `openai`, `ollama`), else to `https://openrouter.ai/api`.
`--prices` defaults to `./prices.toml`, then `~/.tamias/prices.toml`, then a
dated OpenRouter snapshot bundled in the package. To run a single command
through a temporary proxy instead:

```sh
tamias run --prices prices.toml -- your-agent
```

## How it works

![Component architecture: proxy, router, upstream client, log, and adapters](https://raw.githubusercontent.com/kr1shnac/tamias/master/docs/diagrams/architecture.svg)

One request, end to end: the proxy parses it, the **router** records a `STAY` or
`SWITCH` decision, the **upstream client** forwards it (rewriting the model only
in `active` mode), and one row is written to the **SQLite log**. The provider's
reply is returned to the agent unchanged. The `.mmd` sources and rendered SVG
for every diagram live in
[`docs/diagrams/`](https://github.com/kr1shnac/tamias/tree/master/docs/diagrams).

## Modes

| Mode | Behaviour |
| --- | --- |
| `off` | Forward unchanged; record no decision. |
| `shadow` (default) | Record the decision the router *would* make; forward unchanged. |
| `active` | Rewrite the request to `--cheap-model` when the rule says `SWITCH`. Opt-in. |

`off` and `shadow` never change what the provider runs. `active` is **fail-closed**:
the process refuses to start unless the target model is named, listed as
tool-capable in `--registry`, and priced by the sheet. A switch the caller's key
disallows is suppressed rather than refused. See
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
| Pricing and `UNKNOWN` costs | [`docs/PRICING.md`](https://github.com/kr1shnac/tamias/blob/master/docs/PRICING.md) |
| Per-key budgets and rate limits | [`docs/BUDGET.md`](https://github.com/kr1shnac/tamias/blob/master/docs/BUDGET.md) |
| Company keys and model allowlists | [`docs/KEYS.md`](https://github.com/kr1shnac/tamias/blob/master/docs/KEYS.md) |
| Signed policy manifests | [`docs/MANIFEST.md`](https://github.com/kr1shnac/tamias/blob/master/docs/MANIFEST.md) |
| Local web dashboard | [`docs/DASHBOARD.md`](https://github.com/kr1shnac/tamias/blob/master/docs/DASHBOARD.md) |
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
