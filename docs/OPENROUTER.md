# OpenRouter through tamias

Point OpenCode at tamias, and tamias at OpenRouter, to see what a real agent
session costs and what the router would have done with it.

This is the same shape as [OPENCODE.md](OPENCODE.md), which does the same thing
against Zen. Only the upstream and the free-model caveats differ, so read that
one too if you have not.

- **Endpoints cited:** <https://openrouter.ai/docs/api-reference/overview> and
  <https://openrouter.ai/docs/features/limits>
- **OpenCode config shape:** <https://opencode.ai/docs/providers/>
- **Checked against:** `https://openrouter.ai/api/v1/models` on 2026-10-04

## 1. Get an OpenRouter key

Head to <https://openrouter.ai/settings/keys>, click **Create API Key**, and
copy it.

OpenRouter's free `:free` models bill at zero, but the *account* still needs a
key and still counts against the free-tier rate limit. Create a key dedicated to
this test, put a credit limit on it if your plan allows one, and delete it when
you are done.

## 2. Start tamias

```sh
export OPENROUTER_API_KEY=sk-or-...

tamias serve \
  --upstream https://openrouter.ai/api \
  --prices prices.openrouter.toml \
  --db /tmp/or.db \
  --port 8000
```

`--upstream` takes the base **without** the path. tamias appends
`/v1/chat/completions` itself (`proxy.py` builds `upstream_url.rstrip("/") +
CHAT_PATH`), so `https://openrouter.ai/api` becomes
`https://openrouter.ai/api/v1/chat/completions`, which is OpenRouter's
chat-completions endpoint. Passing
`https://openrouter.ai/api/v1/chat/completions` instead would double the path
and 404.

### The key does not go in the URL, a flag or a file

tamias has no `--api-key` flag and stores no credential. The proxy forwards the
client's `Authorization` header to the upstream byte-for-byte, so the key
travels from OpenCode to tamias to OpenRouter and is never read from a
tamias-side setting:

```
OpenCode ──Authorization: Bearer sk-or-…──▶ tamias :8000 ──same header──▶ openrouter.ai
```

This is worth being precise about, because it has a consequence: **if you run
`tamias serve` against OpenRouter, anything that can reach port 8000 can spend
your key.** There is no listener-side authentication. Keep `--host` on
`127.0.0.1` and do not expose the port.

The key never reaches the request log. `tamias` stores only token counts, model
ids, costs and a routing decision. Where a session id has to be derived from the
request, it uses a salted hash of the `Authorization` header, not the header
itself.

## 3. Point OpenCode at the proxy

