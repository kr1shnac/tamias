# TAMIAS — Single Source of Truth (Design Spec v1.1)

> **STATUS BANNER — read first.**
> This document is **Design Spec v1.1 — status: DESIGNED, UNVALIDATED.** It is the reference artifact for Tamias **from now until production**; after production it is updated, not replaced. It is *not* "final" and must not be cited as evidence of savings or quality — three independent councils (§14) established that the savings instrument could not yet falsify its own claim and that **zero savings have ever been measured**. That formula half is now fixed (2026-10-19: `saved` is cache-aware and can be negative, F-1), but no figure has been reconciled against a real invoice. §10 lists the three exit criteria that must be met before any later version may be called final or cited in a paper.
> **Every factual claim carries a status tag:** `PROVEN` = verified by test, live run, or audit spot-check, with source cited. `DESIGNED` = decided in design/council, not implemented or measured. `UNTESTED` = implemented but only fixture/mock-tested or unverified against a live provider. `UNVERIFIED` = asserted, evidence not checked. **An untagged sentence must be treated as `UNVERIFIED`.** Factual claims carry a verification date when they describe the moving codebase.
> **v1.1 changes vs v1.0 (all re-verified 2026-10-19 against the repo by grep):** §6 session-digest description corrected (first user message, not last; `x-tamias-session` precedence); §9 coverage matrix corrected — `/v1/messages` **does** rewrite the model (router.decide + applied_target) — only `/v1/responses` is meter-only, and **effort** is chat-completions-only; §3 error-marker description made precise (default vs generic profile); test count refreshed (932 collected, 48 test files); §15–§20 added (file map, misrepresenting-file dispositions, decided-against list, benchmark protocol, per-agent integration status, update policy).
> **Later fixes, 2026-10-19 (each re-verified against the repo):** **F-20** — a real, dated OpenRouter price sheet is now bundled, so first run prices above `$0.00`; **F-6** — resolved with evidence (published TestPyPI `0.1.0a3` accepts `--router-mode active`; only the never-published `v0.1.0a4`/`a5` tags reject it); **F-12** — the catch-all passthrough now writes one count-only row per request and `tamias report` counts them; **F-1 (code half)** — the savings arithmetic is now **cache-aware and can go negative**: the dashboard `saved` and both `tamias report` saving lines price the stay model with a warm cache and the switch model on the row's real counts, so a constructed cache-miss replay yields `saved ≤ 0` (provider-invoice reconciliation, F-5, is still pending).
> Update rule: change a tag only by changing the underlying evidence, and say where the evidence is (§14, §20).

## 0. How to use this file, and where it is used

| Use | Where | What to read |
|---|---|---|
| Human deciding whether to build/use Tamias | this file | §0 banner, §1, §10, §13 |
| Agent/AI implementing or answering about Tamias | this file as system/context input | all of Part A (§1–§9, §15–§19); consult §13 before claiming anything is proven |
| Researcher / paper author | this file | §1, §8, §10, §13 — **nothing outside `PROVEN` rows may appear in an abstract** |
| Package publisher (PyPI etc.) | this file + `docs/audit/claims.md` | §7 (what the package guarantees), §9 (what routes it rewrites), §19 (integration claims allowed), §10 (release gates) |
| Onboarding / handoff to a new contributor or agent session | this file | §0, §11 (build order), §15 (file map) |
| Reviewing a proposed change against the project's decisions | this file | §4, §5, §17 (what was rejected and why), §13 |
| Writing integration docs (README/INTEGRATION.md) | this file | §9, §19 — copy coverage and wording exactly; never say "any agent" without the §1 qualifier |
| Designing the benchmark | this file | §8, §10, §18 |
| CI / lint discipline (future) | this file | §13 exit criteria can be machine-checked as release gates |

Concretely, other project files should **point here** instead of restating the architecture: `tamias/README.md`, the archived `*.legacy.md` briefs, `ARCHITECTURE.md`, and any future `INTEGRATION.md`/`ROUTER-ARCHITECTURE.md` should be short summaries with a link to this file (dispositions in §16). Any agent session working on Tamias should be given this file path as the first context input.

This file supersedes, for architecture questions: the archived `TAMIAS-PROJECT-BRIEF.legacy.md`, `CLAUDE-REVIEW-PROMPT.legacy.md`, `OBSERVE-EXPERIMENT-CONTROL.legacy.md` briefs, `ARCHITECTURE.md` (scope sections), and `README.md` framing. Those describe the *earlier* cost-observability project; where they conflict with this file on what Tamias *is*, this file wins. `docs/audit/claims.md` remains the authority on what has been **verified in the codebase** and is not duplicated here; on any code question, claims.md outranks this file.

---

# PART A — Normative design spec

## 1. Identity, goal, non-goals

**Tamias is a transparent, stateless-per-request, OpenAI-compatible request-rewriting proxy.** `PROVEN` (identity adopted by council 20261018-1820; code exists).

- **Goal:** reduce cost *inside* an agent's existing request flow, without agent cooperation, harness changes, or loss of streaming, tool calls, or the agent's control loop. Savings are reported as baseline-vs-actual. `PROVEN` goal; `UNTESTED` outcome (§8, §10) — **no measured savings exist** (`claims.md:12` VERIFIED).
- **Unit of intervention:** one HTTP request. Per endpoint: `/v1/chat/completions` full rewrite; `/v1/messages` model rewrite yes, effort no; `/v1/responses` **meter-only, never rewrites** (responses_adapter.py:2-3 "deliberately never asks the router to rewrite a model" `PROVEN`). Tamias sees only that request's messages. No turn orchestration, no decompose/merge. The agent keeps its loop. `PROVEN`.
- **"Stateless" is qualified:** stateless *per request*; Tamias keeps a session/decision ledger across requests (§6) for escalation and accounting. The v0 draft said simply "stateless"; that self-contradiction is fixed here (F-8). `PROVEN` contradiction, corrected.
- **Coverage claim — exact wording:** *"Tamias rewrites model selection for OpenAI-compatible agents on `/v1/chat/completions` and `/v1/messages` (Anthropic Messages wire). Codex-style `/v1/responses` traffic gets metering and cost visibility today, **zero** rewriting."* Do **not** say "any unmodified agent" without this qualifier (F-8). All three named agent integrations (OpenCode, Codex, Claude Code) are currently labelled **UNVERIFIED** in `docs/AGENTS.md:4-21` `PROVEN`. Marketed benefit also includes cost **observability** and per-key **budget governance** (§2 L5) that work on all three routes today (`UNTESTED` live).

