# Tamias — Read-only audit

Date: 2026-10-05. Repo: `/home/krish/Desktop/tamias` @ `52c2e80` (master).
Read-only: no existing file was edited. Only this file was created.
No network was used. No secret was read, printed, or expanded.

**Labels used throughout:** `VERIFIED` = I ran a command in this session and saw the
output (the command is given). `READ` = read from code/doc at `file:line`. `ASSUMED`
= inference, not confirmed.

**Note on AGENT-RULES.md:** it does not exist in this repo. Identical copies
(md5 `2951cb778227ca6126440c17e24d2d59`) exist in the five sibling worktrees
`~/Desktop/tamias-w{1..5}`. I read `~/Desktop/tamias-w1/AGENT-RULES.md` and obeyed
it (venv-only commands, no secrets, no branch switching, claims backed by output).
Its rules 4/5/9 (edit + test + commit) are superseded by the read-only scope of
this task, so nothing was edited and nothing was committed.

---

## 1. Inventory

### 1.1 Git

`VERIFIED` — `git log --oneline -15`:

```
52c2e80 demo: baseline arm injects usage so its cost is measurable
41c371a style: ruff format after merges
bd2a009 tests: hardening (xfail marks 3 real bugs)
f40f2f5 demo: task, runner, model picker, runbook
08151eb feat: local dashboard
91be303 style: format realised-saving tests
66ac826 tests: hardening (xfail marks real bugs)
e5d7e53 feat: local dashboard
0d63f2a demo: task, arm runner, model picker, runbook
d1a61e4 tests: realised saving line
20ed80f report: add realised saving line and 5 new tests
62f694b Add realised saving line and 5 new tests
8728935 Add realised saving line to tamias report
cb0e545 Keep audit docs: HOW-IT-WORKS and claims
3442e43 live evidence: --inject-usage runs, coverage limit, active-mode saving gap
```

`VERIFIED` — `git tag`: `v0.1.0a1` (the only tag).

`VERIFIED` — `git branch -a`:

```
+ feat/dashboard   + feat/demo   + feat/effort   + feat/hardening
+ fix/saving-report  * master     wip/mixed-snapshot
```

`VERIFIED` — `git worktree list`:

```
/home/krish/Desktop/tamias    52c2e80 [master]
/home/krish/Desktop/tamias-w1 d1a61e4 [fix/saving-report]
/home/krish/Desktop/tamias-w2 66ac826 [feat/hardening]
/home/krish/Desktop/tamias-w3 5e6701b [feat/effort]
/home/krish/Desktop/tamias-w4 e5d7e53 [feat/dashboard]
/home/krish/Desktop/tamias-w5 0d63f2a [feat/demo]
```

`VERIFIED` — `git status --short`: one untracked file, `docs/TAMIAS-FINAL-ARCHITECTURE.md`.
The architecture document that this audit is measured against is **not committed**.

`VERIFIED` — ahead/behind vs master (`git rev-list --count`):

| branch | ahead | behind | note |
|---|---|---|---|
| feat/dashboard | 0 | 9 | fully merged |
| feat/demo | 0 | 9 | fully merged |
| feat/hardening | 0 | 9 | fully merged |
| fix/saving-report | 0 | 10 | fully merged |
| feat/effort | 1 | 10 | **unmerged**: `5e6701b feat: optional reasoning-effort switching` |
| wip/mixed-snapshot | 1 | 10 | **unmerged**: `21fe8d0 WIP snapshot: five agents' mixed work (never merge this)` |

`VERIFIED` — `git ls-tree --name-only feat/effort src/tamias/`: contains
`src/tamias/effort.py`, which does **not** exist on master. `src/tamias/effort.py`
is absent from the working tree (only a stale `__pycache__/effort.cpython-314.pyc`
remains — `VERIFIED` via `ls src/tamias/*.py`).

### 1.2 File tree and line counts

`VERIFIED` — `wc -l`:

`src/tamias/` — 3 116 lines total
```
 471 anthropic_adapter.py
 435 cli.py
1058 dashboard.py
   0 __init__.py
 291 pricing.py
 525 proxy.py
 135 router.py
 133 store.py
  68 types.py
```

`demo/` — 1 285 lines total
```
 346 run_arm.sh        237 RUNBOOK.md      236 pick_models.py
 230 make_task.py      185 dry_run_client.py
  26 config.env         25 config.env.example
   0 TASK_PROMPT.txt   <-- EMPTY
```

`tests/` — 6 972 lines total, 17 files. Largest: `test_dashboard.py` 840,
`test_anthropic.py` 735, `test_proxy.py` 705, `test_cli.py` 516,
`test_e2e.py` 537, `test_report_realised_saving.py` 354, `test_hardening_proxy.py` 648,
`test_hardening_router.py` 354, `test_hardening_pricing.py` 389,
`test_hardening_privacy.py` 356, `test_demo_tools.py` 277, `test_store.py` 243,
`test_router.py` 213, `mock_upstream.py` 254, `mock_anthropic_upstream.py` 200,
`test_session.py` 180, `test_pricing.py` 171.

`docs/` + `scripts/`: `TAMIAS-FINAL-ARCHITECTURE.md` 2225 (untracked),
`live-evidence.md` 324, `OPENCODE.md` 263, `OPENROUTER.md` 245, `RUNBOOK.md` (demo) 237,
`audit/hardening-findings.md` 211, `LIVE_CHECK.md` 182, `scripts/live_check.py` 580,
`DASHBOARD.md` 123, `audit/HOW-IT-WORKS.md` 86, `audit/claims.md` 61.

`VERIFIED` — `demo/TASK_PROMPT.txt` is 0 bytes. `run_arm.sh:176` then dies with
`run_arm: prompt.txt is empty`. **Every live arm run is currently blocked.**
(The generated prompt lives at `/tmp/tamias-demo-task/prompt.txt`, `run_arm.sh:167-175`,
built by replacing `{PYTEST}` in a template that is itself empty.)

### 1.3 CLI surface

`VERIFIED` — `tamias --help`:

```
usage: tamias [-h] {serve,report} ...
positional arguments:
  {serve,report}
    serve         run the proxy in front of an upstream
    report        summarise cost and shadow routing
```

`VERIFIED` — `tamias doctor --help`:

```
tamias: error: argument command: invalid choice: 'doctor' (choose from serve, report)
```

`VERIFIED` — `tamias serve --help` flags: `--upstream --prices --db --port --host
--router-mode{shadow,active,off} --cheap-model --strong-model --min-gap
--inject-usage --verbose`. **There is no `--effort-policy`.**

`VERIFIED` — `tamias report --help`: `--db`, `--prices` only. No `--last`,
`--session`, `--since`.

