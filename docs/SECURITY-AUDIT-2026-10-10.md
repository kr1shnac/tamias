# Security Audit — 2026-10-10

> **Scope note (added 2026-10-20):** parts of this document review the separate,
> now-*parked* `tamias-agent` repository as well as the `tamias` proxy. Where a
> finding concerns `tamias-agent` (for example `run_shell`, a CLI the proxy does
> not contain), it does not apply to this repository and is retained only as a
> record of the joint review. The `tamias`-proxy findings in here stand.

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
| High | Mitigated in the agent working tree | `tamias-agent` tool functions accepted arbitrary paths and executed arbitrary shell strings. Fixed by a workspace-root guard (`resolve_in_workspace`) applied to `read_file`, `write_file`, `edit_file`, `list_directory`, `create_directory` and `search_in_files`, so absolute paths, `../` traversal and out-of-tree symlinks are refused before any file is opened; plus a deliberate shell policy (`TAMIAS_AGENT_ALLOW_SHELL`, default on). Regression tests added. The fix sits in the agent's working tree (that repo carries unrelated uncommitted work); its owner must commit it. |
| Medium | Design constraint | The proxy has no authentication. It is safe only on loopback by default; a non-loopback operator must supply `--allow-non-loopback` and provide a trusted authenticated boundary. |
| Medium | Verification gap | Live provider, external clients, and socket-based subprocess behavior were not exercised in this sandbox. They remain unverified, not assumed secure. |
| Low | Build environment | Rebuilding artifacts requires `hatchling`; it is unavailable locally and dependency download is blocked by the no-network environment. The existing a4 wheel passes metadata and content checks. |

## Evidence

- Focused route/session/store tests: `102 passed`.
- Focused dashboard reconciliation tests: `2 passed`.
- Earlier offline proxy, parser, pricing, router, and report subsets: `151 passed`.
- `ruff check src tests scripts`: clean.
- `pip check`: no broken requirements.
- Agent workspace-confinement tests: `.venv/bin/python -m pytest test_agent.py -q`
  → `64 passed` (includes refusal of absolute paths, `../` traversal and
  out-of-tree symlinks for every file tool, and the shell-disable policy).
- Existing `tamias-0.1.0a4` wheel: `twine check` passed; no agent, TUI, or
  demo files are packaged.

## Required follow-up

The demo agent's workspace escape is fixed in its working tree (see the findings
table). `run_shell` remains inherently powerful — it runs a shell line in the
workspace and is **not** sandboxed; a full sandbox needs an OS/container boundary
outside this project's scope. It defaults on and can be disabled with
`TAMIAS_AGENT_ALLOW_SHELL=0`. The agent's owner should commit the fix, which now
sits alongside unrelated uncommitted work, and run
`.venv/bin/python -m pytest test_agent.py -q` (64 passed here).
