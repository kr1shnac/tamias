# Company keys

`--auth-token` is one shared secret. It answers "may this caller use the
proxy?" and nothing else: every holder is indistinguishable from every other.
A company needs per-caller identity, so the gateway can also authenticate with
**company keys** minted by `tamias admin key`.

A key can also carry spend limits — a budget, per-model sub-caps, and per-minute
request and token rates — enforced by the gateway itself. Those are described in
[`BUDGET.md`](BUDGET.md); this document covers identity and the model allowlist.

## What a key is

A key is a random token of the form `vk-<8 hex>-<random>`. The part up to the
second hyphen is the public `key_id`; the whole string is the secret. Only the
token's SHA-256 hash is stored, so:

- a copy of the key store does not yield a usable credential;
- `list` and the stored rows never show the secret;
- `issue` prints the token exactly once and it cannot be recovered.

The store is a small SQLite file (default `~/.tamias/keys.db`, or
`$TAMIAS_KEYS_DB`), created with mode `0600`.

A plain SHA-256 — no salt, no stretching — is the right hash here, unlike for a
password: the input is 32 bytes of `secrets` output, so there is no
low-entropy guess to slow down.

## Managing keys

```sh
# Mint a key for alice on the acme project, allowed two models.
tamias admin key issue --label alice --project acme \
    --model openai/gpt-4o-mini --model anthropic/claude-3.5-haiku

# List keys (never their secrets).
tamias admin key list

# Revoke one. The row stays as an audit trail; the key stops authenticating.
tamias admin key revoke vk-1a2b3c4d
```

Then start the gateway with the same store:

```sh
tamias serve --upstream https://openrouter.ai/api \
    --prices prices.toml --db requests.db \
    --keys-db ~/.tamias/keys.db
```

Key authentication is **off unless `--keys-db` is given**. `serve` has no
default key-store path: a server never starts demanding keys merely because a
file happens to exist somewhere.

## What a key controls

| Field | Effect |
| --- | --- |
| identity | `authenticate` resolves the token to a key; unknown or revoked tokens get 401. |
| project | the key's project overrides any client-supplied `x-tamias-project` header or `/p/<name>/` prefix, so a caller cannot relabel its spend. |
| models | an allowlist. Empty means *no restriction*; a non-empty list means the caller may reach only those models. |

`--auth-token` and `--keys-db` may be set together. The shared token keeps
working and carries no identity of its own, so no allowlist applies to it.

## Where the allowlist is enforced

The check is on the model named in the request body, and it runs before the
request is forwarded or logged. It covers every supported provider API, because
all three name the model in a top-level `model` field:

- `POST /v1/chat/completions`
- `POST /v1/messages`
- `POST /v1/responses`
- the catch-all passthrough, when its JSON body names a `model`

A request that names no model (for example `GET /v1/models`) is not gated: there
is no model to check. This is complete for the three supported APIs. A provider
that named its model somewhere other than the body — a path segment, say — would
not be covered, and no such provider is supported.

## Limits

- **Budgets, per-model sub-caps, RPM and TPM are enforced** by the gateway, by
  reserve-then-settle, before a request reaches a provider. See
  [`BUDGET.md`](BUDGET.md) for what is and is not promised.
- Revocation is immediate for new requests; it does not cancel a request already
  in flight.
- Keys are managed on the gateway host by an operator with filesystem access to
  the store. There is no admin API.