### 1.4 Tests and lint

`VERIFIED` — `.venv/bin/pytest -q`:

```
302 passed, 5 xfailed, 1 warning in 6.36s
```
(the 5 xfails are 3 parametrised cases of one BUG-3 test plus BUG-1 and BUG-2.)

`VERIFIED` — `.venv/bin/ruff check .`: `All checks passed!`

---

## 2. Component status

### 2.1 Observer (Claude Code log parser + ledger + report/receipt/doctor)

| Item | Status | Evidence |
|---|---|---|
| Claude Code JSONL parser | **MISSING** | `VERIFIED` — case-insensitive grep for `ledger` across `src/**/*.py` → NONE. No `observer`, `scan`, `parser` module exists (`VERIFIED` — `ls src/tamias/*.py`: 9 modules, none of them an observer). |
| Dedup on message+request id | **MISSING** | `VERIFIED` — grep `dedup` in `src/` → no hits. `store.py:20` is a single append-only `requests` table; no uniqueness constraint beyond `id INTEGER PRIMARY KEY AUTOINCREMENT` (`store.py:42`). |
| Canonical ledger fields (model source, effort + how known, 5m/1h cache buckets, service tier) | **PARTIAL / mostly MISSING** | `READ` `types.py:14-25` `Usage` has `input_tokens, output_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens`. `store.py:22-38` `COLUMNS` has **no** `model_source`, **no** `effort_used`/`effort_requested`, **no** `service_tier`, **no** separate 5m/1h columns (the 1h count is never persisted). Model source is implied only by `model_requested`/`model_used`. |
| Observed rebuild event detector | **MISSING** | `VERIFIED` — grep `rebuild`, `WARM`, `COLD`, `REBUILT` in `src/**/*.py`: the only `rebuild` hit is the phrase "ignores cache rebuild cost" in `cli.py:28`. `WARM`/`COLD`/`REBUILT` → NONE. |
| Raw flags + normalized cause with UNKNOWN | **MISSING** | `VERIFIED` — no `cause`, no `flag` identifiers in `src/`. `types.py:1-9` states the None-means-UNKNOWN convention, but there is no event type. |
| Typed evidence (two hierarchies: event-cause + semantics) | **MISSING** | `VERIFIED` — grep `evidence` in `src/**/*.py` → NONE. |
| Cache state WARM/COLD/REBUILT/UNKNOWN | **MISSING** | `VERIFIED` — grep → NONE. `store.py` has no cache-state column. |
| Rebuild-size estimate vs cache-write size | **MISSING** | `VERIFIED` — only `cache_write_tokens` exists (`store.py:30`), and `pricing.py:80-81` collapses 1h into the 5m default. No estimate machinery. |
| Dated price sheets | **IMPLEMENTED** | `READ` `pricing.py:45-60` requires a top-level `date` string, else `ValueError`. Five sheets in repo root, all `date = "2026-10-04"`. |
| Historical repricing | **PARTIAL** | `READ` — one `date` per sheet, one `PriceSheet` per invocation; `cli.py:174` loads exactly one sheet. There is no sheet version chain, no "reprice an old log at today's rates" command. Sheets are dated but history is not machine-usable. |
| Ranked rebuild report | **MISSING** | `VERIFIED` — `cli.py:219-271` prints requests / total cost / switched / estimated saving / realised saving / cheap model / sheet. No ranking, no top-N exposure, no evidence state, no unknowns/anomalies section required by `docs/TAMIAS-FINAL-ARCHITECTURE.md:986-996`. |
| Receipt (`tamias receipt <session> [--event N]`) | **MISSING** | `VERIFIED` — `tamias --help` has no `receipt`; grep `receipt` in `src/` → NONE. Spec at `docs/TAMIAS-FINAL-ARCHITECTURE.md:449-477, 997-1002`. |
| Doctor (`tamias doctor`) | **MISSING** | `VERIFIED` — `tamias doctor --help` → `invalid choice: 'doctor'`. Spec at `docs/TAMIAS-FINAL-ARCHITECTURE.md:1003-1016` (9 checks). |
| No prompt/code text persisted | **IMPLEMENTED** | `READ` `store.py:1-10` (module docstring states no text is ever accepted) and `store.py:22-38` — the 15-column schema has no free-text field. `VERIFIED` — the real `requests` table in `demo/runs/routed-2.db` has exactly those columns; `demo/dry_run_client.py` output rows carry no text. `tests/test_hardening_privacy.py` (356 lines) covers this. |

### 2.2 Semantics registry / probe / effort-took-effect

| Item | Status | Evidence |
|---|---|---|
| Provider/client semantics registry | **MISSING** | `VERIFIED` — no registry module; grep `registry`/`semantics` in `src/**/*.py` → no registry. Spec at `docs/TAMIAS-FINAL-ARCHITECTURE.md:893-947` (keyed by provider+client+version+date). |
| Opt-in probe command | **MISSING** | `VERIFIED` — no `probe` in `src/`, not in `tamias --help`. Spec `docs/TAMIAS-FINAL-ARCHITECTURE.md:948-967, 1017-1028`. |
| Effort-took-effect check | **MISSING** | `VERIFIED` — grep `effort\|reasoning` in `src/**/*.py` returns only `dashboard.py:52,53,93,553,567` — i.e. the dashboard's *display* columns. Nothing reads, forwards, plumbs or compares an effort parameter. |

### 2.3 Lab (matched branching, statistics)

Every Lab item is **MISSING**. `VERIFIED` — grep for `snapshot`, `scorer`,
`bootstrap`, `holm`/`Holm`, `censor`, `checkpoint`, `sealed` across
`src/**/*.py` → NONE for all of them. There is no `lab/` package, no experiment
runner, no statistics module. Spec spans `docs/TAMIAS-FINAL-ARCHITECTURE.md:1029-1585`
(E0 `:1169`, E1 `:1205`, E2 `:1233`, E3 `:1262`, causal decomposition `:1454`,
statistics `:1518`).

| Item | Status | Notes |
|---|---|---|
| Harness | MISSING | no code |
| Snapshot / restore | MISSING | no code |
| Checkpoint at request 12 | MISSING | no code; rationale at `docs/...:1318-1323` |
| STAY / EFFORT_ONLY / MODEL_ONLY / BOTH | MISSING | `router.py:52` has `action: Literal["STAY","SWITCH"]` only |
| ≥3 branches per arm | MISSING | no runner |
| Branch validity and censoring | MISSING | no code |
| Sealed scorer | MISSING | no code |
| E0/E1/E2/E3 | MISSING | no code |
| Paired task-level bootstrap | MISSING | no code |
| Holm correction | MISSING | no code |
| `dC = T_cache + T_handoff + dC_behavior` | MISSING | `VERIFIED` — `cli.py:153-165` `_saving()` computes only `max(0, actual - alternative)`, a two-point difference with no cache-rebuild term. `cli.py:28` admits it: `ESTIMATE_LABEL = "estimate; ignores cache rebuild cost; not measured"`. |

