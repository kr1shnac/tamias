# AGENTS.md — Tamias

**Read [`TAMIAS-SSOT.md`](TAMIAS-SSOT.md) first, in full.** It is the single
source of truth for what Tamias is, what is `PROVEN`, and what remains
unvalidated. For any question about what the *code* actually does,
[`docs/audit/claims.md`](docs/audit/claims.md) outranks the SSOT.

Do not cite anything outside a `PROVEN` tag as evidence of savings or quality.
No measured savings exist.

## Rules

1. **Gate after every step** — and stop on a red suite:
   ```bash
   .venv/bin/python -m pytest -q 2>&1 | tail -5 && .venv/bin/ruff check src tests
   ```
   Baseline: **915 passed, ruff clean** (2026-10-19).
2. **Never rewrite the working tree destructively.** Do not run
   `git reset --hard`, `git checkout -- .`, or `git clean -fd` while work is
   uncommitted. Roll back a merge with `git merge --abort`; prefer a new commit
   or `git revert` over discarding history.
3. **Never `git push --all` / `--force` / `--tags`.** Push `master` and an
   explicitly named tag only, and only with the owner's approval.
4. **Project venv only.** There is no bare `python` or `pytest`.
5. **Never weaken a test** to make it pass; fix the code or file the defect.
6. **Never `git add -A`.** Stage the files you changed.
7. **Never print or commit secrets.** Bearer tokens live in `$TAMIAS_AUTH_TOKEN`.
   `demo/config.env` is tracked but holds no credential (spot-checked
   2026-10-19).
8. **Back every claim with command output.** An untagged or uncited statement is
   `UNVERIFIED`.

## Where things are

- **Single source of truth:** `TAMIAS-SSOT.md` (repo root, Design Spec v1.1).
- **Code-truth ledger:** `docs/audit/claims.md`.
- **Build order / roadmap:** `TAMIAS-SSOT.md` §11 and the council verdicts in
  §14.
- **Release state:** `docs/RELEASE-STATUS.md`, `BLOCKED.md`. Tamias is **not yet
  on PyPI**; the newest published artifact is TestPyPI `0.1.0a3`.
- **Legacy, do not treat as authority:** `AGENT-RULES.md` (written for a
  five-parallel-agent phase that forbade merging — obsolete, and it does not
  exist in this checkout) and the external
  `/home/krish/Desktop/HANDOFF-CODEX.md` (an out-of-repo handoff from earlier
  work; its contents are superseded by the SSOT and scheduled for archival, so
  do not follow it for build order or product identity).
