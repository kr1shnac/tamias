# Release Status

## Public alpha (0.1.0a4)

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
| Configuration | Implemented | `~/.tamias/config.toml` (or `$TAMIAS_CONFIG`); provider presets; `tamias config` |
| Log maintenance | Implemented | `tamias prune`; `tamias doctor` size warning |
| Supply chain | CI-gated | `pip-audit --strict`, CycloneDX SBOM artifact, `pip-licenses` report with a copyleft denylist |
| Benchmark | Implemented | `scripts/benchmark_overhead.py`, offline, no socket |
| Live provider behavior | Unverified | CI uses offline/injected transports only |
| Package build | CI-gated | Build, `twine check`, and fresh-venv wheel smoke run in GitHub Actions |
| Publish | Not done | Requires a PyPI pending publisher and explicit approval (see RELEASE-CHECKLIST.md) |

This document describes implemented repository behavior only. It does not claim
provider compatibility, savings, latency, or live-agent success without a
separate captured rehearsal.
