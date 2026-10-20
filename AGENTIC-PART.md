# Agentic Part — Single Source of Truth

**This file is the authoritative guide for coding agents (OpenCode, Codex, Claude Code)
using Tamias as a transparent, OpenAI-compatible request-rewriting proxy.**

It is intended to be the **first and primary reference** that an agent reads when
integrating with Tamias. Keep this file updated whenever the proxy behaviour,
CLI flags, or configuration changes.

---

## 1. Quickstart for Agents

Point your agent's base URL at `http://127.0.0.1:8000` instead of the model
provider. All traffic flows through Tamias, which records token usage and cost per
request.

### Install (from a fresh checkout)

```sh
git clone https://github.com/kr1shnac/tamias
cd tamias
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install .                       # installs `tamias` command
tamias --version                    # prints: tamias 0.1.0a7
```

> **Never** run `pip install tamias` from PyPI — the package is not published
> there today. Always install from this checkout.

### Start the proxy

```sh
tamias serve --upstream https://your-provider.example --prices prices.toml --db requests.db
```

Your agent should now send requests to `http://127.0.0.1:8000` instead of the
provider directly.

---

## 2. Agent Configuration

Run `tamias agent-config` to print a ready-to-paste configuration snippet for a
given coding agent.

```sh
tamias agent-config claude-code --port 8000 --project example
tamias agent-config codex --port 8000 --project example
tamias agent-config opencode --port 8000 --project example
```

### Flags

| Flag | Meaning |
|------|---------|
| `--port` | Port for the local proxy (default: 8000) |
| `--project` | Scopes the local endpoint under `/p/<project>`; use the same project with `tamias run --project example` |

### Example outputs

```sh
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:9/p/p/v1

# Codex
base_url = "http://127.0.0.1:9/p/p/v1"
wire_api = "responses"

# OpenCode
"baseURL":"http://127.0.0.1:9/p/p/v1"
```

---

## 3. Project scoping

The `--project` flag prefixes the local endpoint under `/p/<project>`:

```sh
tamias run --project example your-agent
tamias serve --project example --prices prices.toml --db requests.db
```

Use the same project name when running the agent command through Tamias.

---

## 4. Router modes and error handling

Tamias has three routing modes. The default is `shadow`, which records the
decision it WOULD make without changing the request.

- **`off`** — forward unchanged, record no decision
- **`shadow`** (default) — record the decision it WOULD make, forward unchanged
- **`active`** — rewrite the request to `--cheap-model`; opt-in and fail-closed:
  the process refuses to start unless the target is named in `--registry`,
  tool-capable there and priced by the sheet, and a switch the caller's key does
  not allow is suppressed rather than refused.

### If an agent's tools return `Error: ...`

Set `error_markers` in the router configuration (see
`examples/router.example.toml`) to configure how Tamias handles errors.

---

## 5. What the agent should NOT do

- **Do not** proxy arbitrary web traffic — only the three routes above are
  metered; everything else is passed through counted-only.
- **Do not** store prompt or response text, and **do not** log credentials.
- **Do not** authenticate with the upstream provider — Tamias forwards the
  credentials the client already sends.
- **Do not** rewrite provider requests unless `--router-mode active` is given and
  the target passes every startup gate.

---

## 6. Sources of truth (hierarchy)

When questions arise about what Tamias *is* and what is claimed, follow this
hierarchy:

1. **`TAMIAS-SSOT.md`** — the single source of truth for what Tamias *is* and
   what is `PROVEN`. Outranks every other document.
2. **`docs/audit/claims.md`** — the code-truth ledger. Outranks the SSOT for
   behaviour: what the code actually does.
3. **`docs/ROUTES.md`** — every HTTP route Tamias serves and whether it rewrites,
   meters or passes through.
4. **`docs/PRICING.md`** — how token counts become a dollar figure, and when the
   answer is UNKNOWN.
5. **`docs/RELEASE-STATUS.md`** — what is built, what is published, and what is
   blocked.

> **Never** cite a dollar savings figure as a measurement — all dollar figures
> in this project are hypothetical (no measured savings exist).

---

## 7. Agent integration checklist

Before declaring an agent integrated with Tamias, verify:

- [ ] Agent base URL is `http://127.0.0.1:8000` (or the configured proxy port)
- [ ] `tamias serve` is running with `--prices` and `--db`
- [ ] `tamias doctor --prices prices.toml` reports no blocking checks failed
- [ ] The agent uses `tamias agent-config <agent-name>` for the correct `--project`
- [ ] Error markers are configured if the agent expects error handling
- [ ] The agent does not rely on measured savings — all figures are hypothetical
- [ ] The agent respects router mode: default `shadow` does not rewrite, `active`
  is opt-in and fail-closed

---

## 7. Links made absolute for PyPI rendering

All relative links in this file are written to resolve both on GitHub and, where
possible, on PyPI-rendered Markdown. If a link points to a document within this
repo, prefer an absolute reference beginning with `/` or a full path.

---

## Version

**Current:** `0.1.0a7` (2026-10-20)

**Updated:** whenever `tamias --version` changes, or whenever the proxy behaviour,
CLI flags, or configuration changes in a way that affects an agent's first contact.

---

*This file is the **single source of truth** for agents. Keep it updated whenever
the proxy behaviour, CLI flags, or configuration changes. For the project's
authoritative Design Spec, read `TAMIAS-SSOT.md`. For the code-truth ledger,
read `docs/audit/claims.md`.*