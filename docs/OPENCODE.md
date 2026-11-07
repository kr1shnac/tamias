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

See [Not verified](#not-verified) at the end.

## 1. Get a Zen API key

1. Sign in at <https://opencode.ai/auth> and add billing details.
2. Create an API key.
3. Put it in your shell:

```sh
export OPENCODE_API_KEY=sk-...
```

tamias forwards `Authorization` untouched, so the key reaches Zen unchanged.

## 2. Start tamias

### Across every provider you have

This is the v1 shape: one endpoint in front of every key, with the model id
deciding which provider is called.

```sh
tamias serve \
  --providers ~/.tamias/providers.toml \
  --prices    ~/.tamias/prices.toml \
  --db        ~/.tamias/requests.db \
  --registry  ~/.tamias/models.toml \
  --router-mode active \
  --min-gap  0 \
  --port     8000 \
  --cheap-model model-a,model-b
```

`--providers` is the catalogue from [PROVIDERS.md](PROVIDERS.md). Each entry
names a base URL and where its key comes from; tamias discovers each provider's
`/v1/models`, maps every id to the provider that serves it, and presents that
provider's credential on the way out. A `404` from one provider is recorded as
"not offered for this account" and the request retries on the next gated model
inside the same turn.

### Zen alone

If Zen is your only provider, the preset is shorter:

```sh
tamias serve \
  --provider opencode \
  --prices   prices.toml \
  --db       requests.db \
  --registry models.toml \
  --router-mode active \
  --min-gap  0 \
  --port     8000 \
  --cheap-model deepseek-v4.1-flash-free
```

`--provider opencode` is the Zen preset (`https://opencode.ai/zen`); tamias
appends `/v1/chat/completions`, so the upstream URL it calls is
`https://opencode.ai/zen/v1/chat/completions`. Do **not** include `/v1` in
`--upstream`.

### The flags that matter either way

- `--router-mode` defaults to `shadow`. `active` applies the switch; `off`
  disables the router entirely. Start with `shadow` for a session if you want
  to see what it *would* do before letting it rewrite.
- `--cheap-model` is the model a SWITCH moves mechanical work to, or a
  comma-separated pool. With a pool, each session stays on one member while
  different sessions spread across it; ordering is cheapest first, behind the
  three gates. Without any target, shadow decisions have no target and no
  measurable saving.
- `--registry` is required by `active`: the target must be listed there with
  `tools = true`, because a model that cannot carry a tool call would break the
  turn. `tamias registry zen --out models.toml` builds one from the live Zen
  list plus models.dev's published `tool_call` capability; for other providers
  write the entries yourself, with `provider` naming the catalogue entry.
- `--min-gap` (default 3) is how many requests must pass after the last switch
  before switching again. **For savings, use `0`.** A fresh OpenCode session
  starts at zero requests, so the default 3 holds back the first three easy
  turns; `0` lets the first mechanical tool result route. Raise it if you see
  the cheap model flapping on a long session.

A pool is written as a comma-separated list and keeps each OpenCode session on
one member, spreading different sessions across the rest:

```sh
--cheap-model deepseek-v4.1-flash-free,big-pickle
```

Add a model only after you have seen it complete a tool-calling turn yourself:
the registry proves models.dev publishes `tool_call = true`, not that the free
endpoint is available right now. If a member refuses with `429`, `404`, `5xx` or
a transport failure, the request is retried on the next gated model.

Check it is up:

```sh
curl -s localhost:8000/v1/models | head -c 200
```

## 3. Write a price sheet

Rates are **USD per 1,000,000 tokens**, taken from the Zen pricing table. A rate
of `0` means the model really is free; a rate you leave out means UNKNOWN.

Zen's models endpoint publishes ids but not rates, so `tamias prices zen` can
write the free half of the sheet for you:

```sh
tamias prices zen --out prices.toml
```

It prices every free model at exactly `$0` and leaves paid models out (UNKNOWN)
rather than guessing. Add the paid rates you care about by hand:

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

### One command

`tamias opencode setup` writes the provider block for you: it merges
`provider.tamias` into the existing config, keeps every other key, copies the
original to a timestamped backup, and registers the TUI pop-up when it can find
it. The backup is mode `0600` and may still contain whatever credentials the
config held; setup names it, and you should delete it once the new config
works.

```sh
tamias opencode setup \
  --providers ~/.tamias/providers.toml \
  --registry ~/.tamias/models.toml \
  --prices ~/.tamias/prices.toml \
  --cheap-model model-a,model-b
```

It prints the matching `tamias serve` command, names `--providers` when a
catalogue exists, and warns when JSONC comments had to be dropped. `--dry-run`
shows both documents without writing.

The `apiKey` it writes is the literal `tamias-local`: a placeholder, not a
secret. OpenCode hides a custom provider that carries no credential, while
tamias replaces whatever the client sends with the provider's own key. A real
key here would be one more copy to rotate.

### Manually

Add a custom OpenAI-compatible provider to `opencode.json`. Either:

- **global** — `~/.config/opencode/opencode.json`, for every project, or
- **per project** — `opencode.json` in the project root

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "tamias": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "tamias",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "tamias-local"
      },
      "models": {
        "model-a": {
          "name": "model-a (via tamias)"
        },
        "model-b": {
          "name": "model-b (via tamias)"
        }
      }
    }
  },
  "model": "tamias/model-a"
}
```

Notes on each key, all from the docs cited above:

- `npm: "@ai-sdk/openai-compatible"` is the package for APIs that speak
  `/v1/chat/completions`, which tamias logs and routes. (For `/v1/responses` you
  would use `@ai-sdk/openai`; tamias meters that path but never switches on it —
  see the Zen caveat below.)
- `options.baseURL` is the endpoint. The AI SDK appends `/chat/completions`, so
  `http://localhost:8000/v1` produces exactly the path tamias answers on.
