# Pointing OpenCode at tamias

tamias sits between OpenCode and an OpenAI-compatible upstream:

```
OpenCode  ──POST /v1/chat/completions──▶  http://localhost:8000/v1  ──▶  https://opencode.ai/zen
                                          (tamias, shadow mode)
```

In the default **shadow mode** tamias forwards every request byte-for-byte, logs
token counts and the routing decision it *would* have made, and changes nothing
upstream. Nothing about your prompts or responses is written to disk.

## Verified against current OpenCode docs

Every `opencode.json` snippet below was checked against the live documentation
on **2026-10-04**:

- <https://opencode.ai/docs/providers/#custom-provider> — the custom
  OpenAI-compatible provider shape (`npm`, `name`, `options.baseURL`,
  `options.apiKey`, `options.headers`, `models`)
- <https://opencode.ai/docs/config/> — config file locations, `$schema`,
  `{env:VAR}` substitution, `opencode debug config`
- <https://opencode.ai/docs/zen/> — Zen endpoints per model, model IDs, prices

See [What I could not verify](#what-i-could-not-verify) at the end.

## 1. Get a Zen API key

1. Sign in at <https://opencode.ai/auth> and add billing details.
2. Create an API key.
3. Put it in your shell:

```sh
export OPENCODE_API_KEY=sk-...
```

tamias forwards `Authorization` untouched, so the key reaches Zen unchanged.

## 2. Start tamias

```sh
tamias serve \
  --upstream https://opencode.ai/zen \
  --prices  prices.toml \
  --db      requests.db \
  --port    8000 \
  --cheap-model deepseek-v4-flash \
  --strong-model deepseek-v4-pro
```

tamias appends `/v1/chat/completions` to `--upstream`, so the upstream URL it
calls is `https://opencode.ai/zen/v1/chat/completions`. Do **not** include
`/v1` in `--upstream`.

- `--router-mode` defaults to `shadow`. `active` applies the switch; `off`
  disables the router entirely. Start with `shadow`.
- `--cheap-model` is the model a SWITCH would move mechanical work to. Without
  it, shadow decisions have no target and no measurable saving, and tamias logs
  a warning at startup.
- `--min-gap` (default 3) is how many requests must pass after the last switch
  before switching again.

Check it is up:

```sh
curl -s localhost:8000/v1/models | head -c 200
```

## 3. Write a price sheet

Rates are **USD per 1,000,000 tokens**, taken from the Zen pricing table. A rate
of `0` means the model really is free; a rate you leave out means UNKNOWN.

```toml
date = "2026-10-04"   # the date these rates were published

[deepseek-v4-pro]
input = 1.74
output = 3.48
cached_input = 0.145
cache_write = 0

[deepseek-v4-flash]
input = 0.14
output = 0.28
cached_input = 0.028
cache_write = 0
```

Set `cache_write = 0` for these models. Zen does not publish a cache-write rate
for most models and does not report cache writes in `usage`, so a nonzero
`cache_write` would leave every request UNKNOWN — see
[When cost is UNKNOWN](#when-cost-is-unknown).

## 4. Point OpenCode at the proxy

Add a custom OpenAI-compatible provider to `opencode.json`. Either:

- **global** — `~/.config/opencode/opencode.json`, for every project, or
- **per project** — `opencode.json` in the project root

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "tamias": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "tamias (local shadow proxy)",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "{env:OPENCODE_API_KEY}"
      },
      "models": {
        "deepseek-v4-pro": {
          "name": "DeepSeek V4 Pro (via tamias)"
        },
        "deepseek-v4-flash": {
          "name": "DeepSeek V4 Flash (via tamias)"
        }
      }
    }
  },
  "model": "tamias/deepseek-v4-pro"
}
```

Notes on each key, all from the docs cited above:

- `npm: "@ai-sdk/openai-compatible"` is the package for APIs that speak
  `/v1/chat/completions`. (For `/v1/responses` you would need
  `@ai-sdk/openai` — see the Zen caveat below.)
- `options.baseURL` is the endpoint. The AI SDK appends `/chat/completions`, so
  `http://localhost:8000/v1` produces exactly the path tamias answers on.
- `options.apiKey` uses `{env:VAR}` substitution. If the variable is unset it
  becomes an empty string. You can omit `apiKey` and let `/connect` hold the
  credential instead.
- `models` maps the model id OpenCode will send in the request body to a display
  name. These ids are what tamias sees as `model_requested`, so they must match
  your price sheet keys exactly.
- `model` is `"<provider-id>/<model-id>"`, here `tamias/deepseek-v4-pro`.

### Using `/connect` instead of an inline key

1. In the TUI run `/connect`, scroll to **Other**.
2. Enter the provider id `tamias`.
3. Paste the key.

It is stored in `~/.local/share/opencode/auth.json`. Then drop `options.apiKey`
from the config.

### Verify the config resolved

```sh
opencode debug config
```

You should see the `tamias` provider with `baseURL`
`http://localhost:8000/v1`. In the TUI, `/models` lists your models as
`tamias/deepseek-v4-pro` and `tamias/deepseek-v4-flash`.

While a session runs, tamias logs one decision line per request to stderr:

```
tamias.proxy session=s1 prior_requests=3 model=deepseek-v4-pro mode=shadow \
  decision=SWITCH target=deepseek-v4-flash reason=easy tool: read
```

