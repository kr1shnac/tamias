# Tamias — Final Architecture

**Version:** 1.2 — Final Baseline  
**Date:** 2026-09-30  
**Status:** FINAL BASELINE FOR IMPLEMENTATION  
**Supersedes:** `TAMIAS-FINAL-ARCHITECTURE-v1.1.md` and earlier architecture/review documents.

> **Freeze rule:** This document freezes the architecture and research design. No new subsystem, decision variable, product surface, or experimental family is added without evidence from implementation or experiments. Bug fixes, parser corrections, provider-semantics corrections, and reproducibility amendments are allowed. Any new scope must remove equal scope.

> **Important:** “Final” means that all currently identified architectural issues have been resolved into explicit boundaries, unknown states, gates, or amendments. It does not mean provider behavior or external research can never change after 30 September 2026.

---

# 0. Executive Decision

## 0.1 What Tamias is

Tamias is a **transition-aware inference economics system for coding agents**.

It has one central question:

> **At a live coding-agent checkpoint, what does changing inference configuration `(model, effort)` cost, what does it buy in verified task progress, and when is the change better than staying with the current configuration?**

The system deliberately separates three stages:

```text
OBSERVE
  ↓
EXPERIMENT
  ↓
CONTROL — only if earned by evidence
```

The initial product is an observation instrument. The research contribution is the controlled transition study. The autonomous scheduler is conditional.

## 0.2 Final architecture decision

**Freeze this architecture and begin implementation.**

The architecture no longer depends on any of the following being true:

- every cache miss is caused by a model or effort change;
- every provider uses one TTL rule;
- effort is logged in every client version;
- effort changes always rebuild cache;
- cache-write tokens are always equal to rebuilt-prefix size;
- a parent run's remaining tail is a valid STAY counterfactual;
- replayed trajectories are equivalent to live branches;
- public SWE-bench is a perfect ground-truth benchmark;
- a scheduler must exist for the project to succeed.

Each uncertainty is now represented explicitly as observed data, evidence, or `UNKNOWN`.

---

# 1. Problem Definition

## 1.1 Central research question

> **During an ongoing coding task, when is changing the inference configuration `(model, effort)` worth its transition cost relative to staying with the current configuration?**

The research includes:

- model choice;
- provider-native reasoning effort;
- effort-control mechanism;
- cache state and cache semantics;
- continuation/thinking state;
- explicit handoff cost;
- remaining behavioral cost;
- verified task outcome;
- transition timing.

The research does **not** require memory, RAG, semantic caching, reinforcement learning, multi-agent orchestration, or a proxy/gateway.

## 1.2 Product statement

> **Tamias is a local, read-only coding-agent cost observability tool that reconstructs cache/rebuild events from session logs, prices measurable exposure, attributes likely causes with evidence, and ranks the most expensive rebuild events.**

## 1.3 Research statement

> **Tamias measures the causal effect of changing `(model, effort)` at a pre-specified live coding-agent checkpoint by comparing independent STAY, effort-only, model-only, and combined continuations from an identical prefix.**

## 1.4 Future statement

> **If transition effects generalize on held-out tasks, Tamias may become a transition-aware inference scheduler that selects `STAY` or `SET_CONFIG(model, effort)` when the expected quality benefit exceeds behavioral and transition costs by a pre-registered margin.**

## 1.5 Claims Tamias does not make

Do not claim:

- first;
- only;
- universally novel;
- universally agent-agnostic;
- universally cache-aware;
- optimal;
- zero quality loss;
- exact causal transition cost from logs alone;
- that all cache rebuilds are model/effort transitions;
- that a routing dashboard is itself a new routing algorithm;
- that the scheduler is useful until held-out validation demonstrates it.

---

# 2. Contribution Boundary

The surrounding field already contains substantial work on:

- model routing;
- step-level routing;
- session-aware routing;
- cache-aware switching;
- effort routing;
- handoff cost;
- trajectory branching/replay evaluation.

For example, vLLM Semantic Router's Session-Aware Agentic Routing explicitly models session continuity, safe switch boundaries, and prefix-cache-aware switching; this means “session-aware cache-aware routing” is not a Tamias novelty claim. [1]

Handoff Tax directly studies quality and cost consequences of continuing trajectories produced by another model. [2]

Replay Gap demonstrates why live branching with same-model controls is necessary for evaluating agentic model switching. [3]

Prompt-Induced Waste shows that effort and harness design materially affect coding-agent cost, reinforcing the need to control the harness rather than treating the model as the only cost variable. [4]

Therefore Tamias makes a deliberately narrow contribution claim:

### Research gap

> **A jointly controlled comparison of `STAY` vs `EFFORT_ONLY` vs `MODEL_ONLY` vs `BOTH` at the same live coding-agent checkpoint, under measured current cache/continuation semantics, with remaining-cost and verified-outcome analysis.**

### Product artifact

> **An evidence-aware rebuild ledger that detects observed cache/rebuild events and ranks their measurable cost without assuming every rebuild was caused by a model or effort change.**

This is a narrow combination, not a claim that every component is individually new.

---

# 3. Architectural Principles

## P1 — Inference configuration is explicit

The user-controllable inference decision is:

```text
InferenceConfig = (model_id, effort_value)
```

`effort_value` is provider-native. There is no universal numeric reasoning-intensity scale.

Examples:

```text
low
medium
high
xhigh
max
null
unknown
```

Only values actually supported by the tested model/surface are legal.

## P2 — Execution mechanism is metadata, not an action

The implementation mechanism that applies an effort value is recorded separately:

```text
ExecutionSemantics = (
    effort_mechanism,
    thinking_mode,
    surface,
    client_version,
    provider_flags
)
```

Examples:

```text
per_message_output_config
request_top_level_effort
provider_native_reasoning_budget
unknown
```

The scheduler does not pretend it can freely change mechanisms. A mechanism is an action only when the tested environment explicitly exposes it as one.

This resolves the previous ambiguity between “configuration” and “how the provider implements that configuration.”

## P3 — Observation precedes attribution

Tamias first detects an **observed rebuild/cache event**.