- `options.apiKey` is a placeholder. Tamias forwards the credential its provider
  catalogue names, not this value. If you start `tamias serve` with
  `--auth-token`, put that token here (or in `{env:VAR}`) so OpenCode can
  authenticate to the proxy.
- `models` maps the model id OpenCode will send in the request body to a display
  name. These ids are what tamias sees as `model_requested`, so they must match
  your registry and price sheet keys exactly.
- `model` is `"<provider-id>/<model-id>"`, here `tamias/model-a`.

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

## 5. Install the `/tamias` pop-up (and the report command)

The TUI pop-up and the report command answer different questions, so they have
different names when both are installed:

| Surface | Command | What it does |
| --- | --- | --- |
| TUI plugin | `/tamias` | opens a centred pop-up with the live pool, the latest route, and the savings bar |
| Markdown command | `/tamias-report` | runs `tamias report` and hands the real output to the model |

Install the report command once the proxy has logged a few requests:

```sh
tamias opencode install \
  --db     requests.db \
  --prices prices.toml \
  --port   8000
```

Without the plugin it writes `~/.config/opencode/commands/tamias.md` and the
command is `/tamias`. When `tamias opencode install` (or `setup`) can find the
plugin, it writes `tamias-report.md` instead, because two registrations of
`/tamias` are not a contract. If an older `tamias.md` is still present,
`opencode setup` warns that it may shadow the pop-up. It does **not** touch
`opencode.json`; it prints the provider block to merge instead.

`/tamias-report` runs `tamias report` and gives the model the real output, so
you see requests, tokens, cost (or why it is `UNKNOWN`), how many requests were
routed, and the estimated and realised saving. The model is told never to
invent a dollar figure when the report says `UNKNOWN`.

For the pop-up, register `tamias-opencode-plugin` in the TUI settings
(`~/.config/opencode/cli.json`):

```json
{
  "plugins": ["/absolute/path/to/tamias-opencode-plugin"]
}
```

`tamias opencode setup` adds that entry when it can find the plugin — from
`--plugin DIR`, `$TAMIAS_PLUGIN_DIR`, or the copy bundled inside the installed
package (`tamias/plugin`, present in both the wheel and a source checkout). The
plugin is not on npm; `pip install tamias` carries the TypeScript source. A
checkout is only needed to develop the plugin.