`decision=SWITCH` there means *shadow mode would have switched*. It did not, and
the request that Zen received still said `deepseek-v4-pro`.

## 5. Read the log

```sh
tamias report --db requests.db --prices prices.toml
```

```
requests: 412
total cost: UNKNOWN (known part: $0.00; 412 of 412 requests have unknown cost)
requests shadow would have switched: 96
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured; 96 of 96 switched requests not priced)
cheap model assumed: deepseek-v4-flash
price sheet: prices.toml (2026-10-04)
```

The shadow-mode signal is unaffected: the decision column, the switch count, the
latency, the status and the token counts are all still logged per request, and
the decision log lines are unaffected. Only the money arithmetic is unavailable,
and only until either the upstream reports a cache-write count or the pricing
formula is changed to treat an unreported cache-write count as zero — which
[CONTRACT.md](../CONTRACT.md) forbids ("unknown is NEVER treated as 0").

The number that matters in shadow mode is **requests shadow would have
switched**. It tells you how much of your traffic the router thinks is
mechanical, which is the input to deciding whether to run `--router-mode active`.

## Zen caveat: only some Zen models are reachable through tamias

tamias proxies exactly one route, `/v1/chat/completions`. Zen serves different
model families on different endpoints (from the Zen docs):

| Zen endpoint | AI SDK package | Models |
| --- | --- | --- |
| `https://opencode.ai/zen/v1/chat/completions` | `@ai-sdk/openai-compatible` | `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4.1-flash`, `deepseek-v4-flash-vision-exp`, `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5`, `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `glm-5.1`, `glm-5`, `minimax-m3`, `qwen3.8-max`, and the free models incl. `big-pickle`, `space-bunny-free` |
| `https://opencode.ai/zen/v1/responses` | `@ai-sdk/openai` | `gpt-6*`, `gpt-5*`, `grok-*`, `muse-spark-*` |
| `https://opencode.ai/zen/v1/messages` | `@ai-sdk/anthropic` | `claude-*`, `qwen3.7-*`, `qwen3.6-plus`, `qwen3.5-plus`, `qwen3.8-flash` |
| `https://opencode.ai/zen/v1/models/<id>` | `@ai-sdk/google` | `gemini-*` |

Only the first row is proxied. If you configure a Claude or GPT model here,
Zen will answer `404` for `/v1/chat/completions`. Pick a model from the first
row.

Zen's own list of what is live is at `https://opencode.ai/zen/v1/models`; the
table above reflects the docs page as of the date above and Zen adds and retires
models often.

## When cost is UNKNOWN

Cost is UNKNOWN — never guessed as zero — when a field needed to price the
request is missing. Against Zen the likely causes are:

- the model is not in your price sheet (check the id matches exactly);
- the request was streamed and the client did not send
  `stream_options.include_usage`, so Zen's usage chunk was never requested;
- Zen did not report `prompt_tokens_details.cached_tokens` while your sheet
  prices `cached_input` differently from `input`;
- your sheet sets a nonzero `cache_write` but Zen reports no cache writes.

Fix the sheet or the request; there is no configuration that makes a missing
number appear. `?` in a report means exactly that.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `tamias serve: router is shadow but no cheap model is configured` | Pass `--cheap-model`; without it a SWITCH has no target. |
| Zen returns 404 | The model is not on `/v1/chat/completions`. See the Zen caveat above. |
| Model missing from `/models` in OpenCode | Check `opencode debug config`, and that `models` keys match the ids in your price sheet. |
| Every row has `session_id = default` | No `x-tamias-session` header arrived, so all requests fell into the `default` session. OpenCode does not appear to send this header (unverified — see below); a plugin is needed to group rows by conversation. |
| `tamias serve` exits 1 at startup | Price sheet missing, unreadable, or missing its top-level `date`. The message names the file. |
| Nothing logged after a request | The route was not `/v1/chat/completions`. Only chat completions are logged; other paths are relayed without a row. |

## What I could not verify

- **No Zen API key was available**, so I could not make a real request through
  this chain. The `opencode.json` format, the `@ai-sdk/openai-compatible`
  package choice, the `{env:VAR}` syntax, the config file locations and the Zen
  endpoint/model/price tables are all taken from the live docs cited at the top
  of this file, not from an observed session.
- **I could not confirm what Zen actually returns in the `usage` object.** The
  claim that no cache-write count is reported is based on the OpenAI
  chat-completions schema, not on a Zen response. If Zen does report one,
  `to_usage` would still discard it — see src/tamias/proxy.py:68 — so the
  limitation holds either way.
- **The `@ai-sdk/openai-compatible` package is not pinned anywhere I could
  read**; it is the value the docs prescribe for `/v1/chat/completions`
  providers, but I did not inspect the installed AI SDK to confirm it resolves.
- **Zen's model list changes frequently.** The first-row models above are from
  the docs page; treat `https://opencode.ai/zen/v1/models` as authoritative.
- **I could not confirm whether OpenCode sends an `x-tamias-session` header.**
  I found no mention of it in the docs I read, so I assume it does not and that
  every request lands in the `default` session. If OpenCode does send something
  usable, `tamias.proxy.SESSION_HEADER` is the only place that needs changing.