Only then does it attempt to attribute a cause.

## P4 — Evidence is typed, not collapsed into one confidence score

There is no single numeric “confidence” score.

Instead, evidence is stored by type:

```text
SEMANTIC EVIDENCE
  controlled probe
  observed log
  official documentation
  inference

EVENT EVIDENCE
  vendor/client diagnostic
  deterministic log evidence
  controlled semantic explanation
  inference
  unresolved
```

Different evidence classes answer different questions and must not be falsely ordered as if they were interchangeable.

## P5 — Observed quantities and causal quantities are separate

```text
Observed session data ≠ causal effect
```

The product reports observed quantities. The lab estimates causal differences.

## P6 — STAY is always explicit

No experiment uses the tail of a parent trajectory as the sole STAY counterfactual.

Every checkpoint receives independent STAY continuations.

## P7 — Cost and quality are separate

Cost can be measured continuously.

Task resolution is an outcome.

Lower cost does not imply equal quality.

## P8 — UNKNOWN is a valid result

If a semantic fact, cache state, effort state, rebuild size, or cause cannot be established, Tamias records:

```text
UNKNOWN
```

It does not manufacture certainty.

## P9 — Research complexity must serve an estimand

No module exists merely because it might be useful later.

Every research component must answer a defined measurement question or protect identification of the primary estimand.

---

# 4. Final System Architecture

```text
                           CODING AGENT
                         Claude Code v0.1
                                │
                                ▼
                         LOCAL SESSION LOGS
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ A. TAMIAS OBSERVER                                           │
│                                                               │
│ parser → dedup → canonical ledger                            │
│                    │                                          │
│                    ▼                                          │
│           observed rebuild detector                           │
│                    │                                          │
│                    ▼                                          │
│       event evidence + cause attribution                       │
│                    │                                          │
│                    ▼                                          │
│             pricing engine                                    │
│                    │                                          │
│                    ▼                                          │
│      ranked rebuild ledger / receipts / doctor                │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               │ observed data + semantics
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ B. CONTROLLED RESEARCH LAB                                   │
│                                                               │
│ fixed reference task run                                      │
│          │                                                    │
│          ▼                                                    │
│ pre-registered checkpoint                                     │
│          │                                                    │
│   ┌──────┼─────────┬──────────┐                               │
│   ▼      ▼         ▼          ▼                               │
│ STAY   EFFORT     MODEL      BOTH                             │
│   │      │         │          │                               │
│   └──────┴─────────┴──────────┘                               │
│          │                                                    │
│          ▼                                                    │
│ branch fidelity + sealed scoring                              │
│          │                                                    │
│          ▼                                                    │
│ remaining cost + resolution + transition decomposition        │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               │ validated estimates only
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ C. FUTURE CONTROLLER — CONDITIONAL                           │
│                                                               │
│ state → legal configurations → expected value                 │
│             → STAY / SET_CONFIG                               │
│                                                               │
│ only if held-out validation shows:                            │
│ lower cost per resolved task AND non-inferior resolve rate     │
└───────────────────────────────────────────────────────────────┘
```

## 4.1 Implementation now

Implement:

- Observer;
- semantics registry;
- probe;
- research harness;
- E0–E3;
- analysis.

Do not implement an autonomous scheduler in the first research cycle.

## 4.2 Future controller

The scheduler is a research result, not an architectural prerequisite.

If the scheduler gate fails, Tamias remains a complete measurement/research instrument.

---

# 5. Product v0.1 — Tamias Observer

## 5.1 Product boundary

v0.1 is:

- local;
- read-only;
- no telemetry;
- Claude Code log parser first;
- SQLite-backed;
- token-first;
- evidence-aware;
- safe with the network disabled for scan/report/receipt/doctor.

v0.1 is not:

- a proxy;
- a gateway;
- an autonomous router;
- an LLM classifier;
- a memory system;
- a semantic cache;
- an enterprise control plane.

## 5.2 Day-one user value

Tamias must answer:

> **Which cache/rebuild events in my recent coding sessions were most expensive, what likely caused them, and how strong is the evidence?**

The main output is a **ranked rebuild ledger**, not a generic token dashboard.

Generic usage/token reporting is already available from tools such as ccusage. [5]

## 5.3 CLI

```text
tamias scan

tamias report [--last N | --session ID | --since DATE]

tamias receipt <session> [--event N]

tamias doctor

tamias probe              # opt-in, provider-specific, capped
```

Optional:

```text
tamias prices
```

Pricing information must also be visible from `report` so the primary workflow remains compact.

## 5.4 Product report

```text
SESSION
  total tokens
  input/output tokens
  cache read tokens
  cache write 5m / 1h tokens
  API-equivalent cost

REBUILD LEDGER
  timestamp
  observed event type
  normalized cause
  evidence type
  model before / after
  effort before / after when known
  effort mechanism when known
  cache state when known
  realized cache/write exposure
  reconstructed incremental exposure when justified
  unknown fields

TOP EVENTS
  ranked by measurable incremental exposure

DIAGNOSTICS
  parser anomalies
  stale semantics
  unsupported fields
  effort not recorded
  price-sheet mismatch
  unresolved causes
```

A report must distinguish:

```text
observed billed cost
estimated cache exposure
causal effect
```

These are not interchangeable.

## 5.5 Product receipt

Example:

```text
14:05  OBSERVED REBUILD
model: A → B
cause: MODEL
basis: vendor/client diagnostic

cache state: warm before transition
estimated incremental cache exposure: ~$0.42
basis: provider-supported prefix reconstruction

full next-request realized cost: $1.07
quality: not assessed
```

Unknown example:

```text
14:05  OBSERVED REBUILD
cause: UNKNOWN
basis: cache-read drop; no provider cause available
incremental exposure: UNKNOWN
```

Never label `T_obs` as “the cost of the switch” without causal evidence.

## 5.6 Subscription users

All dollar values are:

> **API-equivalent list-price estimates, not subscription bills.**

Raw token quantities are always shown beside dollar estimates.

---

# 6. Canonical Ledger

