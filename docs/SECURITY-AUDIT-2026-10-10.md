# Security Audit — 2026-10-10

## Scope

Static review and offline tests of the Tamias proxy at commit `2dfca5a`, plus
read-only review of the separate `tamias-agent` checkout. No credentials,
provider traffic, or environment variables were read.

## Proxy verdict

The local proxy has no known high-severity issue in the reviewed code paths.
It defaults to loopback binding, refuses wider binds without an explicit
acknowledgement, strips routing/session/project metadata before forwarding,
keeps request logs private, and stores only coded transport failures.

Session identifiers use a per-database salt. Direct Responses and Anthropic
route registration now reuses that salt rather than falling back to a
per-process value. Legacy session identifiers are migrated to digests.

Provider-billed cost, computed list-price cost, and legacy cost data remain
separate. A legacy `cost_usd` value is treated as unknown for totals and
reconciliation.

## Findings

| Severity | Status | Finding |
| --- | --- | --- |
| High | Open, separate checkout | `tamias-agent` tool functions accept arbitrary paths and execute arbitrary shell strings. A model can escape the intended workspace and access local files. The checkout has uncommitted user work in `agent.py`, so it was not modified by this audit. |
| Medium | Design constraint | The proxy has no authentication. It is safe only on loopback by default; a non-loopback operator must supply `--allow-non-loopback` and provide a trusted authenticated boundary. |
| Medium | Verification gap | Live provider, external clients, and socket-based subprocess behavior were not exercised in this sandbox. They remain unverified, not assumed secure. |
| Low | Build environment | Rebuilding artifacts requires `hatchling`; it is unavailable locally and dependency download is blocked by the no-network environment. The existing a4 wheel passes metadata and content checks. |

## Evidence

- Focused route/session/store tests: `102 passed`.
- Focused dashboard reconciliation tests: `2 passed`.
- Earlier offline proxy, parser, pricing, router, and report subsets: `151 passed`.
- `ruff check src tests scripts`: clean.
- `pip check`: no broken requirements.
- Existing `tamias-0.1.0a4` wheel: `twine check` passed; no agent, TUI, or
  demo files are packaged.

## Required follow-up

Before claiming the whole project is secure or release-ready, the demo agent
needs a workspace-root path guard, a deliberate command-execution policy, and
tests proving it cannot access paths outside the selected workspace. Those
changes must be made by reconciling its existing uncommitted work first.