Add a custom provider. Per-project (`opencode.json` in the project root) or
global (`~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "tamias": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "tamias (local proxy to OpenRouter)",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "{env:OPENROUTER_API_KEY}"
      },
      "models": {
        "nvidia/nemotron-3-ultra-550b-a55b:free": {
          "name": "Nemotron 3 Ultra 550B A55B (free, via tamias)"
        },
        "nvidia/nemotron-3.5-lightning:free": {
          "name": "Nemotron 3.5 Lightning (free, via tamias)"
        }
      }
    }
  },
  "model": "tamias/nvidia/nemotron-3-ultra-550b-a55b:free"
}
```

Every key here is from <https://opencode.ai/docs/providers/> as checked on
2026-10-04, and the same shape appears in that page's Atomic Chat, llama.cpp,
LM Studio, Ollama and Helicone examples:

- `npm: "@ai-sdk/openai-compatible"` is the package for anything that speaks
  `/v1/chat/completions`. (For `/v1/responses` you would need `@ai-sdk/openai`,
  which tamias does not proxy — see the caveat at the end.)
- `options.baseURL` is the endpoint. The AI SDK appends `/chat/completions`, so
  `http://localhost:8000/v1` produces exactly the path tamias answers on.
- `options.apiKey` takes `{env:VAR}` substitution. **Never paste the key here**;
  an unset variable becomes an empty string and a pasted one is a secret sitting
  in a file. `/connect` → **Other** → provider id `tamias` stores it in
  `~/.local/share/opencode/auth.json` instead.
- `models` maps the id OpenCode puts in the request body to a display name.
  **These ids are what tamias logs as `model_requested`, so they must match your
  price sheet keys character for character**, `:` and `/` included. That is why
  the keys in `prices.openrouter.toml` are quoted: TOML bare keys cannot contain
  `/` or `:`.
- `model` is `"<provider-id>/<model-id>"`.

## 4. Write the price sheet

Copy `prices.openrouter.toml`. Its three model tables already carry **every rate
at 0**, including `cache_write`, because the `:free` models bill at zero and
because OpenRouter is an OpenAI-style upstream whose `usage` object has no
cache-write count.

That second point is the one to internalise. If `cache_write` were nonzero,
every request would come out UNKNOWN, because `cache_write_tokens` is always
UNKNOWN on that path and a nonzero rate is then a rate you needed and did not
have. A real `0` is a price you stated; a missing rate is one you do not know,
and tamias keeps those apart. See [When cost is
UNKNOWN](OPENCODE.md#when-cost-is-unknown).

`prices.openrouter-sim.toml` prices those same models at **invented** rates
(3.00/15.00 for the ultra model, 0.25/1.25 for lightning) under the real model
ids, quoted, so a rehearsal can price a real log. It carries `simulated = true`,
which stamps SIMULATED PRICES, NOT REAL SAVINGS on every amount. Its numbers are
made up and must never be quoted as what these models cost.

## 5. Run the live check

```sh
python scripts/live_check.py \
  --proxy http://localhost:8000 \
  --model nvidia/nemotron-3-ultra-550b-a55b:free \
  --api-key-env OPENROUTER_API_KEY \
  --db /tmp/or.db
```

`--api-key-env` names the environment variable holding the key; it defaults to
`ZEN_API_KEY`. Only the variable's *name* is ever printed, and every output line
passes through a redactor before it is shown. Prove the script itself first with
`python scripts/live_check.py --mock`, which needs no key and opens no socket.

## Do not use `openrouter/free`

OpenRouter publishes a router at `openrouter/free`. Its own description, from
`GET /v1/models`:

> The simplest way to get free inference. openrouter/free is a router that
> selects free models at random from the models available on OpenRouter.

**It picks a different model per request.** That breaks every claim tamias
makes. `model_requested` in the log would be the router, not what actually ran;
`model_used` could not be compared against the sheet; a SWITCH decision would
have no fixed target; and a saving computed between two requests would be
dividing by two unrelated models. Pin explicit ids — the `:free` suffix on a
named model, as in `prices.openrouter.toml`.

## Streaming and usage: `--inject-usage`

OpenCode streams, and its requests do not carry
`stream_options.include_usage`. So does tamias: the default is to forward the
body byte-for-byte, which means a streamed request gets no usage chunk, its token
counts are UNKNOWN, and `tamias report` prints `estimated saving: UNKNOWN (0 of
n requests have token counts)` rather than a number.

OpenRouter muddies this. It has been observed sending a usage chunk on a stream
that never opted in, so some of your rows may carry counts and some may not,
with nothing in the request to predict which. That is the upstream's choice, not
something tamias did.

Pass `--inject-usage` to stop guessing:

```sh
tamias serve \
  --upstream https://openrouter.ai/api \
  --prices prices.openrouter.toml \
  --db /tmp/or.db \
  --port 8000 \
  --inject-usage
```

**It is off by default, and off means byte-for-byte.** With the flag on, and only
then, a streaming request body is re-encoded to set
`stream_options.include_usage = true`. Any other `stream_options` key the client
sent is kept, and every other field — `model`, `messages`, `temperature`, `seed` —
is untouched. A non-streaming request is not modified at all.

Turn it on when you want a cost for every streamed request and would rather ask
for the counts than accept UNKNOWN. Leave it off when the forwarded bytes have to
be provably what the client sent.

**The cost is one extra chunk.** The upstream appends a final chunk carrying a
`usage` object and an **empty `choices` array**, after all the content chunks.
Clients that read chunks in order are fine; a client that assumes every chunk has
a choice is not. OpenCode's `@ai-sdk/openai-compatible` path handles it — that is
verified, see `docs/live-evidence.md`.

## Rate limits

Free models are the most rate-limited thing on OpenRouter, and limits are per
account and change without notice; check
<https://openrouter.ai/docs/features/limits> for current numbers. Expect HTTP
429 under a loop, especially with the free tier's low per-model request caps.

If you hit a 429: wait 30 s and retry once. If it repeats, stop. Do not retry in
a tight loop — that is how a free tier turns into a suspended account. A 429 is
also worth reading as a finding: `tamias` logs the upstream status it relayed, so
the report shows the throttling rather than hiding it.

## Caveats

- **Only `/v1/chat/completions` is proxied and logged.** OpenRouter also serves
  `/v1/responses` and `/v1/messages`. A client that uses `/v1/responses` hits
  tamias's catch-all relay: the request is forwarded and the response returned,
  but **no row is written and no cost is computed**. So `model` and `npm` above
  matter — `@ai-sdk/openai-compatible` (chat-completions) works,
  `@ai-sdk/openai` (responses) does not.
- **I have not run any of this against OpenRouter.** No key was available while
  this was written, so no request in this document has been observed. The
  endpoint shape is read off `proxy.py` and the config shape off the OpenCode
  docs; the model ids and the `openrouter/free` description are quoted from a
  real `GET https://openrouter.ai/api/v1/models`. Anything that has actually been
  run against the live API is recorded in `docs/live-evidence.md`, once there is
  a run to record.
- **The model list moves.** `:free` models get added and retired. If a request
  404s or the model stops appearing in `/v1/models`, the fix is a new id, not a
  new price sheet.