The host resolves a plugin's imports **next to the plugin directory**, so the
bundled copy needs its peers (`solid-js`, `@opentui/core`, `@opentui/solid`)
installed beside it; without them OpenCode 2.2.5 refuses the plugin with
`Cannot find package 'solid-js'` and shows `1 plugin failed`. `setup` therefore
copies the bundled plugin to `~/.tamias/plugin`, runs `bun install` there, and
registers *that* path. It needs Bun on `PATH`; without it, setup says so and
registers nothing rather than leaving a broken entry behind. A source checkout
that already has `node_modules` (from `bun install`) is registered as-is and
rewritten by nothing.

`/tamias` opens a `large`, centred dialog over the conversation. It shows nine
lines: the live/log-only indicator, one savings bar, the request/switch/free
counts with the cache share, the latest route and its reason, the eligible pool
with refused gates, any provider quota the provider itself published, any
cooling model, and a footer. The savings bar is **realised**: it counts
only rows the proxy actually rewrote. In shadow mode nothing is rewritten, so
the bar stays at zero and the header says `· shadow`; the hypothetical estimate
lives in `/tamias-report`. `esc` closes it; the first open in a process shows a
one-line hint. It is not a full-screen route, and it needs no session context —
the same pop-up works before the first chat turn. The plugin is TUI-only: the
report command works without it.

## 6. Run it in the background

The foreground should be OpenCode, not a terminal running the proxy. On Linux
with systemd, tamias can write and optionally start a user unit:

```sh
tamias service install \
  --db       ~/.tamias/requests.db \
  --prices   ~/.tamias/prices.toml \
  --registry ~/.tamias/models.toml \
  --providers ~/.tamias/providers.toml \
  --cheap-model deepseek-v4.1-flash-free,big-pickle \
  --port 8000 \
  --start
```

`--providers` takes precedence over `--provider`: the unit routes across every
provider in the catalogue instead of pinning one upstream. Use `--provider
opencode` only for the older single-upstream setup.

It writes `~/.config/systemd/user/tamias.service` and runs `systemctl --user
daemon-reload` plus `enable --now` only when `--start` is passed. Paths are
resolved to absolute because a user unit does not inherit your working
directory. Every input file must already exist, and active mode refuses to
install without a target and a registry, so a unit that would crash-loop is
rejected before anything is written.

A user unit also does not inherit your shell's environment, so a provider that
reads its key from `api_key_env` is unconfigured under the unit — the same
command works in a terminal only because your shell exported it. Route those
providers by `api_key_command`, or put the variable in the unit with
`systemctl --user edit tamias` (`[Service] Environment=NAME=...`).
`service install` names any such provider when it writes.

No secret goes in the unit: OpenCode sends the provider key to tamias, and
tamias forwards it untouched. Use a free target and no paid key is required.

```sh
systemctl --user status tamias
journalctl --user -u tamias -f     # logs, if you want them
```

To see the unit without writing it, pass `--dry-run`. To stop and remove it:

```sh
tamias service uninstall             # stop, disable, remove tamias's unit
tamias service uninstall --keep-running   # remove the file, leave the proxy up
```

`service uninstall` refuses to delete a unit that tamias did not write unless
you pass `--force`.

## 7. Read the log

```sh
tamias report --db requests.db --prices prices.toml
```

```
requests: 412
total cost: UNKNOWN (known part: $0.00; 412 of 412 requests have unknown cost)
requests shadow would have switched: 96
estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured; 96 of 96 switched requests not priced)
switch target: unknown for 96 switched row(s) with no router decision; savings priced against the sheet-cheapest deepseek-v4-flash
price sheet: prices.toml (2026-10-04)
```

The shadow-mode signal is unaffected: the decision column, the switch count, the
latency, the status and the token counts are all still logged per request, and
the decision log lines are unaffected. Only the money arithmetic is unavailable,
and only until either the upstream reports a cache-write count or the pricing
formula is changed to treat an unreported cache-write count as zero — which the
pricing contract forbids ("unknown is NEVER treated as 0").