**What the repo actually has** as the nearest thing to a Lab: `demo/run_arm.sh`
runs one arm N times sequentially (`run_arm.sh:60-80`), writes
`demo/runs/<arm>-<n>.{db,json}` (`:320-345`), and records
`{arm, n, dry_run, tests_passed, wall_seconds, models, price_sheet, db}`.
That is a repeatable single-condition runner, not matched branching.

### 2.4 Controller

| Item | Status | Evidence |
|---|---|---|
| Gain `G(s,a)` | **MISSING** | `VERIFIED` — grep `controller`, `gain` in `src/**/*.py` → NONE. Spec `docs/...:1590-1653` (state, action, policy). |
| Actions STAY / SET_CONFIG | **MISSING** | `types.py:52` `action: Literal["STAY","SWITCH"]`. No SET_CONFIG. |
| Hysteresis | **PARTIAL, and it is the router's, not a controller's** | `READ` `router.py:29` `min_gap: int = 3`; `router.py:123-130` computes `gap` and returns STAY while `gap < min_gap`. This is a fixed-count dampener on a rule-based switch, not a learned/stabilised controller. |
| Held-out gate | **MISSING** | `VERIFIED` — grep `held`, `gate` in `src/**/*.py` → no gate. Spec `docs/...:1654-1669`. |

**Honest reading:** the architecture's Controller is *conditional* — the document
itself defers it to "future" (`docs/TAMIAS-FINAL-ARCHITECTURE.md:339-346
## 4.2 Future controller`). Its absence on master is consistent with the
document, not a defect. The `hysteresis` in `router.py` is not a substitute.

### 2.5 Prototype: proxy, router, plumbing, dashboard

| Item | Status | Evidence |
|---|---|---|
| `/v1/chat/completions` route | **IMPLEMENTED** | `READ` `proxy.py:53` `CHAT_PATH = "/v1/chat/completions"`, `proxy.py:402` `@app.post(CHAT_PATH)`. Streamed and buffered. |
| Anthropic `POST /v1/messages` | **IMPLEMENTED** | `READ` `anthropic_adapter.py:54` `MESSAGES_PATH`, `:266` `register_routes`, `:417` `async def messages`. Both routes share one log (`proxy.py:523`). |
| `/v1/responses` | **MISSING (relayed unlogged)** | `VERIFIED` — grep `v1/responses` / `responses` in `src/tamias/proxy.py` and `anthropic_adapter.py` → no handler. It is swallowed by `proxy.py:502-503` `@app.api_route("/{path:path}", methods=PASSTHROUGH_METHODS)`. `READ` `anthropic_adapter.py:249-254` states the design intent explicitly: *"relayed, but never logged."* |
| Shadow vs active | **IMPLEMENTED** | `READ` `types.py:47-55` `Decision.persisted_only`; `cli.py:29,375-380` `--router-mode {shadow,active,off}`; help text says shadow records, active applies. `VERIFIED` — `demo/runs/routed-2.db` has 2 rows where `model_used != model_requested`, `baseline-*.db` have 0, which is the signature of active vs shadow. |
| Router v1 (rule-based, on master) | **IMPLEMENTED** | `READ` `router.py:84-135` — 4 ordered rules, pure function, no module state (`router.py:1-7`). 135 lines. |
| Router v2 (reasoning-effort switching) | **NOT MERGED** | `VERIFIED` — `git rev-list --count master..feat/effort` = 1; `git ls-tree feat/effort src/tamias/` contains `effort.py`; master has no `effort.py` and `tamias serve --help` has no `--effort-policy`. It is 10 commits behind master, so a merge is not trivial. |
| Effort plumbing (grep `effort`, `reasoning`) | **MISSING** | `VERIFIED` — the only hits in `src/` are `dashboard.py:52,53,93,553,567`, which read `effort_requested`/`effort_used` columns that **no code ever writes** (`VERIFIED` — `store.py:22-38` has no such column). The dashboard would silently show blank forever. |
| `inject_usage` | **IMPLEMENTED** | `READ` `cli.py:283,397-402`; help text: "Adds one final chunk with usage and empty choices". `run_arm.sh:212,218` passes `--inject-usage` to **both** arms. |
| Dashboard | **IMPLEMENTED** | `VERIFIED` — ran it against recorded runs (full output in §6.2): serves, prints the simulated banner, `/api/summary` returns correct totals. 1058 lines, self-contained page, no CDN (`docs/DASHBOARD.md:8-10`). |
| Console / run control | **PARTIAL** | There is no `tamias console` and no in-process run control. `VERIFIED` — `tamias --help` shows only `serve` and `report`. In-page Replay/Play/Pause/Restart/speed controls exist in the dashboard's JS (`docs/DASHBOARD.md:29-32`). Live agent execution is delegated to an external `opencode run --auto` (`run_arm.sh:290`), so tamias cannot start, cap or observe an arm by itself. |
| To-do task | **BROKEN** | `VERIFIED` — `demo/TASK_PROMPT.txt` is 0 bytes; `run_arm.sh:176` `die "prompt.txt is empty"`. The task generator `demo/make_task.py` (230 lines) creates the buggy sources, and `run_arm.sh:299` runs their pytest, but the agent is handed an empty prompt. |

---

## 3. Data truth

All five `demo/runs/*.db` were opened with `sqlite3.connect("file:...?mode=ro", uri=True)`
(`VERIFIED` — script `/tmp/opencode/audit_db.py`; md5 of every `.db` recorded before
and after the audit, **identical**, so nothing was written).

### 3.1 Per-run facts

| db | rows | rows with tokens | priced | total cost_usd | price_sheet_date | rewritten (`model_used != model_requested`) | statuses | `.json` `tests_passed` | `.json` `dry_run` |
|---|---|---|---|---|---|---|---|---|---|
| baseline-1.db | 12 | 12 | 12 | 0.0015588 | 2026-10-04 | 0 | 200 | false | true |
| baseline-2.db | 7 | 7 | 7 | 0.180645 | 2026-10-04 | 0 | 200 | true | false |
| baseline-3.db | 4 | 4 | 4 | 0.0830580 | 2026-10-04 | 0 | 200 | true | false |
| routed-1.db | 12 | 12 | 12 | 0.00120162 | 2026-10-04 | 3 | 200 | false | true |
| routed-2.db | 10 | 10 | 10 | 0.23983187 | 2026-10-04 | 2 | 200 | true | false |
| routed-3.db | **MISSING** | — | — | — | — | — | — | false | false |

