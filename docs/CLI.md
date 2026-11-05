# Command line

Every command runs as `tamias <command>`. `tamias --help` lists them:

```text
{prices,serve,run,demo,reconcile,report,agent-config,opencode,service,registry,doctor,prune,admin,config}
```

| Command | Purpose |
| --- | --- |
| `serve` | Run the proxy in front of an upstream. |
| `run` | Run one command through a short-lived loopback proxy, then print its report. |
| `demo` | Run one request through the proxy against a bundled mock upstream, then print a receipt — zero keys, zero network, zero config. |
| `reconcile` | Reconcile logged rows against the provider's billed cost per request. |
| `report` | Summarise cost and shadow routing from a log. |
| `agent-config` | Print an agent configuration snippet. |
| `opencode install` | Write the report slash command OpenCode loads at startup (`/tamias`, or `/tamias-report` when the pop-up owns the name). |
| `opencode setup` | Merge the `tamias` provider into OpenCode's config (backup first) and register the TUI pop-up. |
| `service install` | Write a systemd user unit that runs `tamias serve` in the background (Linux). |
| `service uninstall` | Stop and remove the unit tamias wrote. |
| `doctor` | Check local tamias readiness. |
| `prune` | Delete old rows from the request log. |
| `admin` | Manage company keys and signed project manifests. |
| `prices fetch` | Fetch an OpenRouter model price sheet. |
| `prices set` | Write one model's rates into the hand-written overlay beside a sheet. |
| `prices zen` | Fetch OpenCode Zen's model list and write a sheet that prices its free models at $0 (paid models stay UNKNOWN). |
| `registry zen` | Build a capability registry for Zen's free models from models.dev's published `tool_call` data. |
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
Run `tamias prices fetch --out ~/.tamias/prices.toml` for current rates. See
[`PRICING.md`](PRICING.md) for
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
| `--cheap-model MODEL[,MODEL...]` | — | Model a SWITCH would move mechanical work to, or a comma-separated pool. Each session keeps one member; different sessions spread across the pool. `active` validates every member. |
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