### Non-goals (explicit, permanent)
- Tamias-agent harness changes; turn-level orchestration; competing with provider-native routing products. `PROVEN` (scope freeze, ARCHITECTURE.md §23/§24).
- Subscription-billed agents (Claude Max, ChatGPT plans) have no per-call cost to rewrite; addressable market = metered-API usage. `UNVERIFIED` (T-11; usage mix unmeasured).
- Guaranteeing output quality from inside the proxy — structurally impossible (counterfactual unobservable); see §4. `PROVEN` structural limit (quality council 2248).

## 2. What Tamias changes (the lever set)

Only the fields below are rewritten on a forwarded request; everything else is byte-identical (`PROVEN` byte-identical forwarding in adapters). `DESIGNED` policy.

| Lever | What it changes | Status |
|---|---|---|
| L1 Model switch | `model` field, per request, per session tier | `UNTESTED` — implemented + unit-tested for active mode in source (`cli.py:64` accepts `active`); live-exercised in recorded dry runs on free models; **source-vs-release conflict open** (F-6) |
| L2 Reasoning effort | `reasoning_effort` / `reasoning.effort` ∈ {low, medium, high} (minimal only where the provider supports it) | `UNTESTED` — fixture-only; applied **only** on the chat-completions route (`proxy.py:872`; `effort.py:17`; `docs/EFFORT.md:20-30` — "not applied to the Anthropic Messages route or to /v1/responses", "NOT been demonstrated end-to-end"). Providers may ignore or let it invalidate cache (`EFFORT.md` Warning, `PROVEN` as documented risk) |
| L3 Context/input shaping | trim stale tool outputs, cap re-sent history, output caps per class | `DESIGNED`, not implemented |
| L4 Cache handling (CACHE-GATE) | refuse switches that destroy a warm prefix cache when the rebuild cost exceeds the switch saving | `DESIGNED`, `UNTESTED` (F-11: "no universal TTL model" conceded; the most likely real-world failure mode even if quality is perfect) |
| L5 Governance layer (shipped, not a lever) | per-key identity + revocation, budgets (total, per-model sub-caps, RPM, TPM), reserve-then-settle, model allowlists, projects, dashboard, report | `UNTESTED` — implemented, mock-tested (`budget.py`, `keys.py` exist; `BUDGET.md:109` "Unverified against a live provider"; revocation does not cancel in-flight) `PROVEN` existence |

**Effort is NOT assumed safe.** Any lever, including effort reduction, ships only with non-inferiority evidence on the same footing as model switching (quality council 2248). `PROVEN` decision.

## 3. Per-request pipeline (8 stages)

`DESIGNED` end-to-end; stage provenance inline. Order is normative.

1. **METER** — record tokens, effort, cache read/write, latency into the per-key ledger. `PROVEN` implemented on all three adapter routes (chat, messages, responses); every other path is relayed by the catch-all. **F-12 FIXED 2026-10-19:** the catch-all `passthrough` route (proxy.py) now writes one **count-only** row per request — `route = "passthrough"`, token counts and cost NULL, decision reason `passthrough route is not metered` — so off-route traffic can no longer leave the ledger invisible, and `tamias report` prints `passthrough requests (count-only, never priced): N`. It is still neither priced nor budget-gated by design (a model named on the passthrough is refused by the budget gate). The old claim (`claims.md:48`) that the catch-all is *unlogged* no longer holds. F-16: streamed responses may omit usage → systematic meter undercount; `claims.md:42` VERIFIED lists "streamed request without include_usage" among known UNKNOWN-cost causes. Streaming usage must request it explicitly where the provider supports it.
2. **CLASSIFY** — rule-based heuristics on the **last message only** (trailing role, tool name/class from EASY_TOOLS, error markers) → task class + STAY/SWITCH with confidence; hysteresis (min_gap) prevents ping-pong. `PROVEN` decide() inspects messages[-1] (router.py); thresholds `DESIGNED`. Error markers, precise: **default profile** ships `ERROR_MARKERS = ("Traceback","FAILED","error:")`, case-sensitive, and `Error:` (capital) is deliberately excluded — pinned by `test_error_markers_are_case_sensitive` as opt-in via `RouterConfig(error_markers=...)` or TOML (router.py:19-34, `PROVEN`); the **generic profile** adds `"Exception"`, `"non-zero exit code"` (`GENERIC_ERROR_MARKERS`, `PROVEN`). The v0 draft understated this (F-21).
3. **ROUTE** — cheapest *capable* (model, effort) from the registry. Guard order: capability filter → context-window fit for this request → tool-calling capability → user-pin precedence → no mid-loop switch when history holds model-bound reasoning content (provider thinking signatures). `PROVEN` registry validates `context_window`, `tools`, `reasoning`, `efforts` as data (registry.py:18-21,58-59); guard sequence `DESIGNED`.
4. **CACHE-GATE** — estimate cache-rebuild cost of the switch; switch only if net-positive (dwell/prefix-growth rule). `DESIGNED`, `UNTESTED` (F-11). Input/context is believed the dominant cost term in an agent loop `[INFERENCE, High]` — hence this stage exists at all.
5. **SHAPE** — trim stale tool outputs beyond recent turns; cap re-sent history; set output caps per class. Recent context untouched. `DESIGNED`, not implemented.
6. **FORWARD** — body to provider; SSE relayed chunk-by-chunk without buffering; upstream status codes pass through; hop-by-hop headers + `accept-encoding` handled; `Authorization` (or `x-api-key`) forwarded untouched. `PROVEN` (proxy + both adapters).
7. **RECONCILE** — response contract: echoed model, usage shape, tool-call IDs, finish_reason. **Tokens already streamed cannot be un-sent** → reconciliation is detect-and-document, or gate before forward (F-23). Known breakages deferred: echoed model vs actually-sent model (model identity is load-bearing, T-6), reasoning signatures (T-7), tool-call IDs (T-8). `PROVEN` risks, `DESIGNED` handling.
8. **REPORT** — net saving = baseline − actual − cache-write penalties − failed-turn cost, with a formula that **can emit a negative number** (§8, F-1). Turns-to-task + completion as quality gates; UNKNOWN stays UNKNOWN. Observer overhead (ledger writes, latency) charged against savings (F-26). `DESIGNED`; **the code half of F-1 is now `PROVEN`** — `dashboard.py:_price` and `cli.py:_saving`/realised saving price the stay model warm and the switch model on the row's real counts, so `saved` can be ≤ 0 (a constructed cache-miss replay yields it; §11 step 2). Failed-turn cost and provider-invoice reconciliation (F-5) remain `UNTESTED`.