`VERIFIED` — no row in any database has a NULL `cost_usd`; `rows with tokens == rows
== priced` in all five. Every row is status 200. Decision mix:
baseline-1 STAY 7 / SWITCH 5; baseline-2 STAY 5 / SWITCH 2; baseline-3 STAY 4 / SWITCH 0;
routed-1 STAY 9 / SWITCH 3; routed-2 STAY 8 / SWITCH 2.

`VERIFIED` — model ids in every row are free-tier OpenRouter models:
`nvidia/nemotron-3-ultra-550b-a55b:free` and `nvidia/nemotron-3.5-lightning:free`.

`VERIFIED` — all five `.json` sidecars name `"price_sheet": "prices.openrouter-sim.toml"`,
which declares `simulated = true` and whose own header comment says:
*"THESE PRICES ARE INVENTED, NOT REAL... The models below are real free OpenRouter models
and really do cost $0.00, so nothing here should ever be read as what they cost."*

### 3.2 The two direct answers

**Does ANY run use a paid model with real billed cost?**
**No.** `VERIFIED` — the only two model ids appearing in any `.db` are the two
`:free` ids; the only price sheet any of them was priced against is
`prices.openrouter-sim.toml`, which is explicitly invented. So `$0.18` for
baseline-2 and `$0.24` for routed-2 are **not bills** — they are arithmetic over
real token counts multiplied by made-up rates. The real, published rate for those
models is 0 (`prices.openrouter.toml:1-18`). Nobody has paid anything and nobody
has measured a saving.

**Does the store keep provider-reported cost?**
**No.** `VERIFIED` — `grep -rn "usage\.cost|native_cost|cost_usd|\"cost\"" src/**/*.py`
finds only: `store.py:31,51` (`cost_usd` column) and `cli.py:35`
(`COST_COLUMNS = ("cost_usd","cost","usd")`). `proxy.py:103-121` `to_usage(raw)`
reads token fields only; `anthropic_adapter.py:110` `usage_from_anthropic(raw)`
likewise. There is no column, no field and no read path for OpenRouter's own
`usage.cost` / `usage.cost_details`. The single `cost_usd` is **always** a
tamias-computed figure from a local price sheet.

### 3.3 Two run-integrity problems

1. **`routed-3` is a partial run.** `VERIFIED` — `demo/runs/routed-3.json` exists
   (`tests_passed: false`, `wall_seconds: 86`, `dry_run: false`, pointing at
   `demo/runs/routed-3.db`) but neither `routed-3.db` nor `routed-3.serve.log`
   exists on disk. The run reached step 6 of `run_arm.sh` (which writes the JSON
   at `:320`) yet left no database. The arm cannot be inspected at all.
2. **Timestamps disagree with the recorded order.** `VERIFIED` (`ls -la demo/runs/`):
   `baseline-3.json` is 11:20 but `baseline-3.db` is 11:23 — the log is written
   *after* its own sidecar, whereas `run_arm.sh` writes the JSON last (`:320`).
   Same shape for `routed-1` (json 11:04, db 11:04) and `baseline-1` (both 11:02).
   `ASSUMED` — likely a later run reused an arm path; the script would otherwise
   not produce this. Worth checking before quoting any run as "the" recorded run.
3. **The recorded runs are untracked.** `VERIFIED` — `.gitignore:12` contains
   `demo/runs/`, and `git ls-files demo/runs` returns nothing. Every number in
   §3 lives only on this laptop. A demo on another machine has no runs to show.

---

## 4. Honesty leaks

### 4.1 LEAK-1 (high) — stored cost and banner come from different price sheets

`VERIFIED`, reproduced:

```
$ .venv/bin/tamias report --db demo/runs/routed-2.db --prices prices.openrouter-sim.toml
total cost: $0.239832  [SIMULATED PRICES, NOT REAL SAVINGS]

$ .venv/bin/tamias report --db demo/runs/routed-2.db --prices prices.openrouter.toml
total cost: $0.239832                       <-- no banner
```

`READ` — `cli.py:192` `cost = _stored_cost(row)` reads the `cost_usd` the **proxy
persisted at request time** (`store.py:51`), i.e. it was computed from whatever
sheet `tamias serve` was given. The banner at `cli.py:218` is derived from the
sheet passed to `report` (`cli.py:174`). Point `report` at the real, all-zero
sheet and the same invented $0.239832 is printed as money, with no label.
The `simulated` flag rides on the *sheet*, not on the *number it produced*.

The same leak is live in the README and RUNBOOK: `README.md:170-172` and
`demo/RUNBOOK.md:220-222` both print dollar figures with the banner absent.

### 4.2 LEAK-2 (high) — RUNBOOK's honest-limits block is factually wrong on three counts

`VERIFIED` against `demo/run_arm.sh` and the tree:

| `demo/RUNBOOK.md` claim | Reality |
|---|---|
| `:216-219` "The routed arm gets `--inject-usage` and the baseline does not… baseline rows are UNKNOWN" | `READ` `run_arm.sh:212` gives baseline `--router-mode shadow --inject-usage`. **Both arms inject.** `VERIFIED` — `baseline-1.db` has 12/12 rows priced, not UNKNOWN. The displayed table (`RUNBOOK.md:220-222`: `baseline-1.db total cost: UNKNOWN (12 of 12 rows have unknown cost)`) does not match any file in `demo/runs/`. |
| `:236-237` "**`tamias.dashboard` is not on this branch**, so section 5's dashboard commands do not run yet." | `VERIFIED` — `src/tamias/dashboard.py` is on master (1058 lines) and I ran it (§6.2). |
| `:220-222` quoted totals `$0.001202` / `UNKNOWN` carry **no** simulated label | `VERIFIED` — `tamias report` appends `  [SIMULATED PRICES, NOT REAL SAVINGS]` to that figure. The doc silently drops the label from the one place a reader would copy a number. |

### 4.3 LEAK-3 (medium) — a `$0.00` "saving" is stated as a measurement

`VERIFIED` — `baseline-3`: 4 rows, 0 switches, and the report prints
`estimated saving: $0.00 (estimate; ignores cache rebuild cost; not measured)`.
`READ` `cli.py:236-245`: when `switched == 0` the branch takes the `else` path and
prints `$0.00` with the estimate label attached. `cli.py:230-235`'s own comment
argues the opposite — that `$0.00` "would read as *the switch would have saved
nothing*, which is a claim about money rather than about missing data" — but the
code only applies that reasoning to the `switched and not with_counts` case, not
to the zero-switch case. A reader sees `$0.00` on the same line as real money.

### 4.4 LEAK-4 (medium) — the dashboard's `saving_total` is a whole-run figure over a subset

