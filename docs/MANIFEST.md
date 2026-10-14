# Signed policy manifests

A company's agent runs under a **use-case policy**: the fence that says which
tools, shells and paths a task may touch. If that policy is a file the employee
edits, the fence is decorative — the agent reads whatever the file says.

A **signed manifest** removes that choice. The policy becomes a JSON document
plus a detached signature, the gateway verifies the signature before it will
serve the policy, and an altered or expired manifest is refused. The gateway
holds only the **public** key, so compromising the gateway does not let it mint
a policy of its own; the private key never leaves the signer.

This document covers the gateway half: signing, verifying, and serving a policy
at `GET /policy`.

## What a manifest is

A manifest is JSON with a canonical byte form — sorted keys, no whitespace,
ASCII — so a signer and a verifier anywhere reproduce the same bytes:

```json
{
  "manifest_version": 1,
  "algorithm": "ed25519",
  "public_key": "<base64 raw Ed25519 public key>",
  "project": "acme",
  "issued_at": "2026-04-01T00:00:00Z",
  "expires": "2099-12-31T00:00:00Z",
  "policy": {"version": "1", "use_case": {"name": "codefix"}, "tools": {...}},
  "signature": "<base64 Ed25519 signature over every field but signature>"
}
```

- `project` may be `""` for a manifest not bound to one project.
- `expires` is **required**: a manifest with no expiry is a policy that can
  never be withdrawn. The expiry instant itself counts as expired.
- `policy` is the parsed policy TOML. It must carry a `[use_case]` table with a
  `name`, the same contract the agent already enforces.

The signature is Ed25519, which needs the optional `cryptography` package:

```sh
pip install 'tamias[manifest]'
```

## Signing a policy

```sh
# 1. Generate a signing keypair. Keep the private key off the gateway.
tamias admin manifest keygen --private-out signing.pem --public-out trust.pem

# 2. Sign a policy TOML, bound to one project and with an expiry.
tamias admin manifest sign --policy policy.toml --key signing.pem \
    --project acme --expires 2099-12-31T00:00:00Z -o manifest.json

# 3. Verify it the way the gateway will — no private key involved.
tamias admin manifest verify manifest.json --key trust.pem --project acme
```

`keygen` writes the private key with mode `0600`. `verify` exits non-zero and
names the failure for any manifest that cannot be trusted: wrong project,
expired, altered, or signed by a different key.

## Serving the policy

```sh
tamias serve --upstream https://your-provider.example --prices prices.toml \
    --keys-db keys.db --manifest manifest.json --manifest-key trust.pem
```

- The gateway **verifies the manifest at startup** and refuses to start if it is
  missing, altered, expired or signed by a key other than `--manifest-key`.
- `--manifest` needs authentication (`--auth-token` or `--keys-db`). Serving a
  policy to an anonymous caller is refused rather than warned about.
- The verified manifest is served at **`GET /policy`**. It is re-verified on
  every request, so a manifest stops being served the moment its expiry passes.
- A manifest bound to one project is served only to a caller the key store has
  placed in that project. The key's project is authoritative, so a client
  cannot relabel its way to another project's policy, and `/p/<project>/policy`
  selects the project the same way the chat routes do.

| Caller | Response |
| --- | --- |
| No manifest configured | `404` |
| Anonymous (auth is on) | `401` |
| Authenticated, wrong project | `403` |
| Authenticated, right project, valid | `200` with the manifest |
| Authenticated, but manifest expired / altered / mis-pinned | `503` |

## What this does not do

- It does **not** force a running agent to obey the policy. The gateway serves a
  verified policy; an agent must fetch it and apply it. An agent left in direct
  mode still reads whatever local policy it is given.
- It has **no revocation list** beyond expiry. Withdrawing a policy means
  re-issuing it with a nearer expiry; there is no online CRL.
- It does not sign the *agent image* or the tool set — only the policy document.