The canonical ledger is the shared data contract between Observer and Lab.

## 6.1 Request record

```text
run_id
session_id
seq
ts_utc
request_id
message_id
is_sidechain
request_bucket                 # main | subagent | compaction | unknown

agent
agent_version
provider
surface
model_id
model_source                   # explicit | session | fallback | skill | command | unknown

effort                         # nullable

effort_source                  # recorded | inferred | unknown
effort_mechanism               # per_message | top_level | native_budget | unknown
thinking_mode                  # adaptive | enabled | disabled | between_tools | unknown

speed_or_service_tier          # nullable
fallback_flag                  # nullable
fallback_target_model          # nullable

input_tokens
output_tokens
cache_read_tokens
cache_write_tokens
cache_write_5m_tokens          # nullable
cache_write_1h_tokens          # nullable
cache_write_total_tokens       # derived if split unavailable

tool_names

test_signal { ran, passed }

cache_miss_reason              # nullable, provider/client-specific
cache_diagnostic_source        # vendor | log | inferred | unknown
compaction_signal              # nullable
image_or_context_trim_signal   # nullable

raw_record_hash
parser_version
```

## 6.2 Why each non-obvious field exists

| Field | Why it is necessary |
|---|---|
| `request_id`, `message_id` | Deduplication and reproducibility |
| `request_bucket` | Separate main requests from subagents/compaction |
| `model_source` | Distinguish user choice, fallback, skill/command override |
| `effort_source` | Prevent inferred effort from being presented as observed fact |
| `effort_mechanism` | Mechanism affects cache behavior |
| `thinking_mode` | Thinking configuration can invalidate or preserve cache differently |
| cache 5m/1h split | Pricing and cache economics differ by bucket |
| service tier | Pricing/behavior may differ by service/speed mode |
| fallback fields | Automatic model fallback is not the same causal event as user switching |
| cache diagnostics | Direct provider/client event evidence is stronger than inference |
| compaction/trim flags | Non-model causes of observed rebuilds |
| parser version | Reproducibility after parser changes |

No field is added merely for future convenience.

## 6.3 Privacy

The product ledger never persists:

- prompt text;
- source-code text;
- tool arguments;
- raw assistant text;
- full transcript content.

The research harness is separate and may store experimental transcripts only under its own research data policy.

## 6.4 Deduplication

Primary key:

```text
(message_id, request_id)
```

When unavailable:

- use a provider-specific deterministic fallback;
- record which fallback was used;
- never silently merge uncertain records.

Parser requirements:

- deduplicate across log files;
- handle subagent logs independently;
- preserve original model id while allowing normalized aliases;
- inspect zero-usage/synthetic records;
- report unsupported/dropped records.

---

# 7. Observed Rebuild Event Model

## 7.1 Primitive

The primitive is:

```text
ObservedRebuildEvent
```

not `MODEL_CHANGE`.

## 7.2 Event fields

```text
event_id
session_id
prev_request_id
next_request_id
gap_s
from_model
to_model
from_effort
to_effort
from_effort_mechanism
to_effort_mechanism
observed_rebuild           # yes | no | unknown
observed_flags[]
raw_cause_labels[]
normalized_primary_cause
cause_basis
evidence_source
rebuild_size
rebuild_size_basis
incremental_cache_exposure
exposure_basis
```

## 7.3 Raw event flags

The normalized schema is extensible.

Known categories include:

```text
MODEL_CHANGE
EFFORT_CHANGE
IDLE_EXPIRY
COMPACTION
TOOLSET_OR_MCP_CHANGE
PLUGIN_CHANGE
FAST_MODE_CHANGE
IMAGE_OR_CONTEXT_TRIM
RESUME_OR_SESSION_RESTORE
CLIENT_UPGRADE
AUTOMATIC_FALLBACK
SKILL_OR_COMMAND_MODEL_OVERRIDE
PROVIDER_CACHE_ANOMALY
UNKNOWN
```

Provider-specific causes may be added under:

```text
raw_cause_labels[]
```

without forcing them into a false universal taxonomy.

## 7.4 Normalized cause

```text
MODEL
EFFORT
EXPIRY
COMPACTION
TOOLSET
CONTEXT_CHANGE
FALLBACK
RESUME
UPGRADE
PROVIDER_ANOMALY
UNKNOWN
```

When multiple causes occur simultaneously, retain all observed flags.

Only choose a single primary cause when the evidence supports it.

Otherwise:

```text
normalized_primary_cause = UNKNOWN
```

## 7.5 Evidence model

### Event cause evidence

Strongest practical evidence for a real event:

1. provider/client diagnostic attached to the observed record;
2. deterministic direct log evidence;
3. semantics validated by controlled probe + observed change;
4. official documentation plus observed compatible behavior;
5. inference;
6. unresolved.

### Semantics evidence

For whether a configuration *should* preserve cache:

1. controlled probe on exact client/model/surface/mechanism;
2. observed real logs under known configuration;
3. official vendor documentation;
4. inference.

The two hierarchies remain separate.

---

# 8. Cache-State Architecture

## 8.1 No universal TTL model

Tamias must never assume:

```text
previous request wrote 1h → current request has 1h TTL
```

Provider cache semantics vary by:

- provider;
- model;
- surface;
- client version;
- effort mechanism;
- thinking configuration;
- feature flags;
- cache bucket;
- compaction behavior;
- tool/system changes.

Current Claude documentation explicitly distinguishes effort mechanisms, thinking configuration, model behavior and preserved-thinking semantics; per-message effort can preserve cache on supported models while top-level effort changes can invalidate cache. [6][7][8]

## 8.2 State sources

Use, in order of preference:

1. observed token fields;
2. provider/client diagnostics;
3. versioned semantics registry;
4. controlled probe;
5. heuristic inference.

## 8.3 First-class states

```text
WARM
COLD
REBUILT
UNKNOWN
```

`UNKNOWN` is returned whenever warm/cold/rebuilt status cannot be established without unsupported assumptions.

## 8.4 Thinking/continuation semantics

Thinking blocks are not universally portable across models.

