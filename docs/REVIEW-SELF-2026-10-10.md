# Self-review — 2026-10-10

**This is a self-review by the author, not the independent Codex review (CX5/RR).**
It records what was re-checked and what was found. Independent review is still
outstanding before the publish gate is considered met.

## Scope

- Package/install surface (`pyproject.toml`, wheel/sdist contents, version).
- CI workflows (`ci.yml`, `publish.yml`).
- Public docs truthfulness (`README.md`, `docs/RELEASE-STATUS.md`).
- Re-verification of the landed correctness batch C1–C6 and hardening G1–G11.

## What was verified

| Check | Result |
| --- | --- |
| Version single-sourced | `pyproject.toml` `0.1.0a4` = `tamias.__version__` = `tamias --version` |
| Wheel contents | CI asserts every file is under `tamias/` or `.dist-info/`, and that no `agent`/`tui`/`demo` file is packaged |
| sdist excludes | `/demo/config.env` and `/demo/history` excluded (no secrets, no synthetic DB) |
| CI matrix | 3.11–3.14, `pytest` + `ruff`; separate `package`, `audit`, `smoke` jobs |
| Audit job | `pip-audit --strict`; CycloneDX SBOM artifact; `pip-licenses` copyleft denylist |
| Smoke job | Builds a wheel, installs it into a fresh venv, checks `--version` and runs `doctor --port 0` (verified locally: exit 0) |
| Publish guard | Tag `v<version>` must equal the packaged version, else the job fails; OIDC only, no stored token |
| Test suite | `711 passed`, `ruff check src tests scripts` clean |
| Public files | `LICENSE`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` present |

## Findings

1. **Fixed — stale status doc.** `docs/RELEASE-STATUS.md` still claimed
   "Authentication: Not implemented" after G1 added bearer auth. Rewritten to
   match the code (auth opt-in, TLS, config, prune, SBOM/license audit).
2. **Residual — CI never executed.** The repo has no remote, so the workflows
   have not run in Actions. Their logic is reviewed and the smoke step is
   reproduced locally, but greenness in CI is unconfirmed until pushed.
3. **Residual — `run_shell` is not sandboxed.** By design and documented; it can
   be disabled with `TAMIAS_AGENT_ALLOW_SHELL=0`. True sandboxing is out of scope.
4. **Residual — live paths unverified.** Provider calls, external clients, and
   socket-based subprocess behavior use offline/injected transports in tests.

## Verdict

No correctness or packaging defect found in the reviewed surface beyond finding 1,
which is fixed. Findings 2–4 are unverified areas, not defects, and are stated as
such in the docs. Independent review (CX5/RR) should confirm findings 1 and the
CI logic before publishing.
