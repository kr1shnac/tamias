# Providers

Tamias is a layer **above** providers, never one of them. It ships no model of
its own and issues no credential of its own. A client points at tamias and names
a model; tamias decides which configured provider serves that model, presents
*that provider's* credential on the way out, and meters the result.

Adding a provider is a config entry, not a code change. This page covers that
entry, how a model is matched to a provider, how the model catalogue is
discovered, and what happens when a provider or one of its models is at its
limit.

## The config file

`--providers PATH` (default: `$TAMIAS_PROVIDERS`, else `~/.tamias/providers.toml`)
reads a TOML file:

```toml
[[providers]]
name = "openrouter"
api_key_env = "OPENROUTER_API_KEY"

[[providers]]
name = "nvidia"
api_key_env = "NVIDIA_API_KEY"
api_key_command = "op read op://Private/NVIDIA/api-key"

# Local models need no credential at all.
[[providers]]
name = "ollama"
base_url = "http://127.0.0.1:11434/v1"
```

| Key               | Required | Meaning                                                                   |
| ----------------- | -------- | ------------------------------------------------------------------------- |
| `name`            | yes      | The provider id used by `models.toml`'s `provider` column and `/limits`.   |
| `base_url`        | no       | API root. Defaults from the built-in preset when `name` is known.          |
| `api_key_env`     | no       | Environment variable holding the key.                                      |
| `api_key_command` | no       | Command that prints the key on stdout.                                     |
| `auth`            | no       | `bearer` (default) or `none` for a local runtime that authenticates nothing. |
| `effort_style`    | no       | `openrouter` or `openai`; defaults from the preset.                        |
| `models`          | no       | Model ids this provider serves, for an explicit mapping.                   |

Built-in presets (base URL and effort style, never a credential or a model
list): `openrouter`, `openai`, `opencode`, `nvidia`, `groq`, `deepseek`,
`mistral`, `ollama`. Any other name works as long as `base_url` is given.

`base_url` may or may not carry a version segment: a trailing `/v1` or `/api/v1`
is stripped and the wire path is appended, so `https://openrouter.ai/api`,
`https://integrate.api.nvidia.com/v1` and `http://127.0.0.1:11434/v1` all work.

## Credentials

**Tamias does not store secrets.** A provider names where the secret already
lives:

- `api_key_env` — an exported variable. A developer's usual setup.
- `api_key_command` — a command that writes the secret to stdout. This is the
  shape a real secret manager already offers (`op read`, `vault read`, `pass
  show`, `secret-tool lookup`).

Give both and the environment wins when it is set, with the command as the
fallback: one file serves a laptop and a service manager.

The key is read **once at start-up**. It is held in a `Credential` whose `repr`
and `str` are opaque, so a secret cannot reach a log line, an error body or a
decision reason by accident. Client-supplied `Authorization` headers are always
dropped before the provider's own credential is applied: whatever the client
used to reach tamias is not a credential for the downstream provider.

A provider whose secret cannot be read is reported as **unconfigured** and is
skipped by routing. It is never tried with the wrong credential. `GET /limits`
lists unconfigured providers (names and URLs only, never the env var or the
command's output).

## Which provider serves a model

Resolution is **explicit, never guessed from the id**. `nvidia/nemotron` names
a *vendor* namespace on OpenRouter far more often than it names the NVIDIA
platform, so prefix matching would silently bill the wrong account.

The order is:

1. a `models = [...]` list in the provider entry;
2. the `provider` column of the registry (`models.toml`);
3. the single configured provider, when there is exactly one.

A model none of those claim is refused with `400` rather than sent to whichever
provider answered last.

## Discovery

At start-up tamias reads each configured provider's `/v1/models` (trying
`/v1/models`, then `/models`), caches the answer at `~/.tamias/models.cache.json`
(mode `0600`), and merges it into the registry. Hand-written registry entries
always win, so a pinned `quality`, a corrected context window or a deliberate
`tools = false` is never overwritten by provider metadata.

New models therefore appear without editing config. `--no-discover` skips the
fetch and uses the cache/registry as they are. Set `TAMIAS_MODELS_CACHE` to
move the cache file.

## The routing gates

A model is only ever chosen after passing three independent gates:

| Gate         | Question                                                            | Rejected as            |
| ------------ | ------------------------------------------------------------------- | ---------------------- |
| `capability` | Can it carry *this* request? tools, window, modality, reasoning effort | `capability: ...`      |
| `supply`     | Does a configured provider serve it, and can the sheet price it?     | `supply: unpriced`     |
| `health`     | Has the limit ledger recorded a lockout?                             | `health: 429 until ...` |

What survives is a `CandidatePool`: ranked **cheapest first, then higher
quality, then model id**, with each model's blended cost per million tokens
(3:1 input:output). The router may only switch within this pool, and the caller
supplies a quality floor for the step, so cheapness can only choose among models
already judged good enough.

`GET /limits` shows the result:

```sh
curl -s localhost:8080/limits | jq '{pool, candidates: [.candidates[] | {model, cost_per_mtok, quality}], cooling_down}'
```

The candidate and rejection lists are capped (`?models=N`, default 25, maximum
500) because the endpoint is polled; the `pool` block always carries the exact
totals, the truncation flag and per-gate rejection counts.

## When a provider says "no"

A `429`, `402` (out of credits), a gated `403`, a `503` or a transport failure
all mean the same operational thing: do not send this model there right now.
Tamias records the lockout in a small SQLite ledger beside the request log
(`--limits`, default `limits.db`), so a restart does not forget an hour-long
limit, and routing skips the model until the cooldown expires.

- **Escalation.** Repeated failures on the same model back off further:
  60s, 5m, 15m, 1h.
- **`Retry-After` wins** when it asks for longer than the ladder (seconds or an
  HTTP date).
- **`401` is provider-wide.** It is about the credential, not one model, so the
  whole provider is skipped and `/limits` shows `model: "*"`.
- **A limit is availability, not a savings preference.** A locked-out requested
  model switches even in shadow mode; the log row still records that the switch
  was forced by availability rather than by cost.
- **One bounded retry.** A request that hits a retryable status is retried once
  against the pool, preferring the cheapest model at least as capable as the one
  that refused and falling back to the strongest survivor when none is. The
  retry uses that model's provider and credential. If it also fails, the request
  fails; there is no unbounded failover loop.

A `400` or `422` is a request problem and never cools a model down.

## Caveats

- **Credential lifetime.** Keys are read once at start-up. Rotating one requires
  a restart; tamias does not re-run `api_key_command` per request.
- **Discovery is best-effort.** A provider that does not answer `/v1/models`
  contributes no discovered models; its hand-written registry entries still
  route.
- **Provider quirks are not hidden.** Some providers reject non-vendor clients
  (OpenCode Zen answers `429 FreeUsageLimitError` to anything that is not the
  OpenCode client), and some accounts are gated per model. Both are recorded as
  limits and skipped, not retried in a loop.
- **Quality is not metadata.** A model's `quality` comes from the registry when
  declared and is otherwise derived from tool/reasoning/window facts, capped at
  `70`. Provider metadata cannot rank answer quality, so an unannotated model
  never outranks one the operator rated.