Current Claude documentation shows that preserved thinking depends on the producing/receiving model relationship and unchanged earlier context; unsupported thinking blocks can be dropped or rejected. [7][8]

Tamias therefore records:

```text
thinking_carry_status =
  preserved
  partially_preserved
  stripped
  rejected
  unknown
```

This is an observed/measured field, not a universal rule.

## 8.5 Rebuild-size estimation

Do not equate:

```text
cache_write_tokens = rebuilt_prefix_size
```

unless the provider's schema guarantees that interpretation for the exact event.

Preferred reconstruction:

```text
if provider-specific prefix alignment is reconstructable:
    estimate rebuilt prefix from prior cacheable context and observed cache reads
elif provider guarantees cache-write fields represent the rebuilt prefix:
    use cache-write field
else:
    rebuilt_prefix_size = UNKNOWN
```

The product uses explicit labels:

```text
observed cache-write exposure
estimated rebuilt-prefix exposure
unknown
```

---

# 9. Pricing Architecture

## 9.1 Dated price sheets

Every price sheet contains:

```yaml
provider:
model:
accessed:
source_url:
input_per_million:
output_per_million:
cache_read_per_million:
cache_write_5m_per_million:
cache_write_1h_per_million:
long_context_tiers: []
speed_or_service_tiers: {}
credits_or_refunds: {}
```

## 9.2 Historical repricing

Store:

```text
original_price_sheet_id
analysis_price_sheet_id
```

Never silently replace historical prices with current prices.

## 9.3 Cache buckets

When 5m and 1h cache-write fields are exposed, price them separately.

## 9.4 Cache exposure quantity

Tamias distinguishes:

```text
C_realized_request
```

actual billed request cost,

from:

```text
E_cache
```

estimated incremental cache exposure relative to a warm-reference condition.

`E_cache` is only reported when its reconstructed prefix and pricing basis are defensible.

A generic formula is:

```text
E_cache = Σ_b [R_b × (p_write_b - p_read_b)]
```

where:

- `b` is a provider-defined cache bucket;
- `R_b` is the estimated rebuilt prefix in that bucket;
- `p_write_b` is the applicable write price;
- `p_read_b` is the applicable read price.

If `R_b` is not identifiable:

```text
E_cache = UNKNOWN
```

This is accounting exposure, not a causal effect.

---

# 10. Provider/Client Semantics Registry

## 10.1 Key

Every entry is keyed by:

```text
provider
model
surface
client_name
client_version
effort_mechanism
thinking_mode
feature_flags
checked_date
```

## 10.2 Example

```json
{
  "provider": "",
  "model": "",
  "surface": "",
  "client": {
    "name": "",
    "version": ""
  },
  "effort_mechanism": "",
  "thinking_mode": "",
  "flags": {},
  "checked": "YYYY-MM-DD",
  "fields": {
    "model_change_preserves_cache": {
      "value": null,
      "evidence": "unknown"
    },
    "effort_change_preserves_cache": {
      "value": null,
      "evidence": "unknown"
    },
    "thinking_blocks_preserved": {
      "value": null,
      "evidence": "unknown"
    },
    "cache_ttl_seconds": {
      "value": null,
      "evidence": "unknown"
    }
  },
  "history": []
}
```

## 10.3 Probe

A probe must:

1. run a fixed-config warm control;
2. change exactly one variable;
3. verify the requested effort actually took effect;
4. record cache reads/writes;
5. repeat the condition;
6. record client/model/surface/version/flags;
7. enforce a hard spend cap.

A probe establishes:

> **what this exact setup did on this date.**

It does not establish a universal vendor law.

---

# 11. Product Commands and Guarantees

## 11.1 `scan`

Input:

- local Claude Code logs.

Output:

- normalized ledger;
- parser diagnostics;
- event records.

Network:

- not required.

## 11.2 `report`

Produces:

- token accounting;
- API-equivalent pricing;
- rebuild ledger;
- top measurable rebuild exposure;
- evidence state;
- unknowns/anomalies.

## 11.3 `receipt`

Explains one observed event.

Never claims quality or causality.

## 11.4 `doctor`

Checks:

- log path;
- parser version;
- unsupported fields;
- semantics age;
- semantics conflicts;
- price-sheet age;
- provider/client version mismatch;
- effort observability;
- cache diagnostics availability.

## 11.5 `probe`

Opt-in only.

Provider-specific.

Hard spending cap.

No hidden telemetry.

---

# 12. Research Lab Boundary

The lab is a separate research instrument and is not part of the production package's public runtime.

```text
lab/
├── harness/
│   ├── loop.py
│   ├── snapshot.py
│   ├── branching.py
│   ├── replay.py
│   └── fidelity.py
├── experiments/
│   ├── e0_semantics.py
│   ├── e1_noise.py
│   ├── e2_arm_calibration.py
│   └── e3_transition.py
├── analysis/
│   ├── estimands.py
│   ├── bootstrap.py
│   ├── power.py
│   ├── censoring.py
│   └── plots.py
└── protocol/
    └── prereg.md
```

The harness must remain a minimal experimental coding-agent loop. It must not become a second production agent framework.

---

# 13. Harness Requirements

The minimum harness supports:

- task execution;
- explicit target `(model, effort)` injection;
- deterministic reference-run checkpoint;
- repository snapshot/restore;
- environment image/digest pinning;
- exact assistant/tool block preservation;
- branch metadata;
- usage ledger;
- replay fidelity checks;
- sealed scoring.

## 13.1 Branch metadata

Every branch records:

```text
parent_run_id
checkpoint_id
branch_id
prefix_hash
replay_success
replay_fidelity_status
snapshot_digest
task_image_digest
execution_seed
model_id
effort
effort_mechanism
thinking_mode
compaction_policy_id
```

## 13.2 Replay fidelity

A branch is valid only if:

- repository state matches;
- prefix hash matches;
- required assistant/tool blocks were preserved exactly;
- environment digest matches;
- tool state matches;
- replay succeeds under the target configuration.

A failed replay is recorded as:

```text
branch_invalidity = true
```

