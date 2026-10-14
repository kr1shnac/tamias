# Tamias

Tamias is a local proxy for recording token usage and cost per request while
optionally applying rule-based model routing.

## Install

Public alpha from PyPI (Python 3.11 or newer):

```sh
pip install tamias
```

To work on tamias itself, install the checkout in editable mode:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Quickstart

Fetch a price sheet, check the local setup, then start the proxy:

```sh
tamias prices fetch --out prices.toml
tamias doctor --prices prices.toml
tamias serve --upstream https://your-provider.example --prices prices.toml --db requests.db
```

`tamias doctor` is offline by default. Add `--online` only when you want its
short, timed upstream reachability check. It warns for a missing
`OPENROUTER_API_KEY` without showing its value.

Run one agent command through a temporary local proxy with `tamias run`:

```sh
tamias run -- your-agent
```

`--prices` defaults to `./prices.toml` and then `~/.tamias/prices.toml`. If
neither file exists the run stops with exit code 2 and

```text
no price sheet found; run: tamias prices fetch --out ./prices.toml
```

`--upstream` defaults to `$TAMIAS_UPSTREAM` when that variable is set and to
`https://openrouter.ai/api` otherwise. Both flags still override the defaults:

```sh
tamias run --prices prices.toml --upstream https://your-provider.example -- your-agent
```

As with `tamias serve`, the upstream is a base URL without
`/v1/chat/completions`. `tamias run --help` prints the same defaults.

### Configuration file

Instead of repeating flags, put them in `~/.tamias/config.toml` (or the path in
`$TAMIAS_CONFIG`, or `--config PATH`):

```toml
provider = "openrouter"   # openrouter | openai | ollama
prices = "prices.toml"
db = "requests.db"
router_mode = "shadow"    # shadow | off
cheap_model = "some-cheap-model"
host = "127.0.0.1"
port = 8000
```

`provider` fills `upstream` from a built-in preset when no explicit URL is set.
Precedence is **CLI flag > environment > config file > built-in default**, and
the file holds no secrets — keep bearer tokens in `$TAMIAS_AUTH_TOKEN`. Inspect
what will be read:

```sh
tamias config path        # which file is used
tamias config show        # the values it holds
tamias config providers   # the built-in upstream presets
```

Provider presets also work directly on the command line:

```sh
tamias serve --provider openai --prices prices.toml --db requests.db
```

Use `tamias agent-config` to print a local configuration snippet for an agent:

```sh
tamias agent-config codex --port 8000 --project example
```

The `--project example` prefix scopes the local endpoint under `/p/example`.
`tamias run --project example` applies the same project prefix.

View a local run with:

```sh
tamias dashboard --db requests.db --prices prices.toml
```

`tamias serve` takes `--upstream`, `--prices`, and `--db` (each with an
environment or config-file fallback; see Configuration above). Its available
flags are `--provider`, `--port`, `--host`, `--router-mode`, `--router-profile`,
`--router-config`, `--cheap-model`, `--strong-model`, `--min-gap`,
`--effort-mode`, `--registry`, `--effort-style`, `--inject-usage`,
`--request-usage-cost`, `--auth-token`, `--keys-db`, `--tls-cert`, `--tls-key`,
`--max-body-bytes`, `--max-concurrency`, and `--verbose`.

Give `--tls-cert` and `--tls-key` together to serve HTTPS; the startup banner
then prints the `https://` URL. TLS is independent of `--auth-token`, which a
non-loopback bind still requires.

Binding to a non-loopback `--host` requires `--auth-token`: `serve` refuses to
start otherwise. The token is checked as a bearer credential in constant time,
and every response carries a `WWW-Authenticate: Bearer` header on a 401. The
three limits are also settable through the environment — `TAMIAS_AUTH_TOKEN`,
`TAMIAS_MAX_BODY_BYTES` (default 10 MiB, larger bodies are rejected with 413),
and `TAMIAS_MAX_CONCURRENCY` (default 64). The log database can be trimmed with
`tamias prune --db requests.db --keep-days 30` (`--keep-rows` and `--dry-run`
are also available); `tamias doctor` warns once the database passes 500 MB.
`tamias --version` prints the single version shared by the package and the
installed metadata.

