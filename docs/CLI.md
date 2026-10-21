# Command line

Every command runs as `tamias <command>`. `tamias --help` lists them:

```text
{prices,serve,run,report,dashboard,agent-config,doctor,prune,admin,config}
```

| Command | Purpose |
| --- | --- |
| `serve` | Run the proxy in front of an upstream. |
| `run` | Run one command through a short-lived loopback proxy, then print its report. |
| `report` | Summarise cost and shadow routing from a log. |
| `dashboard` | Serve the local run dashboard. |
| `agent-config` | Print an agent configuration snippet. |
| `doctor` | Check local tamias readiness. |
| `prune` | Delete old rows from the request log. |
| `admin` | Manage company keys and signed project manifests. |
| `prices fetch` | Fetch an OpenRouter model price sheet. |
| `config` | Show the effective configuration. |

## Configuration precedence

For every value, precedence is **CLI flag > environment variable > config file >
built-in default**.

The config file is `$TAMIAS_CONFIG` when set, else `~/.tamias/config.toml`. A
missing default file is not an error; a file named explicitly with `--config`
must exist. Secrets (auth tokens) are deliberately **not** read from the file —
keep them in the environment.

The file accepts these keys: `upstream`, `provider`, `prices`, `db`,
`router_mode`, `cheap_model`, `strong_model`, `effort_style`, `effort_mode`,
`host`, `port`, `min_gap`, `inject_usage`. An unknown key is a startup error.
`provider` selects a preset upstream (`ollama`, `openai`, `openrouter`); a named
`--upstream` overrides it.

```sh
tamias config path        # which file would be read
tamias config show        # values loaded from it
tamias config providers   # built-in upstream presets
```

## Environment variables

| Variable | Effect |
| --- | --- |
| `TAMIAS_CONFIG` | Config file path (else `~/.tamias/config.toml`). |
| `TAMIAS_UPSTREAM` | Default upstream when neither `--upstream` nor `--provider` is given. |
| `TAMIAS_AUTH_TOKEN` | Bearer token clients must send; required for a non-loopback bind unless `--keys-db` is set. |
| `TAMIAS_AUTH_TOKEN_FILE` | Read the auth token from a file instead of putting it in argv. |
| `TAMIAS_REGISTRY` | Capability catalogue for `--effort-mode apply` (else `~/.tamias/models.toml`). |
| `TAMIAS_KEYS_DB` | Default company-key store path (used by `admin`; `serve` needs `--keys-db`). |
| `TAMIAS_MAX_BODY_BYTES` | Override the request body cap (default `10485760`; `0` disables). |
| `TAMIAS_MAX_CONCURRENCY` | Override the in-flight request limit (default `64`; `0` disables). |
| `TAMIAS_UPSTREAM_TIMEOUT_SECONDS` | Bound every upstream request (default `600`). |

`tamias doctor` also reports whether `OPENROUTER_API_KEY` is set.

## Price-sheet resolution

The sheet a command prices with is resolved in this order:

1. an explicit `--prices PATH` (a named file that does not exist is an error);
2. `./prices.toml`;
3. `~/.tamias/prices.toml`;
4. the OpenRouter snapshot bundled inside the package
   (`src/tamias/data/prices.default.toml`).

When it falls back to the bundled snapshot, `tamias` prints a notice on stderr.
Run `tamias prices fetch` for current rates. See [`PRICING.md`](PRICING.md) for
the schema.