`READ` `dashboard.py:186-216`: totals accumulate only `if row["priced"]`, which is
correct and disciplined, and `n_unpriced` is exposed (`:207`). But `actual_total`,
`baseline_total` and `saving_total` (`:210-213`) are plain floats with no
"over N priced of M" suffix, and the page renders them as
`money(report.actual_total)` (`dashboard.py:757,762,770`). `docs/DASHBOARD.md:74`
documents the n/a rule for the *percentage*; the *amounts* carry no equivalent
caveat. Same shape at `dashboard.py:231` for the compare series.

`VERIFIED` for the shipped runs this is benign (10/10 and 7/7 priced), so this is
a latent leak, not a live one — but the demo has DBs whose rows differ in
priceability, and a future run will hit it.

### 4.5 LEAK-5 (medium) — the realised-saving baseline is not a control

`READ` `cli.py:207-216`: `realised_saving` compares `model_requested` against
`model_used` **on the same row's own tokens**. `VERIFIED` — routed-2 prints
`realised saving: $0.066829 over 2 rows`, i.e. two requests, priced on two models,
with no quality check attached. `demo/RUNBOOK.md:210-213` separately notes the cheap
model might need three attempts to fix `median`. The report prints the number
without that caveat; only `tests_passed` in the sidecar JSON records quality, and
the report never reads it (`VERIFIED` — `cli.py:64-84` reads only the `requests` table).

### 4.6 LEAK-6 (low) — stale claim-to-code mapping in the audit docs

`VERIFIED` — `docs/audit/claims.md:13,62` and `docs/audit/HOW-IT-WORKS.md:38` both
assert the report prints `realised saving:` and `hypothetical saving:`.
`VERIFIED` — the actual output line is
`realised saving (rows the proxy actually rewrote):` and there is **no**
`hypothetical saving:` line at all (`cli.py:247-269`). Anyone auditing against
`claims.md` will fail to find a line the document calls mandatory.

### 4.7 Places that are clean (checked, no leak found)

`VERIFIED` — the price sheets are explicit: `prices.openrouter-sim.toml:1-22` and
`prices.simulated.toml:1-13` both open with a plain statement that the rates are
invented, and `simulated = true` is set. `VERIFIED` — with a `simulated` sheet,
every amount in `report` and in the dashboard carries the banner
(`cli.py:54,218`; `dashboard.py:66,222-223,749-750,1042-1045`). `VERIFIED` — the
dashboard recomputes cost from the sheet it was given (`dashboard.py:155-156`)
rather than trusting stored cost, so it does not have LEAK-1.
`VERIFIED` — unpriced rows render as the literal string `not priced`
(`dashboard.py:565`), never `$0.00`. `docs/live-evidence.md:294-304` is
self-critical to a degree that is unusual and welcome: it names its own saving
figures as *"arithmetic over token counts multiplied by invented rates"* and
records that an active-mode run *"cannot report its own saving"*.

---

## 5. Known problems

### 5.1 The five xfails

`VERIFIED` — `.venv/bin/pytest -q` → `302 passed, 5 xfailed`. `VERIFIED` — three
`xfail` marks exist (`tests/test_hardening_pricing.py:360`,
`tests/test_hardening_proxy.py:308,405`); BUG-3 is parametrised into 3 cases,
hence 5 xfailed tests from 3 marks. All three are `strict=True`, so fixing a bug
turns the suite red until the mark is removed — by design (`docs/audit/hardening-findings.md:5`).

| id | Severity | Defect | Evidence |
|---|---|---|---|
| **BUG-1** | high | A refused upstream connection answers 5xx but writes **no request-log row**. | `VERIFIED` — `tests/test_hardening_proxy.py:407-408` reason. `READ` — `proxy.py:424` `upstream = await client.send(outgoing)` is not wrapped in `try`; `httpx.ConnectError` escapes, FastAPI 500s, and `record(...)` at `proxy.py:432-440` is never reached. |
| **BUG-2** | medium | A 429 loses its `Retry-After`; only `content-type` is relayed. | `VERIFIED` — `tests/test_hardening_proxy.py:310-311` reason. `READ` — `proxy.py:443-447` rebuilds the response from `upstream.content` + `upstream.headers.get("content-type", …)`; `anthropic_adapter.py` builds its relayed response the same way. Confirmed by construction: the header cannot survive a rebuild that only copies content-type. |
| **BUG-3** | high | A model with an unquoted rate is billed as if that rate were zero, reporting `$0.00` as fact. | `VERIFIED` — `tests/test_hardening_pricing.py:362-363` reason. `READ` — `pricing.py:120-126` gates the "unknown" path on `is_nonzero(...)`; a rate that is `None` is not nonzero, so it never reaches the `missing` list at `pricing.py:180-199`, and the arithmetic at `pricing.py:238-244` uses `(input_p or 0)`. Severity is high *because* it is the exact "UNKNOWN reported as $0.00" failure the rest of the codebase is built to prevent. |

`VERIFIED` — `grep -rn "TODO|FIXME|XXX|HACK" src/ tests/ demo/ scripts/ docs/`
→ **no matches**. All known debt is carried as xfail markers or as prose in
`docs/audit/hardening-findings.md`.

### 5.2 Retry-After / 429 handling, precisely

`VERIFIED` — `grep -rn "Retry-After|429|rate_limit" src/ tests/`:
- `tests/test_hardening_proxy.py:283-304` asserts the *status*, *body* and *one
  log row* survive a 429, and that a normal request afterwards still works. That
  test passes (`VERIFIED` — in the 302).
- `tests/test_hardening_proxy.py:308-329` asserts `Retry-After: 7` is relayed. That
  test xfails.
- **No retry logic exists anywhere**: no backoff, no `Retry-After` parse, no
  upstream-side retry (`VERIFIED` — no such code in `src/`). Tamias is a pass-through
  proxy and leaves retry policy to the client. The only defect is that the client
  is not *told* what to do. `demo/RUNBOOK.md:238-240` names "a free tier 429" as a
  live failure mode.

### 5.3 Failure modes of `demo/run_arm.sh`

`READ` + `VERIFIED` against the script and the recorded artefacts:

1. **Missing key → clean die.** `run_arm.sh:256-261` prints `key set` / `key
   missing` (never the value) and `die`s. Correct, and no key is read. But it
   dies **after** `rm -f "$DB"` (`:189`) and after starting the proxy (`:226-237`),
   so the run leaves a fresh empty `.db` and a `.serve.log` behind with no `.json`.