Global: **fail-open** — any internal error → forward the original unchanged, log a warning. `PROVEN` implemented. F-13: fail-open converts any fault into silent zero savings; fail-open events must appear in the report. `PROVEN` risk, `DESIGNED` fix.

## 4. Quality posture (decided by quality-guard council 2248; `PROVEN` as decisions)

- **Only live switching policy initially:** structural capability classes — cheap models allowed only on classes verifiable by inspection (pure formatting, trivial reads), never tool-heavy classes. **Bounds blast radius; is not a quality guarantee.** Effort/context shaping ships with the same non-inferiority evidence requirement.
- **Escalation ladder (monotonic sticky-up): REJECTED as written.** Prerequisites before building it: durable session ledger surviving restart (today in-memory only — proxy.py:562-566 "Held in memory only… a restart resets every counter", LRU-capped, `PROVEN`); a written, case-robust failure definition (F-14: the feedback channel — error markers, retries — is authored by the graded agent → gameable/poisonable; never promote on agent-authored markers alone); idempotency records so escalation never double-applies a mutating tool action; decay/re-probe; an off-proxy oracle. Silent failures are undetectable from inside the proxy by construction. True worst case of naive escalation is **unbounded** (a markerless failing session never escalates), not "1–2 wasted turns".
- **Verify-after-forward ("audit the answer on a strong model"): REJECTED.** Cannot un-forward streamed tokens; audit re-sends context and doubles latency.
- **Shadow-first calibration: adopted in shape only.** Current shadow forwards byte-identical and **never executes the cheap model** — it collects switch-intent, not quality data (the quality council's named blind spot). Required harness: executing shadow on sampled real traffic; task-boundary segmentation (sessions are continuous loops; turns-to-task needs a delimiter); durable session identity surviving restart **and** unifying subagent fan-out (session keys fragment today); off-proxy ground-truth oracle (CI/tests/commit/accept) because the proxy sees tool success, never task success; held-out grading so "proven non-inferior" is not self-scored; thresholds stored as registry data.
- **Metric: cost-per-completed-task**, not cost-per-request — cheap models may need more turns, and those turns grow the re-sent transcript. `DESIGNED`; `UNTESTED` (no task definition exists yet, F-35).

## 5. Anti-spiral design (when escalation is eventually built) — `DESIGNED`; build only after §4 prerequisites

- Failure signals the proxy can see: tool-error markers (untrusted, agent-authored), abrupt turn growth, provider 4xx/5xx, context-overflow errors.
- Monotonic sticky-up: never downgrade after a failure within a session; escalation only.
- Honest accounting: failed turns charged against savings; escalation overhead (extra strong-model calls) netted too.
- **Unresolved structural fact:** the counterfactual (what a stronger model would have done) is unobservable from inside the proxy; no proxy-only mechanism closes this — the oracle lives off-proxy (§4).

## 6. Sessions and state — `DESIGNED` (durable ledger = first build item)

- **Today (verified 2026-10-19):** session id = salted SHA-256 digest derived in `session.derive_session_id` (session.py:185-206) from the request's **authorization header + first system/developer message + first user message** (proxy.py:22-27) — *first*, not last (v1.0 said last; corrected). A well-formed caller-supplied **`x-tamias-session` header wins** when present and is hashed before storage (session.py:194-205, `PROVEN`) — a session-state gaming vector (§7). Sessions live in an **in-memory, LRU-capped table; a restart resets every counter** (proxy.py:562-566, `PROVEN`). Sessions fragment across subagent fan-out (T-19).
- **Target:** durable SQLite **decision/outcome ledger** — per session: routing decisions, escalation tier, decision→outcome links — surviving restart, keyed to unify fan-out. **First build item of the roadmap (§11).** Verification: restart the proxy mid-session and confirm tier + links persist.

## 7. Security and trust model — `PROVEN` unless noted

**The real guarantee, stated exactly:** no prompt/response text stored; hmac-compared shared bearer token (`hmac.compare_digest` at proxy.py:740 — the shared token carries no per-client identity); loopback binding **enforced, not defaulted** (`security.py`: refuses any wider bind unless the operator passes an explicit acknowledgement flag — "refusing beats warning"); fail-open availability; upstream credentials forwarded untouched so Tamias never persists them. **This is a privacy/availability posture — not a savings, quality, or model-identity guarantee.** (All three councils agreed; no dissent.)

Enumerated bypasses / weaknesses:
- Catch-all passthrough relays **unpriced** (never budget-gated) → it still carries no dollar figure. **F-12 narrowed 2026-10-19:** it now writes one count-only row per request (`route = "passthrough"`, tokens/cost NULL) and `tamias report` counts them, so spend no longer escapes *unnoticed*, only *unpriced*. The old "unlogged" reading of `claims.md:48` is superseded.
- Fail-open → silent zero savings; the core feature dies without an alarm (F-13). `PROVEN` mechanism.
- Single shared bearer token; no per-client identity; proxy is a key-relay by design; no auth rate-limit. `PROVEN`.
- `x-tamias-session` caller-supplied (F-25). `PROVEN`.
- Feedback channel (error markers/retries) authored by the graded agent → escalation gameable, calibration poisonable (F-14). `PROVEN` design fact.
- Manifest served, not enforced. `PROVEN`.
- Registry + price sheets are a fleet-wide remote-config surface (`tamias registry update` / `pricing_fetch.py`) with no signing, pinning, provenance, or rollback; staleness silently corrupts STAY/SWITCH and any savings number (F-17). `DESIGNED` concern; surface exists.
- Concurrency/atomicity unexamined (F-15): races on the in-memory session/hysteresis table; TOCTOU on reserve-then-settle (two concurrent requests each read the budget and both pass); proxy death mid-stream hangs the client on a half-delivered response. `UNVERIFIED` advisor claims.
- Data-at-rest exposure (stored keys/budgets/ledger; secrets/PII inside forwarded tool outputs) and unaudited admin-plane authz (`tamias report`, dashboard, keys; F-28, F-29). `UNVERIFIED`.
- Shipped defenses the spec must not omit (F-7): `budget.py` reserve-then-settle, per-model sub-caps, RPM/TPM, per-key identity + revocation (revocation applies to new requests only). Existence `PROVEN`; live behavior `UNTESTED` (`BUDGET.md:109`).
- A silent model rewrite sends the transcript to a vendor the user did not name (privacy/ToS; T-32/F-33). `PROVEN` design consequence; disclosure paragraph `DESIGNED`.
- License: MIT (`LICENSE`; `pyproject.toml:11`). `PROVEN` (spot-checked 2026-10-19).

## 8. Economics and measurement — the load-bearing section

- **F-1, Critical — code half FIXED 2026-10-19 (was CONFIRMED against source earlier that day):** the savings arithmetic used to compute `baseline_cost` by re-pricing **the same tokens** at the requested model, so `saved` was **definitionally non-negative** for a cheaper substitute — **the instrument could not report a savings failure**. **Fixed:** `dashboard.py:_price` now emits `saved = baseline_warm_cost − actual` on a switch (and `0.0` on a non-switched priced row), where `baseline_warm_cost = cost(model_requested, assume_cache_warm(usage))`; `cli.py`'s `_saving` and realised saving do the same and are unclamped; `pricing.assume_cache_warm` is the new primitive. `saved ≤ 0` is asserted by `tests/test_cli.py::test_report_shows_a_negative_saving_when_a_switch_would_have_lost` and `tests/test_dashboard.py::test_a_cache_miss_replay_can_show_a_negative_saving`. The old non-negative same-tokens difference is retained as `saved_upper_bound`. **Still open (F-1/F-5):** extra-turn cost and transcript growth are not in the formula, and no number has been reconciled against a provider invoice — that needs a paid, non-zero-price run.
- **F-2, Critical `[INFERENCE, High]`, zero traffic data:** the headline lever (swap model) destroys prefix cache while re-sent input/context is believed dominant in an agent loop — the per-request delta may be **negative per task**. The single most decision-relevant unknown.
- **Invoice reconciliation** is the cheapest oracle-free ground truth for the *cost* half of the claim: compare the meter to the provider bill; no quality question needed. `DESIGNED` (peer review).
- **F-19:** the experiment itself costs money (both arms of a paired/executing-shadow benchmark) and can rival the savings it tests; budget it and get consent for real-traffic manipulation. `DESIGNED` concern.
- **F-16:** streaming usage undercount may partly explain $0.00/UNKNOWN rows (`claims.md:42` VERIFIED; other causes there: model not in price sheet; provider omits `prompt_tokens_details.cached_tokens`; sheet prices cached_input differently). `PROVEN` known causes, extent `UNTESTED`.
- First run used to print **$0.00** because shipped price sheets were all zero (`claims.md:15` VERIFIED) — indistinguishable from a broken proxy (F-20). **Fixed 2026-10-19:** the package now ships a dated OpenRouter snapshot bundled at `src/tamias/data/prices.default.toml` and the resolver falls back flag → `./prices.toml` → `~/.tamias/prices.toml` → bundle (a `--prices` path given explicitly but missing still exits 2). A clean install prints a non-zero cost line without any price file of its own.

## 9. Coverage matrix (per endpoint × stage) — verified against source 2026-10-19

| Stage | /v1/chat/completions | /v1/messages (Anthropic wire) | /v1/responses |
|---|---|---|---|
| METER | implemented (`PROVEN`) | implemented (`PROVEN`, anthropic_adapter) | implemented (`PROVEN`) |
| CLASSIFY | implemented (messages[-1] rules, `PROVEN`) | implemented — calls `router.decide` (anthropic_adapter.py:427, `PROVEN`) | **never** |
| ROUTE L1 model | implemented in source (`UNTESTED` live; F-6 resolved 2026-10-19 — `active` accepted in every published artifact) | implemented — `router.applied_target`, gated on active mode + allowlist (anthropic_adapter.py:556, `PROVEN`) | **never — meter-only by design** (responses_adapter.py:2-3) |
| ROUTE L2 effort | implemented, fixture-only (`UNTESTED`; proxy.py:872) | **not applied** (`PROVEN` absence; EFFORT.md:29-30) | **not applied** |
| CACHE-GATE (L4) | designed | designed | n/a |
| SHAPE (L3) | designed | designed | designed |
| FORWARD | implemented (`PROVEN`) | implemented (`PROVEN`) | implemented (`PROVEN`) |
| RECONCILE | designed (detect-and-document) | designed | n/a (no rewrite to reconcile) |
| REPORT | implemented; cache-aware, can emit ≤ 0 (F-1 code half FIXED 2026-10-19) | metered rows appear in report (`PROVEN`) | metered rows appear in report (`PROVEN`) |

Anthropic-usage specifics (`PROVEN`, anthropic_adapter docstring): `input_tokens` excludes cache reads/writes → Tamias sums the three; a missing component is UNKNOWN, never zero; stream usage merged from `message_start` + `message_delta` (last non-null wins).
v1.0 of this matrix wrongly said `/v1/messages` routing was "not implemented" — corrected here (F-24 is thereby partially resolved).

## 10. Exit criteria — before this file may be called final or cited

1. The savings number is **falsifiable** (formula can emit ≤0) and reconciles against provider-reported usage/invoice (F-1, F-5). *The formula half is met as of 2026-10-19 (`saved ≤ 0` on a constructed cache-miss replay); the reconciliation half is not.*
2. One **paired benchmark** runs end-to-end per §18: routing off vs on, written task oracle, cache-aware net-delta, turns-to-task gate, `model_requested ≠ model_sent` rows present, task completes in both arms (F-3).
3. The **`active`-mode conflict is resolved** (F-6) — **RECORDED 2026-10-19.** `ROUTER_MODES` accepts `active` in every *published* artifact: TestPyPI `0.1.0a3` is the newest published, and `a1`/`a2`/`a3` all list `("shadow", "active", "off")`. Only the **never-published** `v0.1.0a4`/`v0.1.0a5` tags list `("shadow", "off")`, so "0.1.0a4 rejects `active`" (`claims.md` rows 23/47) describes an unpublished tag, not a release. There is therefore **no source-vs-release conflict on the mode name**. Drift recorded: the published `a3` accepts bare `active` with none of the `a6` guards, while current source (`0.1.0a6`) re-adds `active` fail-closed (the target must be named in `--registry`, tool-capable and priced). Evidence: `git show v0.1.0a1..a5:src/tamias/cli.py | grep ROUTER_MODES`; a downloaded TestPyPI wheel, `tamias/cli.py:50`.

Even after these, the file may be called final **for design**; claims of *savings* additionally require a positive signed net delta from the paired benchmark; claims of *quality non-inferiority* additionally require the off-proxy oracle and held-out grading (§4).

**EVIDENCE GATE — assessed 2026-10-19 (council `20261019-1732`).** The gate is reached after the measurement-honesty phase. Of its six tasks, four are **done**: B1 (real rates, F-20), B3 (`saved` falsifiable, F-1 code half), B4 (passthrough counter, F-12) and B5 (F-6). Two are **unfunded**: B2 (paid provider reconciliation) and B6 (the null-hypothesis arm, F-18). With the phase done only partially and no owner-named buyer, the council's rule resolves to **Option 3 — the governance/metering layer is the finished v1 — with the explicit caveat "budgets UNTESTED" and no savings claim.** The "passthrough leak open (F-12)" caveat the rule attaches no longer applies: F-12 is closed. Reaching Phase D (the paid paired benchmark, §18) still requires the budget and a writable task oracle (F-35). The formal declaration is the owner's; this records the assessment.

## 11. Roadmap and build order (dependency-ordered; `DESIGNED`)

**Phase 0 — documentation truth (DONE 2026-10-19):** this file plus the in-repo §16 dispositions (`README.md`, `docs/ARCHITECTURE.md`, `AGENTS.md`). The remaining §16 items are Desktop files and are owner-gated.

**Phase 1 — measurement honesty (the chairman's concrete next step, run 2321):**
1. Seed real rates into price sheets; first run must not print $0.00 (F-20). Check: fresh install + one request → non-zero cost line. **DONE 2026-10-19** — bundle-and-fallback as in §8; the committed wheel ships `src/tamias/data/prices.default.toml`.
2. Make the meter externally falsifiable: reconcile logged token counts/cost against provider-reported usage for the recorded live runs; recompute `saved` with a cache-aware formula that can emit a negative number (F-1). Check: ≥95% of rows match provider usage within a stated tolerance; a constructed cache-miss replay yields `saved ≤ 0`. **Half DONE 2026-10-19** — the arithmetic is cache-aware (`pricing.assume_cache_warm`; `dashboard.py:_price`; `cli.py:_saving`/realised saving) and a constructed replay asserts `saved ≤ 0` (`tests/test_dashboard.py::test_a_cache_miss_replay_can_show_a_negative_saving`, `tests/test_cli.py::test_report_shows_a_negative_saving_when_a_switch_would_have_lost`). **The provider-reconciliation half is still open** — it needs a paid, non-zero-price live run.
3. Count-only meter on the passthrough route (F-12). Check: proxy-level counter == `tamias report` count. **DONE 2026-10-19** — every catch-all request logs one `route="passthrough"` row with NULL tokens/cost; `tamias report` prints the count (`tests/test_cli.py::test_report_counts_passthrough_rows_without_pricing_them`, `tests/test_hardening_proxy.py::test_passthrough_logs_one_count_only_row`).

**Phase 2 — durable state:** SQLite decision/outcome ledger surviving restart and unifying fan-out (§6). Check: restart mid-session, tier + decision→outcome links persist. Then: DB cap/retention, fail-open events in report (F-13, F-26).

**Phase 3 — task definition + oracle:** write 5 benchmark tasks with pass/fail oracles; if the oracle cannot be written, the benchmark cannot run (F-35) — this ordering is the entire gate.

**Phase 4 — paired benchmark** (§18), cache-aware net delta per completed task (F-2, F-3).

**Phase 5 — levers behind evidence:** then, and only then: executing shadow harness, task segmentation, context shaping (L3), CACHE-GATE calibration (L4), `/v1/responses` routing, effort on Messages route. Escalation ladder last, only after §4 prerequisites.

**Deliberately not doing yet:** default-on `active` or `apply`; escalation ladder before Phases 1–4; publishing the registry as a standalone package (candidate upside, F-17 governance needed first); cache-*gain* prefix-stabilization experiments (cheap follow-up once Phase 4 harness exists).

**Candidate upsides recorded, not committed** (Expansionist, run 2321, carried into the register): sell the shipped governance/control plane (works today, needs no quality oracle); cache *gain* (prefix stabilization) rather than cache avoidance; registry as a versioned distribution artifact; per-process session keying → per-task cost trees. Each needs what it says on the tin before adoption.

## 12. Glossary

- **Task class:** structural bucket of request types (formatting, trivial read, tool-heavy, reasoning-heavy) derived rule-based from the last message. Cheap models are only eligible for inspection-verifiable classes.
- **STAY/SWITCH:** the router's per-request decision; STAY forwards the requested model unchanged.
- **Hysteresis (min_gap):** minimum requests between two switches on one session, preventing ping-pong. `DESIGNED`; value unchosen.
- **Dwell/prefix-growth rule (CACHE-GATE):** only switch when expected dwell length and prefix growth amortize the cache-rebuild cost. `UNTESTED` (F-11).
- **Effort:** `reasoning_effort` parameter; chat-completions route only today (§2 L2).
- **Executing shadow:** a mode that actually runs the proposed cheap model on sampled traffic (vs today's byte-identical forward) to collect counterfactual data. Not built.
- **Off-proxy oracle:** ground-truth task-success signal harvested outside the proxy (CI, tests, commit, human accept). Required for any non-inferiority claim.
- **Cost-per-completed-task:** the only decision-relevant cost metric; supersedes cost-per-request.
- **Fail-open:** on any internal error, forward the request unchanged; the agent never breaks, savings may silently be zero.
- **Response contract:** the set of invariants a rewritten request's response must satisfy or Tamias must document breaking (echoed model, usage shape, tool-call IDs, finish_reason).
- **Decision/outcome ledger:** the planned durable SQLite record of routing decisions, escalation tiers, and outcome signals per session (§6).
- **Pin:** a user-supplied model preference that always wins over routing (guard order, §3 stage 3).

## 13. Unified issue register (successor to the colliding T-1..T-25 and T-13..T-33; full F-1..F-36 table with evidence/fix/done-when/raised-by: `.council/runs/20261018-2321-tamias-final-architecture/chairman.md`)

- **Critical (5):** F-1 baseline not counterfactual (`PROVEN` in source) · F-2 per-task net effect unknown, cache-inversion risk · F-3 circular validation path · F-4 no off-proxy task-success oracle · F-5 zero measured savings, sample size zero.
- **High (13):** F-6 `active` source-vs-release conflict — **RESOLVED 2026-10-19:** every *published* artifact accepts `active`; only the never-published `v0.1.0a4`/`a5` tags reject it (evidence: `git show v0.1.0a1..a5:src/tamias/cli.py`, downloaded TestPyPI `0.1.0a3`) · F-7 governance layer omitted from v0 trust model · F-8 internal contradictions (fixed in this file v1.1) · F-9 T-ID collisions (fixed: this register supersedes) · F-10 stages named-not-specified (partially addressed §3; thresholds still open) · F-11 CACHE-GATE untested · F-12 unlogged passthrough — **FIXED 2026-10-19** (count-only rows; see §2 step 1) · F-13 fail-open silent-zero · F-14 gameable feedback channel · F-15 concurrency/atomicity unexamined · F-16 streaming usage undercount · F-17 registry supply chain · F-18 **null hypothesis never evaluated** — a static cheaper model in the agent's own config, or provider-native routing, may capture the same savings with none of the quality/cache/MITM risk; **no advisor rebutted this**.
- **Medium/Low (18):** F-19 experiment cost/consent · F-20 first run $0.00 · F-21 v0 understated shipped safety (corrected §3) · F-22 repo instructions depend on a file outside the repo (`AGENTS.md:3` → `/home/krish/Desktop/HANDOFF-CODEX.md`, which exists at the Desktop root) · F-23 reconcile-after-stream impossible · F-24 coverage matrix (provided §9) · F-25 session state (§6) · F-26 ops gaps (unbounded DB, no proxy-down fallback, unmeasured TTFT tax, observer overhead not netted) · F-27 auth posture · F-28 data-at-rest · F-29 admin-plane authz · F-30 model-behavior drift, no revalidation cadence/frozen test set · F-31 server-side conversation state (`previous_response_id`, stored conversations) makes mid-conversation substitution unsound — hard-exclude such calls · F-32 commercial vacuum (buyer unstated; market = metered-API only; competitors LiteLLM/Portkey/provider-native unrivaled in the docs) · F-33 privacy/vendor disclosure; estimate-only labels; MIT citation · F-34 one artifact vs four audiences (addressed by §0 map) · F-35 missing apparatus (glossary §12 provided; experiment protocol with assignment unit/power/sample size still open — see §18) · F-36 charter conflict (old ARCHITECTURE.md §5.1 "not a proxy"; superseded by §1).

## 14. Provenance and authority

- **This file:** assembled and v1.1-corrected by the orchestrator; its structure follows council run **20261018-2321** (chairman verdict: do not finalize; publish as Design Spec v1 with exit criteria — adopted here rather than overridden).
- **Council runs** (each with chairman.md, verification.md, transcript.md, report.html):
  - `20261018-1820` — architecture approach: Tamias = rewriter, not orchestrator (orchestrator disqualified: merge = harness change); register T-1..T-25; cache-rebuild can invert savings; model identity load-bearing; transcript portability.
  - `20261018-2248` — quality guard: no quality guarantee observable from inside the proxy; failure undefined; durable outcome ledger first; executing shadow; off-proxy oracle; segmentation; idempotency; feedback channel adversarial; register T-13..T-33.
  - `20261018-2321` — final review (whole-project): Option 3 verdict; F-1..F-36; reviewer consensus ranked Contrarian and First Principles highest; six peer-review-only findings folded in (null hypothesis F-18, concurrency F-15, streaming undercount F-16, registry supply chain F-17, invoice reconciliation §8, experiment cost F-19).
  - `20261019-1732` — finish plan (whole-project): **Option 2 sequence with an Option 3 ceiling** — land the truth, then Phase 1 measurement honesty, then an EVIDENCE GATE. Produced the Phase A/B roadmap in §11 and the artifact-disposition table in §16; its EVIDENCE-GATE verdict is recorded in §10.
- **Other runs:** the same `.council/runs/` root holds **14** run directories in total — the three above, six agent-integration runs (`20261019-0058`, `0131`, `1330`, `1417`, `1550`, `2330`), the finish-plan run, and two SQLite-vs-Postgres evaluations (`20261016-1516`, `20261018-1625`). Only the runs named here bear on identity, governance and build order; the rest settle narrower runtime or agent-coverage choices.
- **Simulation flags:** all council runs used real isolated subagents (5+5+1 per run); no fabricated outputs; run 2321 was clean (no retries); one anonymization deviation noted in run 1820's transcript.
- **Code-truth authority:** `tamias/docs/audit/claims.md` outranks this file on any code question; this file outranks the older briefs/architecture docs on what Tamias *is*.
- **Verification practice:** v1.1 claims about the moving codebase were re-checked 2026-10-19 by grep (dashboard baseline, cli ROUTER_MODES, router markers, effort scope, anthropic routing, session derivation, registry fields, budget/keys existence, LICENSE, 932 collected tests across 48 test files). A final consistency pass the same day re-located the unlogged passthrough route (proxy.py:1257-1275; the claims.md-internal citation 799-802 is stale, claim still holds), corrected the bearer-token comparison to proxy.py:740 (the loopback policy is what security.py enforces — it refuses wider binds, stronger than "default"), and removed a duplicated phrase in §4. All 14 council run directories re-listed and complete (framed-question, advisors, reviews, mapping, aggregate, chairman, verification, transcript, report.html).

## 15. Project file map (repo: `/home/krish/Desktop/tamias`, package `src/tamias/`, 932 tests, MIT)

| File | Role | Status |
|---|---|---|
| `proxy.py` | HTTP server, catch-all + three adapter routes, session table (in-memory, LRU), fail-open | `UNTESTED` live beyond dry runs |
| `router.py` | decide() on messages[-1], ERROR_MARKERS/GENERIC_ERROR_MARKERS, min_gap hysteresis, applied_target | `PROVEN` unit-tested |
| `router_config.py` | RouterConfig incl. opt-in error markers | `PROVEN` |
| `session.py` | derive_session_id (salted digest, first system+first user, `x-tamias-session` precedence, WIRE_CHAT/WIRE_ANTHROPIC) | `PROVEN` |
| `effort.py` | apply_effort; EFFORT_MODES; chat-completions only | `UNTESTED` fixture-only |
| `anthropic_adapter.py` | /v1/messages: meter + model route (decide/applied_target), Anthropic usage merge, allowlist/authorize hooks | `UNTESTED` live; mock tests only |
| `responses_adapter.py` | /v1/responses: meter-only relay, never rewrites | `PROVEN` by design |
| `registry.py` | model metadata as data: context_window/tools/reasoning/efforts | `PROVEN` |
| `pricing.py`, `pricing_fetch.py` | cost arithmetic; remote price fetch (supply-chain surface F-17) | `UNTESTED` live |
| `budget.py`, `keys.py` | budgets (total/sub-caps/RPM/TPM, reserve-then-settle), per-key identity/revocation | `UNTESTED` against live provider |
| `security.py` | loopback-only binding policy (refuses non-loopback bind without explicit ack); bearer-token comparison itself lives in proxy.py:740 | `PROVEN` |
| `store.py`, `dashboard.py`, `cli.py` | request log (SQLite), report/dashboard, CLI (`ROUTER_MODES`, admin, agent-config, doctor, registry) | `UNTESTED` live; report/dashboard saving is cache-aware and can go negative (F-1 code half fixed 2026-10-19; provider reconciliation open) |
| `sse.py`, `upstream.py`, `types.py`, `manifest.py` | streaming relay, upstream client, shared types, manifest (served, not enforced) | `UNTESTED` |
| `docs/audit/claims.md` | the verified-claims ledger (VERIFIED/SUPERSEDED/UNVERIFIED rows) | authority on code truth |
| `docs/EFFORT.md`, `docs/BUDGET.md`, `docs/KEYS.md`, `docs/AGENTS.md` | effort limits; budget/keys semantics; per-agent integration status (all UNVERIFIED) | as stated |
| `tests/` | 48 files, 932 collected (2026-10-19): fixtures + mock upstreams; `mock_upstream.py`, `mock_anthropic_upstream.py` | `PROVEN` fixture-level |
| `TAMIAS-SSOT.md` (this file, repo root) | the single source of truth | v1.1 |

Outside the repo: `/home/krish/Desktop/TAMIAS_PROJECT_BRIEF`-class documents and the old handoff are archived as `*.legacy.md` on the Desktop and in the separate `tamias-agent/` repo (see §16); `/home/krish/Desktop/tamias-agent/` (the old agent harness; **deferred**, used only as a benchmark testbed for §18); `/home/krish/Desktop/.council/runs/` (14 council runs, §14).

## 16. Misrepresenting files — dispositions (applied 2026-10-19; the only open item is the deferred `tamias-agent/` repo)

| File | Problem | Disposition | Status |
|---|---|---|---|
| `README.md` | framing predates router-first identity | rewritten: router-first summary, coverage wording per §1, "not yet on PyPI" banner, link to this file | **DONE** (commit `a68f7e9`) |
| `docs/ARCHITECTURE.md` | §5.1/§23 "not a proxy" charter conflict (F-36); scope freeze still valid | rewritten to a short pointer (cache-semantics detail kept); full v1.2 preserved at `docs/ARCHITECTURE.legacy.md` | **DONE** |
| `AGENTS.md` | destructive `git reset --hard a0f6ca1`; stale 915 baseline; pointed at out-of-repo `/home/krish/Desktop/HANDOFF-CODEX.md` (F-22) | rewritten: destructive command and pointer removed, baseline 932, handoff named only as obsolete legacy | **DONE** (`a68f7e9`) |
| `EXECUTION-REPORT.md` (Desktop) | 0.1.0a4-readiness report framed by an external plan | renamed `EXECUTION-REPORT.legacy.md` with a legacy banner | **DONE** (archived 2026-10-19) |
| `HANDOFF-CODEX.md` (Desktop) | out-of-repo handoff (F-22 source); contents superseded | renamed `HANDOFF-CODEX.legacy.md` with a legacy banner | **DONE** (archived 2026-10-19) |
| `TAMIAS-COMPANY-GATEWAY-CONTEXT.md` (Desktop) | superseded company-gateway brief | renamed `TAMIAS-COMPANY-GATEWAY-CONTEXT.legacy.md` + banner | **DONE** (archived 2026-10-19) |
| `TAMIAS_COMPLETE_PLAN.md` (Desktop) | superseded package-first plan | renamed `TAMIAS_COMPLETE_PLAN.legacy.md` + banner | **DONE** (archived 2026-10-19) |
| `tamias-refs-2026-10-06.bundle` (Desktop) | stale git bundle of refs | renamed `tamias-refs-2026-10-06.legacy.bundle` (bundle untouched) | **DONE** (archived 2026-10-19) |
| `TAMIAS-PROJECT-BRIEF.md` / `CLAUDE-REVIEW-PROMPT.md` (earlier listed as "Desktop") | old framing/brief — these actually live in the separate `tamias-agent/` repo | left in place: `tamias-agent/` is a **deferred** repo used only as the §18 benchmark testbed; not renamed here | **DEFERRED** |
| `tamias-agent/legacy/` for `tamias_agent*.py` | no such scripts exist on the Desktop as of 2026-10-19 | nothing to archive | **N/A** |
| Future `INTEGRATION.md` | — | write from §9/§19 only; never claim "any agent" | rule |

## 17. Decided-against list (do not re-propose without new evidence)

- **Orchestrator architecture** (Tamias decomposes/merges turns): disqualified — merge = harness change (council 1820).
- **Escalation ladder as written**: rejected pending §4 prerequisites (council 2248).
- **Verify-after-forward quality audit**: rejected — streamed tokens cannot be un-forwarded (council 2248).
- **Model-switching on unverifiable classes** (tool-heavy work): rejected as policy — bounds blast radius only, but the risk is accepted only where inspection can verify (council 2248).
- **Treating effort cuts as a safe lever**: rejected — same evidence bar as model switching (council 2248).
- **Calling the project's dollar figures evidence**: rejected — all simulated; "No measured savings" is the project's own VERIFIED claim (`claims.md:12`).
- **Optimizing document-finality over measurement** (First Principles, run 2321): the exit-order is measurement backward (§11); finality follows evidence.

## 18. Benchmark protocol (design sketch; `DESIGNED` — power/assignment details still open, F-35)

- **Design:** paired, same task + same seed, routing **off** (arm A) vs **on** (arm B), N tasks ≥ 5 to start; assignment unit = task; hold the agent, provider, and price sheet fixed.
- **Logged per request:** `model_requested`, `model_sent`, effort sent, tokens (input/output/cache read/cache write), cache hit state, turns, session id, decision + reason, class.
- **Per task:** turns-to-task, completion per the task's oracle, wall time, TTFT aggregate, total cost both arms using the **falsifiable** formula (§8).
- **Gates (all must pass):** (i) ≥1 `model_requested ≠ model_sent` row; (ii) net delta positive after cache-write penalties and extra turns; (iii) turns-to-task not worse than arm A beyond a stated tolerance; (iv) task completes in both arms; (v) cost reconciles against provider-reported usage (Phase 1).
- **Cost/consent:** the benchmark pays for both arms (F-19); run it on tasks the project is already paying for; no real-user traffic without consent.
- **Failure handling:** a failed task in either arm is charged to that arm; UNKNOWN usage stays UNKNOWN and is excluded from means, never imputed as zero.

## 19. Per-agent integration status (copy exactly; `PROVEN` labels from `docs/AGENTS.md` 2026-10-19)

| Agent | Route | Rewrite today | Label |
|---|---|---|---|
| OpenCode | /v1/chat/completions | model + effort (source) | **UNVERIFIED** integration; dry runs only |
| Claude Code | /v1/messages | model only (effort not applied) | **UNVERIFIED**; adapter mock-tested |
| Codex | /v1/responses | **none** — metering only | **UNVERIFIED**; zero savings by design until Responses routing is built |

Allowed claim until §10 is met: *"Tamias meters and can lower per-request model cost for OpenAI-compatible Messages-API traffic; savings are estimated, not yet measured."*

## 20. Update policy for this file

- This file is updated, not replaced, through production. Version bump in the header (v1.1 → …) with a dated change note in the banner.
- Sections whose facts describe the codebase must be re-verified (grep or test) at each version bump; record the date (as in §14 verification practice).
- Tag transitions (`UNTESTED` → `PROVEN`) require the evidence path (test name, claims.md row id, or live-run log). Register rows close only when their "Done when" is met, and the closure is recorded here.
- After production: this file becomes the changelog anchor for the architecture; new levers, routes, and measured results are appended to §2, §9, §8 respectively with their own status tags.