## `tamias serve` flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--upstream URL` | `$TAMIAS_UPSTREAM` or the `--provider` preset | Base URL of the OpenAI-compatible upstream, **without** `/v1/chat/completions`. |
| `--provider {ollama,openai,openrouter}` | — | Named upstream preset, used when `--upstream` is not set. |
| `--prices PATH` | resolved (see above) | Path to the TOML price sheet. |
| `--db PATH` | `requests.db` | Path to the sqlite request log. |
| `--port PORT` | `8000` | Port to listen on. |
| `--host HOST` | `127.0.0.1` | Address to bind. |
| `--allow-non-loopback` | off | Acknowledge a wider bind and bind anyway; a non-loopback bind still requires `--auth-token` / `$TAMIAS_AUTH_TOKEN` / `--keys-db`. |
| `--trusted-host NAME` | — | Allow a Host header beyond loopback (repeatable); enables the Host allowlist on a wider bind. |
| `--router-mode {shadow,off,active}` | `shadow` | `off` disables routing; `shadow` records the decision without changing the request; `active` also rewrites the model to `--cheap-model`, but only a target `--registry` vouches for and `--prices` can price. |
| `--router-profile {generic,legacy}` | `generic` | Matching strategy for the router. |
| `--router-config PATH` | — | TOML router configuration path. |
| `--cheap-model MODEL` | — | Model a SWITCH would move mechanical work to. |
| `--strong-model MODEL` | — | Model the agent is expected to start on. |
| `--min-gap N` | `3` | Requests that must pass after the last switch before switching again. |
| `--effort-mode {off,meter,apply}` | `off` | Scope of the effort axis: `off` computes nothing, `meter` records the effort the policy would use, `apply` also sends it (only for a model `--registry` vouches for). |
| `--effort-style {openrouter,openai}` | `openrouter` | Request-field style used when the effort policy is active. |
| `--registry PATH` | `$TAMIAS_REGISTRY` or `~/.tamias/models.toml` | TOML capability catalogue `apply` consults. |
| `--inject-usage` | off | Ask the upstream for a usage chunk on streaming requests even when the client did not. Off forwards the body byte-for-byte. |
| `--request-usage-cost` | off | Ask OpenRouter to include usage and provider-reported cost. |
| `--request-ceiling-usd USD` | `0.25` | USD reserved for one request before it is forwarded. |
| `--keys-db PATH` | off | Enable company-key authentication from this SQLite key store. |
| `--auth-token TOKEN` | `$TAMIAS_AUTH_TOKEN` | Bearer token clients must send. Prefer the environment over argv. |
| `--tls-cert PATH` / `--tls-key PATH` | — | Serve HTTPS instead of HTTP. |
| `--max-body-bytes N` | `10485760` | Reject larger request bodies with 413 (`0` disables). |
| `--max-concurrency N` | `64` | Limit concurrent in-flight requests (`0` disables). |
| `--manifest PATH` | off | Signed policy manifest (JSON) verified at startup and served at `GET /policy`. |
| `--manifest-key KEY` | — | Pin the manifest to this Ed25519 public key. |
| `--verbose` | off | Log every proxy event. |

## `tamias report` flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--db PATH` | — (required) | Path to the sqlite request log. |
| `--prices PATH` | — (required) | TOML price sheet used to price the log. |
| `--inr-rate RUPEES_PER_USD` | off | Also show each dollar amount converted to rupees at this fixed rate, e.g. `--inr-rate 85`. This is a **display conversion**, not a billed amount: the dollar figure stays authoritative and is the one that reconciles against a provider invoice. |

## `tamias doctor`

```text
tamias doctor [--prices PATH] [--db PATH] [--port PORT] [--upstream URL] [--online]
```

It prints one line per check, prefixed `OK`, `WARN` or `FAIL`. **Exit code is a
contract: `0` = ready, `1` = a blocking problem.**

- `FAIL` lines — unsupported Python, an unwritable DB directory, a busy port —
  set the exit code to `1`.
- `WARN` lines — a stale or missing price sheet, a missing `OPENROUTER_API_KEY`,
  an unreachable upstream — do **not**. A WARN-only run still exits `0`, so it
  is safe to chain with `&&`.
- `--online` additionally checks upstream reachability (a `WARN` if unreachable).

## Other exit codes

- `2` is the argparse/usage code: an unknown command or missing required
  argument.
- `1` is the runtime failure code: `serve`/`report`/`admin`/`prices fetch`
  print the reason on stderr and exit `1`.