2. **Partial run leaves a lying sidecar.** `run_arm.sh:320-345` writes the JSON
   unconditionally at step 6. If the proxy started but then died, or the files are
   later removed, you get a `.json` with `tests_passed: false` and a `db` path that
   does not exist. **`routed-3` is exactly this state** (`VERIFIED`, §3.3).
   `ASSUMED` — I cannot tell from the artefacts whether the `.db` was deleted or
   never created; the `.serve.log` is missing too, which no single point in the
   script explains.
3. **Agent failure is recorded, not fatal.** `run_arm.sh:290-291`
   `timeout 900 opencode run --auto … || echo "opencode exited non-zero; recording
   tests_passed=false"`. Good for data collection, bad for comparability: a
   routed arm that ran out of budget at 899 s is stored next to one that finished
   in 60 s, and nothing records *why* it failed. `wall_seconds` is recorded
   (`:303`) but no failure reason is.
4. **A 900 s timeout silently truncates an arm.** `AGENT_TIMEOUT=900`
   (`run_arm.sh:38`) leaves no marker in the `.json` distinguishing "agent finished"
   from "agent killed by timeout". `tests_passed` then reflects only the leftover
   pytest state, not an attempt.
5. **`--effort-policy` is a live landmine.** `run_arm.sh:220-222` appends
   `--effort-policy "$EFFORT_POLICY"` when it is non-empty.
   `VERIFIED` — `tamias serve` has no such flag (§1.3). `VERIFIED` — it is currently
   empty in `demo/config.env:27` and `demo/config.env.example:26`, and
   `config.env.example:24-26` says so: *"that only works once `tamias serve` grows
   the flag. Leave it empty until the flag is merged."* Set it and every routed arm
   exits 2 with `unrecognized arguments`, after the DB has been deleted.
6. **The prompt is empty, so every live arm dies at step 1.** `VERIFIED` —
   `demo/TASK_PROMPT.txt` is 0 bytes; `run_arm.sh:168-176` generates
   `prompt.txt` by substituting into it and then dies on `[ -s "$PROMPT_FILE" ]`.
   This blocks **all** live runs, not just effort ones.
7. **Two fixed ports, no collision check.** `PROXY_PORT=8000` (`:36`) and
   `MOCK_PORT=9001` (`:37`) are hardcoded. `wait_for_port` (`:122-137`) returns
   success if *anything* is listening, so a stale proxy from a previous run makes
   the new arm log to a DB nothing is writing.
8. **Shared mutable task dir.** `TASK_DIR=/tmp/tamias-demo-task` (`:32`) with
   `rm -rf` at `:161`. Two arms on one machine — and five worktrees exist on this
   machine — destroy each other's task project.
9. **Costs are incomparable across arms as recorded.** `VERIFIED` — baseline-1 has
   12 rows, baseline-2 has 7, baseline-3 has 4; routed-1 has 12, routed-2 has 10.
   Different amounts of work, so `$0.001559` vs `$0.239832` is a statement about
   how much the agent did, not about routing.

---

## 6. Demo readiness

### 6.1 The dashboard on the recorded runs, today — exact commands, in order

`VERIFIED` — this exact sequence ran successfully in this session.

```sh
# 1. from the repo root; no key, no network, nothing is written
cd /home/krish/Desktop/tamias

# 2. sanity: the numbers you are about to show
.venv/bin/tamias report --db demo/runs/routed-2.db --prices prices.openrouter-sim.toml
.venv/bin/tamias report --db demo/runs/baseline-2.db --prices prices.openrouter-sim.toml

# 3. serve the page: routed run in the foreground line, baseline as the compare line
.venv/bin/python -m tamias.dashboard \
  --db demo/runs/routed-2.db \
  --prices prices.openrouter-sim.toml \
  --compare-db demo/runs/baseline-2.db \
  --port 8010

# 4. open http://127.0.0.1:8010
```

`VERIFIED` — startup banner printed:

```
tamias dashboard: http://127.0.0.1:8010
tamias dashboard: request log demo/runs/routed-2.db, prices prices.openrouter-sim.toml (2026-10-04)
tamias dashboard: this price sheet declares itself simulated, so every amount on the page is labelled: SIMULATED PRICES - NOT REAL SAVINGS
tamias dashboard: comparing against demo/runs/baseline-2.db
```

`VERIFIED` — `curl -s http://127.0.0.1:8011/api/summary` (same invocation, port 8011):

```json
{"n_requests":10,"n_priced":10,"n_unpriced":0,"n_switched":2,"share_cheap":0.2,
 "actual_total":0.23983187,"baseline_total":0.3066612,"saving_total":0.06682933,
 "saving_pct":0.21792561302179725, "simulated":true,
 "compare":{"n_requests":7,"n_priced":7,"actual_total":0.180645, ...}}
```

That is a **21.8 % "saving" on free models priced at invented rates**. It is the
best-looking number in the repo and it means nothing about money. Say so out loud
during the demo; `docs/live-evidence.md:294-304` gives you the words.

`VERIFIED` — md5 of both `.db` files identical before and after the dashboard ran.

**What will break if the demo is not this one:** `demo/runs/` is gitignored
(`VERIFIED`), so on any other machine step 3 has nothing to read and
`dashboard.py:1030-1036` will serve an empty page rather than error. **Copy the
five `.db` + `.json` files across, or commit them deliberately.**

### 6.2 Minimum change to run a paid OpenRouter model and show OpenRouter's own cost

Two changes, both small. The second is the one that matters for honesty.

**(a) Point at a paid pair and price it with real rates. No code change.**
`VERIFIED` — `demo/pick_models.py --emit-toml` (`:63-70` of RUNBOOK; `demo/pick_models.py:236`)
emits a real, dated, `simulated`-absent sheet from the public catalogue, and
`config.env` already has `STRONG_MODEL` / `CHEAP_MODEL` / `PRICES` slots wired
(`run_arm.sh:90-94`). So: pick two non-`:free` models, write the sheet, set
`EFFORT_POLICY=` (empty), and run the arm. `run_arm.sh:290` already invokes
`opencode run --auto` against the proxy. **But** `demo/TASK_PROMPT.txt` must be
filled in first (0 bytes today) or the arm dies at `run_arm.sh:176`; and the key
must be in the environment as `OPENROUTER_API_KEY` (`run_arm.sh:256`).

**(b) Add provider-reported cost to the ledger. This is the real gap.**
`VERIFIED` — no code path captures OpenRouter's `usage.cost` /
`usage.cost_details` (§3.2). The minimum is four edits:

| # | file | change | size |
|---|---|---|---|
| 1 | `src/tamias/types.py:14` | add `provider_cost_usd: float \| None = None` to `Usage` | S |
| 2 | `src/tamias/proxy.py:103` (`to_usage`) and `src/tamias/anthropic_adapter.py:110` (`usage_from_anthropic`) | read `raw["cost"]` / `raw["cost_details"]` into the new field; leave `None` when absent | S |
| 3 | `src/tamias/store.py:22` (`COLUMNS`) + `:41` (`_CREATE_TABLE`) + `:103` (`log_request`) | add `provider_cost_usd REAL` column; nullable; **migration on an existing db is a new table or an `ALTER TABLE` guard** | S |
| 4 | `src/tamias/cli.py:239` | new report line: `provider-reported cost: $X over N of M rows (tamias computed: $Y; delta Z%)` — UNKNOWN when `n == 0`, never `$0.00` | S |

`VERIFIED` — the pattern to copy is already in the codebase: `cli.py:87-96`
`_stored_cost()` is exactly the "a value that may be NULL and NULL means UNKNOWN,
not zero" helper, and `cli.py:220-225` is the UNKNOWN-total rendering. Tests
belong beside `tests/test_report_realised_saving.py` (354 lines, the existing
template for a new report line) plus one case each in `tests/test_proxy.py` and
`tests/test_anthropic.py` for the parse.

The payoff is specific and worth the hour: with (b), the demo can say *"OpenRouter
reports $0.041; tamias computed $0.039; the 5 % gap is our price sheet being a few
days stale"* — a **measured** reconciliation instead of invented arithmetic.
Without (b), every dollar on screen remains tamias talking to itself.

---

## 7. Gap list, prioritized

### P0 — needed for a faculty demo today

| # | What | Files | Size | Test needed |
|---|---|---|---|---|
| P0-1 | Carry the simulated/estimated provenance on the **number**, not the sheet: store `price_sheet_simulated` per row and use it for the banner in `report` and the dashboard. Fixes LEAK-1 (`$0.24` printed unlabelled from a real sheet). | `src/tamias/store.py:22,41,103`; `src/tamias/proxy.py:373`; `src/tamias/cli.py:87,218`; `src/tamias/dashboard.py:222` | S | new `tests/test_hardening_privacy.py` case: db priced by a sim sheet + report by a real sheet → output **must** contain `SIMULATED` |
| P0-2 | Fill in `demo/TASK_PROMPT.txt`. Today it is 0 bytes and **every live arm dies** at `run_arm.sh:176`. One paragraph + `{PYTEST}`. | `demo/TASK_PROMPT.txt` | S | `tests/test_demo_tools.py` (277 lines, existing) — assert the template is non-empty and contains `{PYTEST}` |
| P0-3 | Commit the recorded runs, or document the copy step. `demo/runs/` is gitignored (§3.3); the demo has no data on any other machine. | `.gitignore:12`; `demo/runs/*` | S | none (data, not code) — but decide deliberately: 5 small dbs, ~12 KB each |
| P0-4 | Correct `demo/RUNBOOK.md`'s three false "honest limits" (LEAK-2: `--inject-usage` claim, `tamias.dashboard is not on this branch`, and the stripped banner). This document is what a faculty member will read. | `demo/RUNBOOK.md:216-237` | S | none; verified by re-running the commands it quotes |
| P0-5 | Make `realised saving` state its own weakness inline: it is a two-row, no-quality-control difference. Add "(same rows, no quality control; see arm `tests_passed`)". | `src/tamias/cli.py:253-260` | S | `tests/test_report_realised_saving.py` gains one assertion on the label |

### P1 — this week

| # | What | Files | Size | Test needed |
|---|---|---|---|---|
| P1-1 | Fix **BUG-1**: wrap `client.send` in `proxy.py:424` and `anthropic_adapter.py:390+` in `try/except httpx.HTTPError`, answer 5xx, **and write the log row**. A failed call that leaves no row is a hole in the audit log — the exact thing this project exists to prevent. | `src/tamias/proxy.py`; `src/tamias/anthropic_adapter.py` | S | un-xfail `tests/test_hardening_proxy.py:405` (strict=True already there) |
| P1-2 | Fix **BUG-3**: a `None` rate must make the cost UNKNOWN, never 0. Currently `pricing.py:120-126` treats a missing rate as "not needed". | `src/tamias/pricing.py:117-126,180-199,238-244` | M | un-xfail `tests/test_hardening_pricing.py:360` |
| P1-3 | Fix **BUG-2**: relay `Retry-After` (and `x-ratelimit-*`) on relayed error responses. | `src/tamias/proxy.py:443-447`; `src/tamias/anthropic_adapter.py` | S | un-xfail `tests/test_hardening_proxy.py:308` |
| P1-4 | Persist effort end to end: `effort_requested` / `effort_used` columns in the store, populated by the proxy, read by the dashboard. The dashboard already renders both and always shows blanks. | `src/tamias/store.py`; `src/tamias/proxy.py`; `src/tamias/dashboard.py:52-53` | M | store round-trip + one proxy test asserting the columns are written |
| P1-5 | Merge `feat/effort` (or rebase it — it is 10 commits behind) and give `tamias serve` a real `--effort-policy`, so `run_arm.sh:220-222` stops being a landmine. | `src/tamias/effort.py` (branch only); `src/tamias/cli.py:361`; `src/tamias/router.py`; `src/tamias/proxy.py` | M | port `tests/test_effort.py` (the `.pyc` shows it existed on the branch) + one `tamias serve --help` assertion |
| P1-6 | Record a **provider cost** column so a paid run can reconcile against OpenRouter's own number (§6.2b). | `types.py`; `proxy.py:103`; `anthropic_adapter.py:110`; `store.py`; `cli.py:239` | S (4 files) | new report-line tests + parse tests in `test_proxy.py` / `test_anthropic.py` |
| P1-7 | Make `run_arm.sh` fail loudly instead of leaving a lying sidecar: write the JSON only if the DB exists and has rows; record `failure_reason` and `timed_out`. | `demo/run_arm.sh:289-345` | S | a shell-level test, or `tests/test_demo_tools.py` over the JSON schema |
| P1-8 | Delete or quarantine `wip/mixed-snapshot` (`21fe8d0`, *"never merge this"*). One unmerged "mixed work" commit on a branch five agents share is how a bad merge happens later. | git | S | none |

### P2 — later

