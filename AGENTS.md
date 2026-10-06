# AGENTS.md — Tamias

**Read `/home/krish/Desktop/HANDOFF-CODEX.md` first, in full. It contains
everything: folder inventory, git topology, the conflict map with decided
resolutions, the task list, the gates, and the rules.**

## Supersession notice

**Ignore `AGENT-RULES.md` in every clone of this project.** It is obsolete: it
was written for a five-parallel-agent phase and forbids merging. Merging is now
the job. Do keep its rules 3–9 (project venv only, never weaken a test, never
`git add -A`, never print secrets, back every claim with command output).

## The three absolute rules

1. **Gate after every step** — and stop on a red suite:
   ```bash
   .venv/bin/python -m pytest -q 2>&1 | tail -5 && .venv/bin/ruff check src tests
   ```
   Baseline: **377 passed, ruff clean** at `a0f6ca1`.
2. **Rollback exists:** `git merge --abort`, or `git reset --hard a0f6ca1`.
3. **Never `git push --all` / `--force` / `--tags`.** Push `master` and an
   explicitly named tag only.

## Today's two goals

1. Faculty demo tomorrow (offline act guaranteed; live act shown only after two
   consecutive passing rehearsals).
2. Publish: GitHub push + TestPyPI tonight, real PyPI after the demo.

## Where you are

Read `HANDOFF-CODEX.md` §19 for the execution order and §C for the report you
must return. Tasks **T-09, T-17, T-19** are the hard ones; the rest are
mechanical and follow §13's resolutions.