*(The sample above predates two changes and is kept only to show the shape of
the output: the `estimate;` label is now `cache-aware, can be negative`, and the
saving prices a warm stay against a cold switch; and the shadow count line now
reads `requests shadow mode would have switched`. The line that used to read
`cheap model assumed: <model>` is now `switch target (from router decisions):
<model>` — the estimate prices the model the router's own decision named, not a
guess from the price sheet. Rows that predate the target column say so and name
the sheet-cheapest fallback explicitly.)*

The number that matters in shadow mode is **requests shadow would have
switched**. It tells you how much of your traffic the router thinks is
mechanical, which is the input to deciding whether to run `--router-mode active`.

## Zen caveat: only some Zen models are reachable through tamias

tamias logs and prices three routes — `/v1/chat/completions`, `/v1/messages`
and `/v1/responses` — and routes (may record a `SWITCH`) on the first two; the
responses route is meter-only. Zen serves different model families on different
endpoints (from the Zen docs):

| Zen endpoint | AI SDK package | Models |
| --- | --- | --- |
| `https://opencode.ai/zen/v1/chat/completions` | `@ai-sdk/openai-compatible` | `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4.1-flash`, `deepseek-v4-flash-vision-exp`, `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5`, `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `glm-5.1`, `glm-5`, `minimax-m3`, `qwen3.8-max`, and the free models incl. `big-pickle`, `space-bunny-free` |
| `https://opencode.ai/zen/v1/responses` | `@ai-sdk/openai` | `gpt-6*`, `gpt-5*`, `grok-*`, `muse-spark-*` |
| `https://opencode.ai/zen/v1/messages` | `@ai-sdk/anthropic` | `claude-*`, `qwen3.7-*`, `qwen3.6-plus`, `qwen3.5-plus`, `qwen3.8-flash` |
| `https://opencode.ai/zen/v1/models/<id>` | `@ai-sdk/google` | `gemini-*` |

The first two rows are routed through tamias (the responses row is metered, not
routed). If you configure a model on an endpoint Zen does not serve for that
path, Zen answers `404`. For routing, prefer a model from the first row.

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
| A hard step was switched to a cheap model | Send `x-tamias-pin: 1` on that request to keep the model you named; the row is logged `STAY` with `pinned by client: kept <model>`. See [ROUTING.md](ROUTING.md#overriding-the-router-for-one-request). |
| Nothing priced after a request | The path was neither `/v1/chat/completions`, `/v1/messages`, nor `/v1/responses` — those three are priced. Every other path is relayed by the catch-all without a price, though it still leaves one count-only row (`route="passthrough"`, shown by `tamias report`). |

## Not verified

- **No live OpenCode-to-Zen session is confirmed.** The `opencode.json` format,
  the `@ai-sdk/openai-compatible` package choice, the `{env:VAR}` syntax, the
  config file locations and the Zen endpoint/model/price tables are all taken
  from the live docs cited at the top of this file, not from an observed session.
- **What Zen actually returns in the `usage` object is not confirmed.** The claim
  that no cache-write count is reported is based on the OpenAI chat-completions
  schema, not on a Zen response. If Zen does report one, `to_usage` would still
  discard it (see `src/tamias/proxy.py`), so the limitation holds either way.
- **The `@ai-sdk/openai-compatible` package is not pinned anywhere this
  repository can read**; it is the value the docs prescribe for
  `/v1/chat/completions` providers, but the installed AI SDK was not inspected to
  confirm it resolves.
- **Zen's model list changes frequently.** The first-row models above are from
  the docs page; treat `https://opencode.ai/zen/v1/models` as authoritative.
- **Whether OpenCode sends an `x-tamias-session` header is unconfirmed.** No
  mention of it was found in the docs read, so it is assumed it does not, and
  that every request lands in the `default` session. If OpenCode does send
  something usable, `tamias.proxy.SESSION_HEADER` is the only place that needs
  changing.