# Security Policy

Tamias is an alpha local proxy. Its authentication is **opt-in and required
beyond loopback**: `serve` refuses to bind a non-loopback `--host` unless you
pass `--auth-token` (or `$TAMIAS_AUTH_TOKEN`) or configure company keys with
`--keys-db`. The `--allow-non-loopback` flag exists to deliberately acknowledge
that requirement and bind anyway, with no authentication. On loopback there is
no authentication by default. Keep it bound to loopback unless it is placed
behind a trusted, authenticated network.

Do not commit provider keys, request bodies, prompts, responses, or live logs.
Report security issues privately through the repository security-advisory flow
or the maintainer contact listed in the repository metadata.