`--cheap-model` accepts a comma-separated pool, for example
`--cheap-model deepseek-v4.1-flash-free,big-pickle`. With `--router-mode
active`, a missing, tool-less, or unpriced member refuses startup, so a pool
can never silently pick a model the deployment did not approve. The pool spreads
sessions across members; on a refusal (`429`, `404`, `5xx`, transport failure)
the request is retried once on the next member that passed the same gates. See
[`ROUTING.md`](ROUTING.md#the-cheap-model-pool).

## `tamias run` flags

`tamias run [FLAGS] -- COMMAND` starts a short-lived proxy, runs `COMMAND` with
`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` pointed at it, then prints the receipt
for that run. It takes the **same routing flags as `serve`**, with the same
meanings and the same precedence (CLI flag > config file > built-in default):

`--router-mode`, `--min-gap`, `--cheap-model`, `--strong-model`, `--registry`,
`--router-config`, `--effort-mode`, `--keys-db`, `--request-ceiling-usd`,
`--prices`, `--db`, `--upstream`, `--provider`.

`--mode` is a **deprecated alias** for `--router-mode`, kept so existing
`tamias run --mode active` invocations keep working. It prints a deprecation
notice on stderr, and passing both flags with different values is a usage
error (`2`). `--min-gap` was previously available only on `serve`.

## `tamias report` flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--db PATH` | — (required) | Path to the sqlite request log. |
| `--prices PATH` | — (required) | TOML price sheet used to price the log. |
| `--inr-rate RUPEES_PER_USD` | off | Also show each dollar amount converted to rupees at this fixed rate, e.g. `--inr-rate 85`. This is a **display conversion**, not a billed amount: the dollar figure stays authoritative and is the one that reconciles against a provider invoice. |

## `tamias demo` flags

| Flag | Required | Meaning |
| --- | --- | --- |
| `--prices PATH` | no | Price sheet used for savings estimates. Defaults to the bundled OpenRouter snapshot. |

`tamias demo` runs one request against a bundled mock upstream, writes a local
SQLite log (`.tamias-demo/demo.db`), prints the status, and then runs
`tamias report` over it. Zero keys, zero network, zero config.

## `tamias prices zen`

```text
tamias prices zen --out PATH [--url URL] [--force]
```

Fetches OpenCode Zen's model list (`https://opencode.ai/zen/v1/models`) and
writes a price sheet in which **every model whose id marks it free** (`-free`
suffix, plus the documented `big-pickle` exception) is priced at exactly `$0`.

Paid models are deliberately **absent** from the sheet: Zen's models endpoint
publishes ids but no rates, and an absent rate means `UNKNOWN` — never a guessed
dollar figure. `tamias report` then prints exact token counts for those rows and
says the dollars are unknown, instead of inventing a price.

## `tamias prices set`

```text
tamias prices set MODEL [--input USD] [--output USD] [--cached-input USD]
                        [--cache-write USD] [--cache-write-1h USD]
                        [--sheet PATH]
```

Writes one model's rates into the hand-written overlay beside a generated sheet:
`--sheet ~/.tamias/prices.toml` means `~/.tamias/prices.local.toml`. The overlay
wins per model id, so it prices a direct provider, a free development tier or a
local model that the generated catalogue does not cover, and re-running
`tamias prices fetch` cannot erase it. At least one rate is required; a negative
rate is refused with exit `2`.

`--input 0 --output 0` is how a genuinely free tier is recorded. An omitted
rate stays `UNKNOWN` — never free by assumption.

## `tamias opencode install`

```text
tamias opencode install --db PATH --prices PATH [--port PORT]
                        [--project DIR] [--registry PATH] [--providers PATH]
                        [--cheap-model MODEL[,MODEL...]] [--model ID ...]
                        [--plugin DIR] [--inr-rate RUPEES_PER_USD] [--force]
```

Writes a report slash command into the directory OpenCode loads at startup:

- without the pop-up: `/tamias`, at
  `$XDG_CONFIG_HOME/opencode/commands/tamias.md` (falling back to
  `~/.config/opencode/commands/tamias.md`) or
  `<project>/.opencode/commands/tamias.md` with `--project`;
- with the pop-up installed: `/tamias-report`, in the same directory, because
  the TUI plugin already registers `/tamias` and two registrations of one slash
  command are not a contract.

The plugin is found from `--plugin DIR`, `$TAMIAS_PLUGIN_DIR`, or the source
checkout beside the package. When a legacy `tamias.md` exists, `opencode setup`
warns that it may shadow the pop-up.

The command file injects `tamias report` output with OpenCode's shell syntax, so
typing the command shows real logged numbers. The installer **never edits
`opencode.json`**; it prints the provider block for you to merge, because
rewriting a working config is not a safe default.

`--inr-rate` embeds `report --inr-rate` into the slash command, so the report
shows each dollar amount with a rupee conversion. The rate is a display
conversion you supply, not a billed amount; leave it off rather than guess.

Exit codes: `0` written, `2` the command file already exists and `--force` was
not passed, `1` the file could not be written.

## `tamias opencode setup`

```text
tamias opencode setup [--config PATH] [--plugin DIR] [--db PATH] [--prices PATH]
                      [--providers PATH] [--registry PATH] [--port PORT]
                      [--cheap-model MODEL[,MODEL...]] [--model ID ...]
                      [--dry-run]
```

The opt-in writer behind `install`. It merges `provider.tamias` into OpenCode's
config (`~/.config/opencode/opencode.jsonc` when present, else `opencode.json`),
keeping every other key, and copies the original to
`<name>.bak-YYYYMMDDTHHMMSSZ` first. The backup is written mode `0600` because
the config it copies may already hold provider credentials; the command names
it and says to delete it once the new config is confirmed. When the TUI plugin
can be found — a
`--plugin` path, `$TAMIAS_PLUGIN_DIR`, or the source checkout beside the package
— it is appended to `plugins` in `cli.json`, once, and `/tamias` opens the
pop-up while `/tamias-report` runs the report.

OpenCode's config is JSONC; rewriting it needs plain JSON, so `//` and `/* */`
comments and trailing commas are dropped. When any comment was present the
command says so and names the backup. `--dry-run` prints both documents and
writes nothing.

The printed `tamias serve` command names `--providers` when a catalogue exists
(default: `~/.tamias/providers.toml`), so one endpoint fronts every provider;
without a catalogue it falls back to `--provider opencode`.

Exit codes: `0` written, `2` no model to offer or a usage error, `1` the config
could not be read or written.

## `tamias service install`

```text
tamias service install --db PATH --prices PATH [--registry PATH]
                       [--providers PATH | --provider NAME]
                       [--cheap-model MODEL[,MODEL...]] [--port PORT]
                       [--min-gap N] [--router-mode {shadow,off,active}]
                       [--exec PATH] [--start] [--dry-run] [--force]
```

Writes `$XDG_CONFIG_HOME/systemd/user/tamias.service` (falling back to
`~/.config/systemd/user/tamias.service`). `--db`, `--prices`, `--registry` and
`--providers` are resolved to absolute paths; the input files must already
exist. The unit is written but not started unless `--start` is passed, which
then runs `systemctl --user daemon-reload` and `enable --now tamias.service`.

`--providers` wins over `--provider`, so the unit can route across every
catalogue entry rather than a single upstream preset. The fallback remains
`--provider opencode` for an older single-provider setup.

In `active` mode (the default) a target and a registry are required, mirroring
`tamias serve`'s fail-closed check. `--dry-run` prints the unit without writing
it; `--force` replaces an existing file. Exit codes: `0` written, `2` a usage
error or an existing unit without `--force`, `1` the file could not be written
or systemctl failed.

```text
tamias service uninstall [--dry-run] [--keep-running] [--force]
```

Stops and disables the unit, removes the file, and reloads systemd. It refuses
to remove a file that lacks tamias's marker unless `--force` is given.
`--keep-running` removes the file without stopping the proxy.

## `tamias registry zen`

```text
tamias registry zen --out PATH [--models-url URL] [--catalog-url URL] [--force]
```

Joins two public feeds:

- `https://opencode.ai/zen/v1/models` — the ids that are live right now;
- `https://models.dev/api.json` — capability metadata, including `tool_call`
  and the context limit.

Only models that are **live**, **free** (`-free` suffix or the documented
`big-pickle` exception), and **published as tool-capable** are written. A model
missing from either feed, or with `tool_call` not true, is listed as excluded on
stdout and left out of the registry. That is what lets `--router-mode active`
start without guessing whether a free model can carry a tool call.

This registry contains **capabilities only**. Prices stay in the price sheet.

## `tamias reconcile` flags

| Flag | Required | Meaning |
| --- | --- | --- |
| `--db PATH` | yes | Path to the SQLite request log. |
| `--prices PATH` | no | TOML price sheet used to price the log. Defaults to the same resolution as `report`. |
| `--api-key-env NAME` | no | Environment variable holding the provider API key (default `OPENROUTER_API_KEY`). Only the name is read; the secret never enters argv. |
| `--out PATH` | no | Write a text-free JSON artifact (one row per request) to this path. |
| `--since YYYY-MM-DD` | no | Lower bound on row timestamp (inclusive). |
| `--until YYYY-MM-DD` | no | Upper bound on row timestamp (inclusive). |
| `--tolerance USD` | no | Absolute USD tolerance used when comparing computed vs billed cost. |
| `--json` | no | Emit a compact JSON summary on stdout instead of the human-readable report. |

**Exit codes:** `0` complete, `1` runtime failure (reason on stderr), `2`
`ZERO_ROWS` (no rows with a `generation_id` were found). See
[`docs/RECONCILE.md`](RECONCILE.md) for the full contract, artifact columns, and
third-party check notes.

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
