# Release Status

## Public alpha — source `0.1.0a6`; tag pushed to GitHub, not yet on PyPI

`master` and the annotated tag `v0.1.0a6` are pushed to
`github.com/kr1shnac/tamias` (2026-10-19), and CI is green on both. Nothing is on
real PyPI: the tag-triggered publish job fails with `invalid-publisher` until the
owner adds a PyPI pending publisher. TestPyPI's newest is `0.1.0a3` (2026-10-07).

| Area | Status | Evidence or limitation |
| --- | --- | --- |
| Local proxy metering | Implemented | Chat, Responses, and Anthropic routes share one log; offline client-matrix test |
| Shadow routing | Implemented | Decisions recorded without rewriting upstream requests |
| Active routing | Opt-in | `--router-mode active` rewrites the model to `--cheap-model`; refused at startup unless the target is in `--registry`, tool-capable, and priced; a switch the caller's key forbids is suppressed, not refused |
| Cost provenance | Implemented | Provider billed cost and list-price computation remain separate |
| Legacy cost rows | Unknown | Ambiguous `cost_usd` is excluded from computed/reconciliation totals |
| Session privacy | Implemented | Per-database salt and legacy session migration |
| SSE parsing | Implemented | Shared assembler and adversarial parser fixtures; live providers unverified |
| Authentication | Opt-in | `--auth-token` / `$TAMIAS_AUTH_TOKEN`; constant-time bearer check; required for a non-loopback bind |
| Listener hardening | Implemented | Body cap (413) and concurrency limit; optional TLS via `--tls-cert`/`--tls-key` |
| Signed policy manifest | Opt-in | `serve --manifest` verifies an Ed25519-signed policy at startup and serves it at `GET /policy`; expired/altered/mis-pinned manifests are refused (404/401/403/503). Needs `tamias[manifest]`; agent-side application is not enforced by the gateway |
| Configuration | Implemented | `~/.tamias/config.toml` (or `$TAMIAS_CONFIG`); provider presets; `tamias config` |
| Log maintenance | Implemented | `tamias prune`; `tamias doctor` size warning |
| Supply chain | CI-gated | `pip-audit --strict`, CycloneDX SBOM artifact, `pip-licenses` report with a copyleft denylist |
| Benchmark | Implemented | `scripts/benchmark_overhead.py`, offline, no socket |
| Savings measurement | Not attempted | No paid, non-zero-price run has been authorised. The 2026-10-19 EVIDENCE GATE resolves to **Option 3** — the governance/metering layer is v1 — with the caveat "budgets UNTESTED" and no savings claim (`TAMIAS-SSOT.md` §10) |
| Live provider behavior | Unverified | CI uses offline/injected transports only |
| Package build | CI-gated | Build, `twine check`, and fresh-venv wheel smoke run in GitHub Actions |
| Publish | Blocked on owner | `v0.1.0a7` is the source version and builds clean, but the PyPI publish job returns `invalid-publisher`: the owner must add a PyPI pending publisher (`kr1shnac/tamias`, workflow `publish.yml`, environment `pypi`) before it can run |

This document describes implemented repository behavior only. It does not claim
provider compatibility, savings, latency, or live-agent success without a
separate captured rehearsal.