`--keys-db` turns on **company keys**: per-caller tokens minted with
`tamias admin key issue` that carry a project and an optional model
allowlist. A key is checked before the request is forwarded or logged; an
unknown or revoked token gets 401, and a restricted key gets 403 for any
model it does not list. The key's project overrides a client-supplied
project header. Key auth is off unless `--keys-db` is given. This layer is
identity only - it does **not** cap spend; see [`docs/KEYS.md`](docs/KEYS.md).


Public alpha routing is limited to `shadow` and `off`; `active` routing is
disabled because it rewrites provider requests. The effort axis is separate
and off by default: `--effort-mode meter` records the effort the policy would
have chosen without changing the request, and `--effort-mode apply` sends it,
but only for a model listed in `--registry` (a capability catalogue); a level
a model does not list is downgraded to one it does. `--effort-style` chooses
OpenRouter's `reasoning.effort` field or OpenAI's `reasoning_effort` field.
The log records the requested setting; it does not prove the provider applied
it.

If an agent's tools return `Error: ...`, configure `error_markers`; see
[`examples/router.example.toml`](examples/router.example.toml).

## Status

- Metered: tokens and cost are recorded per request; cost is exact or UNKNOWN,
  never guessed.
- Routed: model switching is rule-based. Optional effort switching is fixture
  tested only; provider behaviour remains unverified.
- Tested on fixtures only: live provider behaviour and real Codex and Claude
  Code traffic are unverified.
- Evidence: dry runs on free models with simulated prices. The two arms did
  different amounts of work, so their totals are not comparable in either
  direction, and every dollar figure shown is hypothetical — the real billed
  cost of the free models is `$0`.

## Client matrix

Three client protocols are metered. Any other path is relayed unlogged.

| Client protocol | Route | Streaming | Buffered |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | metered | metered |
| Anthropic Messages | `POST /v1/messages` | metered | metered |
| OpenAI Responses | `POST /v1/responses` | metered | metered |
| Anything else | `/{path}` | relayed, not logged | relayed, not logged |

## Capability matrix

| Capability | Alpha state |
| --- | --- |
| Per-request token and cost log | Yes |
| Rule-based shadow routing | Yes — records decisions, does not rewrite |
| Active routing (request rewriting) | No — rejected by the CLI and library |
| Reasoning-effort switching | Metering only; provider behaviour unverified |
| Proxy authentication | Opt-in bearer token; required off loopback |
| Company keys (identity + model allowlist) | Yes — opt-in via `--keys-db`; not a spend cap |
| Per-key budget / spend cap | No — not implemented |
| Prompt / response text storage | No — never stored |
| Multi-user or remote hardening | Not supported — single user |

## What Tamias does not do

- It does not proxy arbitrary web traffic: only the three routes above are
  metered; everything else is passed through without a log row.
- It does not store prompt or response text, and it does not log credentials.
- It does not authenticate with the upstream provider; it forwards the
  credentials the client already sends.
- It does not rewrite provider requests in this alpha (`active` routing is
  disabled), so it cannot change which model a provider actually runs.
- It does not prove that a provider honoured a requested reasoning effort; the
  log records the request only.
- It does not enforce a spend cap. Company keys identify a caller and restrict
  which models it may reach, but nothing yet refuses a request because a budget
  is exhausted.


## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the frozen architecture and
  research design (v1.2, 2026-09-30). It is the spec; the code is an
  implementation of a subset of it, and
  [`docs/RELEASE-STATUS.md`](docs/RELEASE-STATUS.md) records which parts exist
  and which do not.
- [`docs/ROUTING.md`](docs/ROUTING.md) — the router's matcher, profiles and
  TOML keys.
- [`docs/PRICING.md`](docs/PRICING.md) — how token counts become a dollar
  figure, and when the answer is UNKNOWN.
- [`docs/EFFORT.md`](docs/EFFORT.md) — optional reasoning-effort switching.
- [`docs/KEYS.md`](docs/KEYS.md) — company keys: per-caller identity and model
  allowlists (identity only, not spend caps).
- [`docs/PLATFORMS.md`](docs/PLATFORMS.md) — supported Python and OS, and the
  Windows database-permission caveat.
- [`demo/RUNBOOK.md`](demo/RUNBOOK.md) — how to produce and read the two-arm
  demo, including its known limits.
- [`DEMO.md`](DEMO.md) — the demo script: three offline acts, exact commands,
  output captured on a real run.