| # | What | Files | Size | Test needed |
|---|---|---|---|---|
| P2-1 | **Observer, smallest honest slice**: `tamias scan` over Claude Code JSONL → the ledger in `types.py`, with `model_source`, `effort_used` + `effort_source`, `service_tier`, and 5m/1h cache buckets. This is the product's actual subject (`docs/...:597-693, 718-813`). | new `src/tamias/observer/` (parser, ledger, dedup) | **L** | fixture JSONL → golden ledger; dedup test (same `message_id`+`request_id` twice → one row); no-prompt-text test (the schema has nowhere to put it, and that must stay true) |
| P2-2 | **Observed rebuild event model + cache state**: raw flags, normalized cause with UNKNOWN, the two evidence hierarchies, WARM/COLD/REBUILT/UNKNOWN, and the rebuild-size estimate. `docs/...:635-718, 753-813`. | observer + a new cache-state module | **L** | every flag combination → expected cause; UNKNOWN never collapses to a guess; estimate-vs-cache-write divergence is reported, not reconciled |
| P2-3 | **Rebuild report**: the ranked top-exposure view with evidence state and an unknowns/anomalies section. `docs/...:997-1002`. | `src/tamias/cli.py` (new subcommand) | M | ranking order is stable; a run with zero observable events reports UNKNOWN, not `$0.00` |
| P2-4 | **`tamias receipt <session> [--event N]`** and **`tamias doctor`** (the 9 checks). The cheapest high-credibility win in the Observer: a receipt that explains one event and refuses to claim causality is a demo in itself. | `src/tamias/cli.py` | M | receipt output contains no quality/causal word; doctor flags a stale sheet and an unknown parser version |
| P2-5 | **Semantics registry + opt-in probe** with a hard spending cap. `docs/...:893-967`. | new `src/tamias/semantics.py`; opt-in CLI | M | probe is refused unless explicitly enabled; cap is enforced in code, not in the caller |
| P2-6 | **Lab skeleton**: harness + snapshot/restore + the four actions (STAY / EFFORT_ONLY / MODEL_ONLY / BOTH) + censoring. `docs/...:1029-1118, 1266-1452`. | new `src/tamias/lab/` | **L** | snapshot→restore is byte-identical; a censored branch is marked, not silently dropped; ≥3 branches per arm is enforced by the runner |
| P2-7 | **Statistics**: paired task-level bootstrap, Holm, and `dC = T_cache + T_handoff + dC_behavior`. `docs/...:1454-1585`. | new `src/tamias/stats.py` | **L** | bootstrap CI against a known fixture; Holm on a known p-vector; the decomposition sums to `dC` and each term is separately falsifiable |
| P2-8 | **Historical repricing**: multiple dated sheets loadable at once, and `tamias report --as-of DATE`. `pricing.py:45-60` already carries `date`; nothing consumes the history. | `src/tamias/pricing.py`; `src/tamias/cli.py` | M | repricing a log at two dates gives two totals that agree with two sheets |
| P2-9 | **`/v1/responses` logged rather than relayed unlogged**, and `/v1/messages` catch-all behaviour made explicit. | `src/tamias/proxy.py:502`; new adapter | M | a Responses-API request leaves a priced row |
| P2-10 | Conditional **Controller** (gain `G(s,a)`, STAY/SET_CONFIG, held-out gate). Deliberately last: `docs/...:339-346` defers it, and it is meaningless until the Lab produces data. | new `src/tamias/controller/` | **L** | held-out gate refuses to promote without held-out evidence |

### Recommended build order

1. **P0-1** — provenance rides with the number. Nothing else on this list is worth
   much if `$0.24` can still be printed as money. One column, one banner source.
2. **P0-2** — write `TASK_PROMPT.txt`. It is 0 bytes and it is the difference
   between "we can show you a recorded run" and "we can show you a live run".
3. **P0-4 + P0-5** — correct the RUNBOOK and label the realised-saving line.
   Thirty minutes; removes the three statements a sharp reader will catch.
4. **P0-3** — commit `demo/runs/` so the demo travels.
5. **P1-6** — provider cost column. This is what turns the demo from invented
   arithmetic into a measured reconciliation. Do it *before* any paid run, so the
   first paid run already produces the comparison.
6. **P1-1, P1-2, P1-3** — the three xfails. Each is a small, well-specified fix with
   a strict test already written and waiting; fixing them removes the mark and the
   suite goes green at 307.
7. **P1-7 + P1-8** — make the runner honest about failure; delete the WIP branch.
8. **P1-4 + P1-5** — effort end to end, merge `feat/effort`.
9. **P2-1 + P2-4** — Observer: `scan` and `receipt`/`doctor`. This is where the
   project becomes the thing the architecture document describes rather than a
   proxy with good arithmetic.
10. **P2-2 + P2-3** — rebuild events, cache state, ranked report.
11. **P2-6 → P2-7 → P2-10** — Lab, then statistics, then Controller. In that order.
    The Controller is last on purpose: it is a conditional component and there is
    nothing for it to condition on until the Lab has produced paired branches and
    a bootstrap.

---

## 8. Doubts (for you)

1. **Which document governs?** `AGENT-RULES.md` is absent from `~/Desktop/tamias`
   and present only in the five sibling worktrees. Is `~/Desktop/tamias` supposed
   to have one, or is master intentionally ruleless? I obeyed `tamias-w1`'s copy.
2. **`docs/TAMIAS-FINAL-ARCHITECTURE.md` is untracked.** Is it the agreed target,
   or someone's draft that has not been accepted yet? My whole gap list is measured
   against it; if it is a proposal, P2 shrinks and P0 grows.
3. **Should the five `demo/runs/*.db` be committed?** They are gitignored and they
   are the only data that makes the demo work. That looks like an oversight, but
   committing a directory of sqlite files into a public repo is a decision, not a
   fix.
4. **Is `wip/mixed-snapshot` meant to survive?** Its own message says
   *"never merge this"*. Do you want it kept as a reference, or deleted? If kept,
   where should it be described so the next person does not merge it by accident?
5. **Is `demo/TASK_PROMPT.txt` empty by accident?** `run_arm.sh` treats it as the
   agent's whole brief. Every recorded live run has `tests_passed` from a run that
   presumably had a prompt, so either the file was truncated recently or those runs
   used a different path. Which is it?
6. **How much of the Observer do you actually want before the next milestone?** The
   architecture document describes a large thing (parser, dedup, ledger, event model,
   two evidence hierarchies, cache state, price history). P2-1 through P2-3 is
   weeks, not days. Is there a smaller "Observer v0" you would accept — for example
   `scan` over JSONL producing a flat ledger and nothing else?
7. **Should `tamias report` refuse to print a dollar figure it cannot defend?** The
   alternative to P0-1 is stricter: `report` recomputes from tokens + the sheet it
   was given and ignores `cost_usd` entirely, the way the dashboard already does.
   That is more honest and it breaks continuity with what the proxy logged. Which
   do you prefer?
8. **Is the Lab in scope for you at all, or is this a product project that borrows
   research language?** `demo/run_arm.sh` is a repeatable single-condition runner,
   not matched branching, and `run_arm.sh:60-80` takes an arm name and an index.
   If the Lab is deferred, I would drop P2-6/7/10 from the critical path entirely and
   spend that time on Observer + provider-cost reconciliation.