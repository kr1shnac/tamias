# Security Policy

Tamias is an alpha local proxy. Its authentication is **opt-in**, and binding
beyond loopback is a **two-key** act: a wider bind requires *both*

- **authentication** — `serve` requires `--auth-token` (or `$TAMIAS_AUTH_TOKEN`)
  or company keys configured with `--keys-db`; and
- **an explicit acknowledgement** — the `--allow-non-loopback` flag.

Passing authentication alone does **not** lift the refusal, and
`--allow-non-loopback` alone is refused too: without authentication `serve`
will not start on a wider host. The dashboard (`tamias dashboard`) has no
authentication at all, so its `--allow-non-loopback` is the only gate — never
bind it beyond loopback on an untrusted network. On loopback there is no
authentication by default. Keep it bound to loopback unless it is placed behind
a trusted, authenticated network.

Do not commit provider keys, request bodies, prompts, responses, or live logs.
Report security issues privately through the repository security-advisory flow
or the maintainer contact listed in the repository metadata.