It is not silently dropped.

Replay Gap shows why this is necessary: post-fork agent behavior can diverge dramatically even when the initial state is matched. [3]

---

# 14. Benchmark Strategy

No single benchmark is treated as unquestioned ground truth.

SWE-bench Verified has documented contamination concerns. [9]

SWE-Bench Pro was subsequently audited and significant task-quality issues were reported. [10]

A current SWE-Bench Pro V2 release dated 22 September 2026 removed invalid tasks, introduced a locked evaluation protocol, pristine re-grading, and other safeguards; V2 contains 642 public tasks. [11]

Therefore the final strategy is:

## Primary research set

**Fresh or independently screened coding tasks**, with a held-out split that was not used for tuning policy thresholds.

The primary set should have:

- known repository snapshot;
- deterministic environment;
- pre-validated tests;
- sealed scoring;
- no internet access except the model endpoint when required;
- task hash;
- task-origin metadata.

## Secondary comparability set

**SWE-Bench Pro V2 public split**, using the locked current release and clearly reporting its version/hash.

## Optional historical replication

SWE-bench Verified may be used only as a historical comparability replication.

Do not combine benchmark generations without labels.

---

# 15. Experimental Program

The experiment program is intentionally minimal.

```text
E0 → E1 → E2 → E3
```

E4 scheduler validation exists only if E3 clears its gate.

---

# 16. E0 — Semantics / Cost / Harness Pilot

## Purpose

Validate measurement before increasing sample size.

Check:

- parser;
- ledger;
- pricing;
- provider semantics;
- effort-control mechanism;
- thinking carry;
- cache behavior;
- compaction behavior;
- branch fidelity;
- rough continuation cost.

## Minimum pilot

- 3–5 tasks;
- 2 target configurations;
- repeated fixed-config controls;
- multiple provider-semantic probe runs where applicable.

## Gate

If the environment cannot reliably apply or observe the intended transition:

> **Fix measurement correctness before increasing N.**

---

# 17. E1 — STAY Noise Floor

## Purpose

Measure how much the coding agent varies when configuration does not change.

## Design

For each eligible task/checkpoint:

- identical prefix;
- identical configuration;
- independent environment restores;
- multiple STAY continuations.

## Outputs

```text
discordant resolution rate
cost coefficient of variation
branch divergence rate
replay-fidelity rate
```

The noise floor determines the minimum effect size Tamias can credibly study.

No treatment effect is called meaningful merely because two branches differ.

---

# 18. E2 — Minimal Arm Calibration

E2 is deliberately small.

It is **not** a full model/effort frontier study.

Use only arms needed for E3:

```text
reference/current config
EFFORT target
MODEL target
BOTH target
```

plus the opposite-direction targets if the selected models permit them.

Purpose:

- verify that all target arms execute;
- estimate coarse cost ranges;
- verify outcome instrumentation;
- identify obviously unusable arms;
- feed the E3 budget calculation.

E2 is not a claim of Pareto optimality.

---

# 19. E3 — Final Transition Experiment

E3 is the research centerpiece.

## 19.1 Primary estimand

At pre-specified checkpoint `s`:

```text
ΔC(a) = E[remaining cost | action a, s]
      - E[remaining cost | STAY, s]

ΔP(a) = P(resolve | action a, s)
      - P(resolve | STAY, s)
```

The estimand is conditional on:

- the reference task distribution;
- the checkpoint rule;
- the pinned provider/client configuration;
- the harness;
- branch fidelity;
- the task remaining eligible at checkpoint.

## 19.2 Actions

```text
STAY
EFFORT_ONLY
MODEL_ONLY
BOTH
```

Each action specifies a full target `InferenceConfig`.

Verification is an observation channel, not an action.

## 19.3 Reference trajectory and checkpoint

To eliminate post-treatment checkpoint selection:

1. Start each task under one fixed **reference configuration**.
2. Run the reference trajectory once.
3. The checkpoint is a fixed pre-registered request index: **request 12**.
4. A task is eligible only if the reference trajectory reaches request 12 without an infrastructure failure.
5. Snapshot exactly after request 12.
6. All branches begin from that identical snapshot/prefix.

No checkpoint may depend on:

- whether the task later resolves;
- whether the task becomes “stuck”;
- which treatment wins;
- post-checkpoint outcomes.

### Why request 12

A fixed request index is deliberately less adaptive than “first stuck point.” It sacrifices some semantic richness to gain clean identification and reproducibility.

Secondary analyses may examine context size, test state, or error repetition as covariates, but they do not redefine the primary checkpoint.

## 19.4 Independent STAY controls

At least three independent STAY branches are run from the same checkpoint.

The parent reference trajectory's tail is never used as the only STAY counterfactual.

## 19.5 Replication

Target:

```text
≥ 3 independent continuations per action cell
```

including STAY.

The number of task checkpoints is determined by E1 variance and the pre-registered quality margin.

If the required sample cannot be funded, quality conclusions are explicitly labeled pilot-scale.

## 19.6 Execution order

Randomize branch execution order where infrastructure allows.

Each branch gets:

- its own restored environment;
- its own branch metadata;
- identical initial repository state;
- the same tool availability;
- the same continuation budget.

## 19.7 Thinking-carry

Primary E3 uses the provider's **native continuation semantics**.

Record:

```text
preserved
partially_preserved
stripped
rejected
unknown
```

Do not manually force incompatible thinking blocks merely to make branches look identical.

Current Claude documentation shows that preserved thinking is model-specific and can depend on unchanged prior context; some model changes preserve or drop earlier thinking selectively. [7][8]

This is therefore a recorded causal-context property, not a nuisance to hide.

## 19.8 Compaction

Primary E3 uses one pre-registered compaction policy:

- disable compaction if the harness can safely disable it for the experiment window; **or**
- use the same deterministic compaction threshold and implementation in every branch.

If a branch naturally reaches compaction sooner because its behavior differs, that occurrence is an outcome/cost component.

It is not silently treated as a protocol failure.

## 19.9 Cache-state protocol

At checkpoint and every post-checkpoint request, record where observable:

