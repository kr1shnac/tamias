# Tamias — architecture pointer

> **This file is a pointer, not the architecture.** On *what Tamias is*, the
> single source of truth is [`TAMIAS-SSOT.md`](../TAMIAS-SSOT.md) (Design Spec
> v1.1). On *what the code actually does*, the authority is
> [`docs/audit/claims.md`](audit/claims.md). Read those first.
>
> This file previously carried the frozen v1.2 "Final Architecture"
> (2255 lines) for an earlier read-only cost-observability product. That
> document was superseded on product identity (F-36) and is preserved verbatim
> at [`docs/ARCHITECTURE.legacy.md`](ARCHITECTURE.legacy.md); it is not an
> authority on what Tamias is today. Section references in the SSOT to
> `ARCHITECTURE.md §5.1` / `§23` / `§24` (the old "not a proxy" charter and the
> scope freeze) now resolve inside that legacy file. What is kept below is the
> one thing this document was still cited for: **cache semantics**.

## Where to read what

| Question | Read |
| --- | --- |
| What is Tamias? What is `PROVEN`? | [`TAMIAS-SSOT.md`](../TAMIAS-SSOT.md) |
| What does the code actually do? | [`docs/audit/claims.md`](audit/claims.md) |
| Routes, wire protocols, coverage | [`docs/ROUTES.md`](ROUTES.md), [`README.md`](../README.md) |
| Routing decisions and modes | [`docs/ROUTING.md`](ROUTING.md) |
| Pricing and dated sheets | [`docs/PRICING.md`](PRICING.md) |
| Effort lever | [`docs/EFFORT.md`](EFFORT.md) |
| Keys, budgets, manifests | [`docs/KEYS.md`](KEYS.md), [`docs/BUDGET.md`](BUDGET.md), [`docs/MANIFEST.md`](MANIFEST.md) |
| Release state | [`docs/RELEASE-STATUS.md`](RELEASE-STATUS.md) |
| Full historical architecture | [`docs/ARCHITECTURE.legacy.md`](ARCHITECTURE.legacy.md) |

## Cache semantics (kept from the frozen architecture)

The sections below are the reason this file was retained. They describe how
cache state is modelled and priced; the implementation and the exact fields it
writes are in the code and in `docs/audit/claims.md`.

### Event flags and normalized causes

Observed context invalidation is recorded as raw event flags, then optionally
normalised to a single primary cause — or `UNKNOWN` when the evidence does not
support one cause. Known raw categories include:

```text
MODEL_CHANGE  EFFORT_CHANGE  IDLE_EXPIRY  COMPACTION  TOOLSET_OR_MCP_CHANGE
PLUGIN_CHANGE  FAST_MODE_CHANGE  IMAGE_OR_CONTEXT_TRIM  RESUME_OR_SESSION_RESTORE
CLIENT_UPGRADE  AUTOMATIC_FALLBACK  SKILL_OR_COMMAND_MODEL_OVERRIDE
PROVIDER_CACHE_ANOMALY  UNKNOWN
```

Provider-specific causes may be added under `raw_cause_labels[]` without forcing
them into a false universal taxonomy. When multiple causes occur at once,
retain all observed flags; set a primary cause only when the evidence supports
it, otherwise `UNKNOWN`.

### No universal TTL model

Tamias must never assume that "previous request wrote 1h → current request has
1h TTL". Cache lifetime varies by provider, model, surface, client version,
effort mechanism, thinking configuration, feature flags, cache bucket,
compaction behaviour, and tool/system changes. Per-message effort can preserve
cache on some models while a top-level effort change can invalidate it.

### State sources (in order of preference)

1. observed token fields;
2. provider/client diagnostics;
3. versioned semantics registry;
4. controlled probe;
5. heuristic inference.

### First-class cache states

```text
WARM  COLD  REBUILT  UNKNOWN
```

`UNKNOWN` is returned whenever warm/cold/rebuilt status cannot be established
without unsupported assumptions.

### Rebuild-size estimation

Never equate `cache_write_tokens` with the rebuilt-prefix size unless the
provider's schema guarantees that for the exact event. Prefer reconstructing
the rebuilt prefix from prior cacheable context and observed cache reads; use
the cache-write field when the provider guarantees it means the rebuilt prefix;
otherwise the value is `UNKNOWN`. The product labels these explicitly as
*observed cache-write exposure*, *estimated rebuilt-prefix exposure*, or
*unknown*.

### Cache pricing

- When 5m and 1h cache-write fields are exposed, price them separately.
- Tamias separates `C_realized_request` (actual billed request cost) from
  `E_cache` (estimated incremental cache exposure relative to a warm reference):

  ```text
  E_cache = Σ_b [ R_b × (p_write_b − p_read_b) ]
  ```

  where `b` is a provider cache bucket, `R_b` the estimated rebuilt prefix in
  that bucket, and `p_write_b`/`p_read_b` the applicable write/read prices. If
  `R_b` is not identifiable, `E_cache = UNKNOWN`. This is accounting exposure,
  **not** a causal effect.
