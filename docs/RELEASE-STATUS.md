# Release Status

## Public alpha

| Area | Status | Evidence or limitation |
| --- | --- | --- |
| Local proxy metering | Implemented | Chat, Responses, and Anthropic routes have offline tests |
| Shadow routing | Implemented | Decisions are recorded without rewriting upstream requests |
| Active routing | Disabled | Public alpha accepts only `shadow` and `off` |
| Cost provenance | Implemented | Provider billed cost and list-price computation remain separate |
| Legacy cost rows | Unknown | Ambiguous `cost_usd` is excluded from computed/reconciliation totals |
| Session privacy | Implemented | Per-database salt and legacy session migration |
| SSE parsing | Implemented | Shared assembler and adversarial parser fixtures; live providers unverified |
| Authentication | Not implemented | Keep the service on loopback or add an authenticated front end |
| Live provider behavior | Unverified | CI uses offline/injected transports only |
| Package build | CI-gated | Build and wheel audit run in GitHub Actions |

This document describes implemented repository behavior only. It does not claim
provider compatibility, savings, latency, or live-agent success without a
separate captured rehearsal.