```text
cache state
cache age
cache mechanism
cache TTL source
cache-read state
cache-write bucket
surface
client version
thinking mode
effort mechanism
```

If warm/cold status is not defensible:

```text
UNKNOWN
```

## 19.10 Budget and censoring

Every branch uses the same pre-registered continuation budget for a given task.

The cap may be based on:

- maximum turns;
- maximum billed tokens;
- maximum cost;

but the rule must be frozen before the test split.

A branch that reaches the cap is:

```text
resolved = false
censored = true
```

It is not excluded.

Report censoring by action cell.

## 19.11 Outcome

Primary quality outcome:

```text
resolved / not_resolved
```

using a sealed scorer.

Secondary outcomes:

- hidden test result;
- patch validity;
- time to first valid patch;
- post-checkpoint tool-call count;
- cost to resolution;
- total continuation cost.

---

# 20. Causal Cost Decomposition

The previous decomposition is retained only after making the terms mutually exclusive.

## 20.1 Deterministic cache exposure

```text
T_cache
```

This is the accounting exposure associated with a known cache reconstruction relative to a warm-reference condition.

It is not itself a causal treatment effect.

## 20.2 Explicit handoff cost

```text
T_handoff
```

Only explicit additional requests whose purpose is handoff/state reconstruction belong here.

Examples:

- explicit summary generation;
- explicit state reconstruction;
- provider-required handoff requests.

## 20.3 Behavioral continuation cost

```text
ΔC_behavior
```

The difference in remaining task cost after removing the mutually exclusive accounting terms above.

## 20.4 Causal total

```text
ΔC = T_cache + T_handoff + ΔC_behavior
```

This identity is valid only when the implementation guarantees that each token/request is assigned to exactly one term.

If a component cannot be uniquely assigned:

```text
component = UNKNOWN
```

and the paper must not pretend the decomposition is exact.

## 20.5 Realized branch cost

Also report:

```text
C_realized(a)
```

because observed total billed cost is a separate quantity from the causal difference.

---

# 21. Statistical Design

## 21.1 Unit of analysis

The independent unit is the **task checkpoint**.

Branches are nested within a checkpoint.

Checkpoints are nested within tasks if future extensions introduce multiple checkpoints.

Do not treat every branch or request as an independent observation.

## 21.2 Primary contrasts

Three confirmatory cost contrasts:

1. `EFFORT_ONLY vs STAY`
2. `MODEL_ONLY vs STAY`
3. `EFFORT_ONLY vs MODEL_ONLY`

Apply Holm correction across these three primary comparisons.

`BOTH vs STAY` is secondary unless separately powered.

## 21.3 Quality

Cost is the primary outcome.

Quality is a non-inferiority guard.

Freeze before test data:

```text
δ = 5 percentage points absolute resolve-rate margin
```

If the study cannot reach the required sample size for a 5-point margin, do not silently loosen the margin after seeing data. Instead:

```text
quality result = exploratory / pilot-scale
```

and avoid a non-inferiority claim.

## 21.4 Inference

Use one of:

- paired task-level bootstrap; or
- mixed-effects modeling if multiple checkpoint strata are introduced.

Bootstrap at the task level.

Do not resample individual branches as if independent.

## 21.5 Power

E1 determines the observed discordance/noise floor.

Then power analysis determines the minimum number of independent task checkpoints needed for:

- the chosen cost effect size of practical interest;
- 5-point quality non-inferiority margin.

No arbitrary fixed `N` is declared before E1.

---

# 22. Scheduler — Future Only

The scheduler is not part of v0.1.

## 22.1 State

A future scheduler may use only validated observable features:

```text
current model
current effort
current cache state
cache age
context utilization
request position
recent test signal
recent failure/repetition signal
budget spent
budget remaining
estimated cost-to-go
```

No semantic LLM classifier is required initially.

## 22.2 Action

```text
STAY
SET_CONFIG(model, effort)
```

## 22.3 Policy

Let:

```text
ΔP_hat(a|s)          estimated resolve/progress benefit
ΔC_behavior_hat(a|s) behavioral cost difference
T_cache(s,a)         measured/reconstructed cache exposure
T_handoff_hat(a)     measured handoff cost
λ                    utility weight
m                    safety margin
```

Then:

```text
G(s,a) =
    λ · ΔP_hat(a|s)
    - ΔC_behavior_hat(a|s)
    - T_cache(s,a)
    - T_handoff_hat(a)
```

Choose a change only if:

```text
max_a G(s,a) > m
```

otherwise:

```text
STAY
```

Use dwell/hysteresis to prevent oscillation.

## 22.4 Scheduler validation gate

The scheduler may enter the product only if, on held-out tasks:

```text
cost per resolved task decreases
AND
resolve rate is non-inferior within δ
```

If the condition fails:

> Tamias remains a successful measurement/research instrument.

---

# 23. Out of Scope

Not core:

- memory/RAG;
- semantic cache;
- prompt compression engine;
- RL;
- multi-agent orchestration;
- parallel execution optimization;
- enterprise SSO/MDM;
- proxy/gateway product;
- credits abstraction;
- universal agent support;
- LLM semantic segmentation;
- automatic phase discovery;
- verification as an action;
- broad cross-provider support.

Potential replication after v1:

- Codex;
- another provider surface;
- another coding-agent harness.

These are validation/replication paths, not prerequisites for the core architecture.

---

# 24. Phase/Segmentation Decision

The previous phase automaton is removed from v0.1 and E1–E3.

Use raw observable covariates:

```text
request index
context utilization
last verification signal
error repetition count
tool class
main/subagent
cache state
```

A semantic phase model can be added only if the first results demonstrate that these raw variables are insufficient for the intended analysis.

---

# 25. Legal Actions Decision

`legal_actions()` is not a v0.1 component.

The research harness uses a static pre-registered arm list.

Future controller code may construct:

```text
legal_actions(state, semantics_registry)
```

but only after transition semantics and scheduler validation exist.

This prevents the registry from silently becoming an unvalidated policy engine.

---

# 26. Handoff Decision

