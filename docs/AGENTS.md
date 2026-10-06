# Agent configuration

The commands below print local proxy snippets. Real agent traffic is not
confirmed by this repository; each integration is therefore UNVERIFIED.

## Claude Code — UNVERIFIED

```sh
tamias agent-config claude-code --port 8000 --project example
```

## Codex — UNVERIFIED

```sh
tamias agent-config codex --port 8000 --project example
```

The generated snippet uses Tamias as its local model provider and the Responses
wire API.

## OpenCode — UNVERIFIED

```sh
tamias agent-config opencode --port 8000 --project example
```

`--project example` prefixes the local endpoint with `/p/example`. Use the
same project with `tamias run --project example` when running one agent command
through Tamias.

If an agent's tools return `Error: ...`, set `error_markers` in the router
configuration; see [`examples/router.example.toml`](../examples/router.example.toml).
