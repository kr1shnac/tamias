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

**Delivering the shared token.** `--auth-token VALUE` puts the secret on argv,
where it is visible in `ps`, the shell history and CI logs. Prefer one of:

- `export TAMIAS_AUTH_TOKEN=...` (environment, not on argv); or
- write the token to a `chmod 600` file and set
  `TAMIAS_AUTH_TOKEN_FILE=/path/to/token`; the file's trailing newline is
  stripped.

`serve` prints a warning when it detects `--auth-token` on a command line.
`--keys-db` avoids the shared-secret problem entirely by issuing per-caller
keys (`tamias admin key issue`).

Report security issues privately through the repository security-advisory flow
or the maintainer contact listed in the repository metadata.