Handoff representation is a measured factor, not the headline contribution.

Primary E3:

```text
provider-native continuation semantics
```

Secondary sensitivity subset, if budget allows:

```text
native/raw
compacted
state-only
restart
```

Do not attempt to reproduce the entire handoff literature.

Handoff Tax already establishes that direction and handoff representation matter. Tamias instead asks how those effects interact with the **joint model/effort decision at a matched checkpoint**. [2]

---

# 27. Product / Research Boundary

## Product answers

> What happened in my real sessions?

## Research answers

> What would happen if I changed configuration at the same checkpoint?

## Future controller answers

> Should I change configuration now?

This separation is mandatory.

---

# 28. Acceptance Criteria

## Product

### A1 — Parsing

Every usage-bearing supported record is either:

- parsed;
- explicitly marked unsupported;
- or reported as anomalous.

### A2 — Cost reconciliation

Reconcile totals against an independent trusted tool where available.

Any discrepancy above the pre-registered tolerance is explained.

### A3 — Event detection

All hand-labeled supported rebuild events are detected.

Unknown events remain visible.

### A4 — Pricing

5m and 1h buckets are priced separately when available.

### A5 — Privacy

No prompt/code text is persisted by product v0.1.

### A6 — Offline

`scan`, `report`, `receipt`, and `doctor` work without network access.

### A7 — Probe safety

Probe is opt-in, capped, versioned, and repeated.

### A8 — Installation

Package installs successfully on the supported development platforms.

### A9 — Explainability

Every dollar output identifies:

```text
source tokens
price sheet
formula
semantic evidence status
```

### A10 — Unknown handling

Unsupported effort/cache/cause information is shown as `UNKNOWN`.

## Research

### R1
Branch replay fidelity is measured.

### R2
Independent STAY noise is measured.

### R3
Checkpoint rule is frozen before test runs.

### R4
Compaction policy is fixed.

### R5
Thinking carry is recorded.

### R6
Task-level clustering is used.

### R7
Censoring is reported, not dropped.

### R8
Primary cost contrasts are preregistered.

### R9
Quality margin is frozen before test data.

### R10
Scheduler validation is held out.

---

# 29. Decision Gates

## Gate A — Measurement validity

If logs cannot support the intended cause attribution:

```text
reduce claim → observed rebuild ledger
```

## Gate B — Effort observability

If effort cannot be observed reliably in real logs:

```text
real-session effort attribution = unavailable
lab effort study = retained if controllable
```

## Gate C — Arm validity

If an arm cannot be applied consistently:

```text
remove arm from E3
```

Do not emulate an unsupported provider feature.

## Gate D — E1 noise

If STAY variance is too large to detect the planned effect:

```text
increase replication or narrow the estimand
```

Do not simply increase claims.

## Gate E — E3 identification

If replay fidelity, cache-state observability, compaction control, or STAY noise is inadequate:

```text
fix measurement before increasing N
```

## Gate F — Scheduler

If no policy lowers cost per resolved task with non-inferior resolution:

```text
no autonomous scheduler release
```

The measurement paper/product remains valid.

---

# 30. Risk Register

| Risk | Control |
|---|---|
| Provider semantics drift | versioned registry + dated re-probe |
| Effort absent from logs | `effort_source=unknown`; lab-only effort claims |
| Model/effort transition does not rebuild cache | measured as finding, not failure |
| Unknown cache cause | evidence-aware attribution |
| Partial rebuild | provider-specific estimator + unknown fallback |
| Thinking carry asymmetry | record actual carry status |
| Compaction divergence | fixed/disabled primary policy |
| Replay divergence | exact prefix fidelity + fresh environments |
| STAY noise | independent STAY branches |
| Checkpoint bias | fixed reference-run request index |
| Regression to mean | no stuck-run selection; independent STAY |
| Pseudoreplication | task-level inference |
| Benchmark contamination | fresh/screened primary + V2 comparability set |
| Winner's curse | held-out scheduler validation |
| Budget explosion | E0/E1-based power and tiered continuation |
| Product duplication | rebuild ledger as headline |
| Privacy leakage | metadata-only product ledger |
| Pricing error | dated price sheets + raw tokens |
| False causal language | observed/causal separation |

---

# 31. Repository Layout

```text
tamias/
├── src/tamias/
│   ├── core/
│   │   ├── ledger.py
│   │   ├── parser.py
│   │   ├── cache_events.py
│   │   ├── cache_state.py
│   │   ├── attribution.py
│   │   ├── pricing.py
│   │   ├── semantics.py
│   │   └── receipts.py
│   ├── sources/
│   │   └── claude_code.py
│   ├── probe/
│   │   └── probe.py
│   └── cli.py
├── registry/
│   ├── prices/
│   └── semantics/
├── lab/
│   ├── harness/
│   ├── experiments/
│   ├── analysis/
│   └── protocol/
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── LOG-SCHEMA.md
│   ├── SEMANTICS.md
│   ├── PREREG.md
│   └── AMENDMENTS.md
├── README.md
├── CITATION.cff
└── LICENSE
```

Only `src/tamias` is the user-facing package in the first release.

The lab is research infrastructure and need not be shipped as the same public package.

---

# 32. Implementation Order

## Phase 1 — Real log correctness

1. inventory real Claude Code logs;
2. verify schema;
3. implement parser;
4. implement global deduplication;
5. implement canonical ledger;
6. implement token extraction;
7. create dated price sheet;
8. implement observed rebuild detection;
9. implement evidence-aware attribution;
10. implement ranked rebuild report;
11. implement receipts;
12. implement doctor.

## Phase 2 — Provider semantics

13. build semantics registry;
14. add versioned documentation evidence;
15. build opt-in probes;
16. verify effort control;
17. verify thinking carry;
18. verify cache state.

## Phase 3 — Research harness

19. minimal coding-agent loop;
20. environment snapshot/restore;
21. replay fidelity;
22. sealed scorer;
23. E0;
24. E1;
25. E2;
26. E3.

## Phase 4 — Conditional scheduler

27. estimate validated transition economics;
28. implement lookup-based policy;
29. tune only on development data;
30. evaluate on held-out tasks;
31. integrate into product only after Gate F.

---

# 33. What Changed From v1.1

### C1 — Configuration/action split

`InferenceConfig=(model, effort)` is now separate from provider execution mechanism metadata.

### C2 — Observed rebuild is the primitive

Model/effort/idle are causes or flags, not the complete event universe.

### C3 — Evidence types separated

Semantic evidence and event-cause evidence no longer compete in one artificial global ranking.

### C4 — Cache TTL is not universal

No single TTL heuristic is treated as truth.

### C5 — Rebuild size is not assumed equal to cache-write size

Provider-specific reconstruction is required.

### C6 — Product output is retrospective rebuild intelligence

No “what would have prevented it” claim unless provider semantics support it.

### C7 — Fixed reference checkpoint

Primary checkpoint is request 12 on a fixed reference trajectory.

### C8 — Independent STAY branches

Parent-run tail is never the sole control.

### C9 — Thinking carry is recorded

It is part of continuation semantics, not hidden as noise.

### C10 — Compaction is controlled

Disabled where possible or deterministic across branches.

### C11 — E2 is reduced to arm calibration

No broad fixed-grid frontier study.

### C12 — Quality margin is fixed in advance

No post-hoc relaxation of non-inferiority criteria.

### C13 — Benchmark strategy changed

Fresh/screened tasks are the primary scientific evidence; current SWE-Bench Pro V2 is a comparability benchmark.

### C14 — Phase automation removed

Raw covariates are sufficient initially.

### C15 — `legal_actions()` is future-only

The registry is not a hidden policy engine.

### C16 — Scheduler remains conditional

The project succeeds scientifically even if the scheduler gate fails.

---

# 34. External Evidence Used For This Freeze

## Primary technical evidence

1. **Anthropic Claude Effort documentation** — current documentation states that effort is provider-native, model-dependent, and that per-message effort can preserve prompt cache on supported models while top-level effort changes can invalidate cache.  
   https://platform.claude.com/docs/en/build-with-claude/effort

2. **Anthropic thinking documentation** — current documentation describes model-specific thinking-block preservation, cache implications, and invalidation behavior when thinking configuration or top-level effort changes.  
   https://platform.claude.com/docs/en/build-with-claude/thinking

3. **Anthropic compaction/preserved-thinking documentation** — current documentation establishes additional conditions under which kept thinking blocks remain valid after compaction and model changes.  
   https://platform.claude.com/docs/en/build-with-claude/compaction-thinking-blocks

4. **vLLM Session-Aware Agentic Routing** — explicitly covers session continuity, safe switching, prefix-cache-aware switch economics, and replayable traces.  
   https://vllm.ai/blog/2026-06-02-session-aware-agentic-routing

5. **ccusage** — demonstrates existing local coding-agent token/cost/cache observability.  
   https://ccusage.com/guide/cost-modes

## Research overlap

6. **The Handoff Tax** — direct prior work on continuing non-native model trajectories, handoff direction, interface, cost and quality.  
   https://arxiv.org/abs/2608.24358

7. **The Replay Gap** — direct prior work motivating live branching, matched controls and replay-fidelity measurement.  
   https://arxiv.org/abs/2608.08239

8. **Control the Harness, Control the Cost** — current prior work on harness-level routing, switch payback and cost governance for coding agents.  
   https://arxiv.org/abs/2609.28919

9. **Prompt-Induced Waste in Large Reasoning Models** — current evidence that effort and harness design materially affect coding-agent cost.  
   https://arxiv.org/abs/2608.01347

## Benchmark evidence

10. **OpenAI: SWE-bench Verified no longer measures frontier coding capability** — documented contamination concerns.  
    https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/

11. **OpenAI: Separating signal from noise in coding evaluations** — audit of SWE-Bench Pro and widespread task-quality issues in the earlier version.  
    https://openai.com/index/separating-signal-from-noise-coding-evaluations/

12. **SWE-Bench Pro V2** — September 22, 2026 refreshed public split with 642 tasks, removed invalid tasks, locked evaluation protocol, and pristine re-grading.  
    https://labs.scale.com/leaderboard/swe_bench_pro_public_v2

---

# 35. Final Freeze Checklist

Before implementation begins, run this checklist once:

```text
[ ] Real Claude Code log schema verified
[ ] Effort field presence/absence verified
[ ] Cache 5m/1h fields verified
[ ] Client version captured
[ ] Provider/surface captured
[ ] Price sheet pinned
[ ] Semantic registry initialized
[ ] Probe mechanism tested
[ ] Observed rebuild detector tested
[ ] Unknown paths tested
[ ] Product does not persist prompt/code text
[ ] E0 task/harness succeeds
[ ] Branch snapshot is reproducible
[ ] Thinking carry is recorded
[ ] Compaction policy is fixed
[ ] Checkpoint request 12 is implemented
[ ] Independent STAY branches implemented
[ ] Censoring implemented
[ ] Sealed scorer validated
[ ] Task-level bootstrap implemented
[ ] Quality margin frozen in preregistration
[ ] Benchmark version/hash recorded
[ ] Held-out scheduler gate defined
```

If any checkbox fails, **fix that component; do not redesign Tamias.**

---

# 36. Final Definition

> **Tamias is a transition-aware inference economics system for coding agents. It measures real cache/rebuild events, experimentally determines what changing model and effort buys and costs relative to staying at the same live checkpoint, and only then earns the right to automate those decisions.**

The architecture is therefore complete at the required breadth:

```text
REAL OBSERVATION
      ↓
REBUILD / CACHE EVIDENCE
      ↓
CONTROLLED TRANSITION ECONOMICS
      ↓
HELD-OUT VALIDATION
      ↓
OPTIONAL SCHEDULER
```

No additional subsystem is required for the core research question, first product, or first paper.

---

# 37. Final Engineering Rule

From this point forward:

> **Do not improve the architecture by adding features. Improve it by making the measurement more correct, the experiment more identifiable, and the evidence more reproducible.**

