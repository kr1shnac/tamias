/**
 * Live provider/limit snapshot from tamias' own `GET /limits`.
 *
 * The request log can say which model ran and what it cost, but it cannot say
 * which providers are configured right now, which models are cooling down
 * after a rate limit, or which models are free. Those answers belong to the
 * running proxy, so the panel polls them rather than guessing from model ids.
 *
 * Polling is deliberately asymmetric: a light snapshot (counts, providers,
 * cooling) every few seconds, and the bounded candidate catalogue once a
 * minute. The catalogue is what maps a used model to its provider and tier;
 * the light snapshot keeps the limit state current without shipping hundreds
 * of model records on every tick.
 *
 * An unreachable proxy is normal (tamias may not be running yet): the hook
 * reports `live() === false` and the panel degrades to the request log rather
 * than showing an error.
 */

import { createSignal, onCleanup } from "solid-js";

export interface LimitsProvider {
  name: string;
  base_url: string;
  configured: boolean;
  key_source: string;
}

export interface LimitsCooling {
  provider: string;
  model: string;
  status: string;
  reason: string;
  remaining_seconds: number;
  strikes: number;
}

/** What one provider published about its own request quota, verbatim. */
export interface LimitsQuota {
  provider: string;
  remaining_requests: number | null;
  resets_in: number | null;
}

export interface LimitsPool {
  considered: number;
  candidates: number;
  refused: number;
  listed: number;
  truncated: boolean;
  gates: Record<string, number>;
}

export interface LimitsCandidate {
  model: string;
  provider: string;
  quality: number | null;
  cost_per_mtok: number | null;
  context_window: number | null;
  tools: boolean;
  reasoning: boolean;
}

export interface LimitsRefusal {
  model: string;
  gate: string;
  reason: string;
}

export interface LimitsSnapshot {
  providers: LimitsProvider[];
  unconfigured: string[];
  /** Empty means no provider published one, never "unlimited". */
  quota: LimitsQuota[];
  cooling_down: LimitsCooling[];
  pool: LimitsPool;
  candidates: LimitsCandidate[];
  refused: LimitsRefusal[];
}

export interface CatalogueEntry {
  provider: string;
  /** Blended USD per million tokens, or null when the pool could not price it. */
  cost: number | null;
}

export type LimitsTier = "free" | "paid" | "unknown";

export interface UseLimits {
  snapshot: () => LimitsSnapshot | null;
  live: () => boolean;
  error: () => string | null;
  providerOf: (model: string) => string | null;
  tierOf: (model: string) => LimitsTier;
  refresh: () => void;
}

const DEFAULT_URL = "http://127.0.0.1:8000";
const DEFAULT_POLL_MS = 5000;
const DEFAULT_FULL_MS = 60_000;
/** tamias caps this at 500; asking for the maximum keeps paid models visible. */
const FULL_MODELS = 500;

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function parseSnapshot(value: unknown): LimitsSnapshot {
  const data = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const providers = Array.isArray(data.providers)
    ? data.providers.flatMap((item): LimitsProvider[] => {
        if (!item || typeof item !== "object") return [];
        const row = item as Record<string, unknown>;
        if (typeof row.name !== "string") return [];
        return [
          {
            name: row.name,
            base_url: stringOr(row.base_url, ""),
            configured: row.configured === true,
            key_source: stringOr(row.key_source, "none"),
          },
        ];
      })
    : [];

  const quota = Array.isArray(data.quota)
    ? data.quota.flatMap((item): LimitsQuota[] => {
        if (!item || typeof item !== "object") return [];
        const row = item as Record<string, unknown>;
        if (typeof row.provider !== "string") return [];
        return [
          {
            provider: row.provider,
            remaining_requests: numberOrNull(row.remaining_requests),
            resets_in: numberOrNull(row.resets_in),
          },
        ];
      })
    : [];

  const cooling = Array.isArray(data.cooling_down)
    ? data.cooling_down.flatMap((item): LimitsCooling[] => {
        if (!item || typeof item !== "object") return [];
        const row = item as Record<string, unknown>;
        if (typeof row.model !== "string") return [];
        return [
          {
            provider: stringOr(row.provider, "?"),
            model: row.model,
            status: stringOr(row.status, "?"),
            reason: stringOr(row.reason, ""),
            remaining_seconds: numberOrNull(row.remaining_seconds) ?? 0,
            strikes: numberOrNull(row.strikes) ?? 0,
          },
        ];
      })
    : [];

  const rawPool =
    data.pool && typeof data.pool === "object"
      ? (data.pool as Record<string, unknown>)
      : {};
  const gates =
    rawPool.gates && typeof rawPool.gates === "object"
      ? Object.fromEntries(
          Object.entries(rawPool.gates as Record<string, unknown>).filter(
            (entry): entry is [string, number] =>
              typeof entry[1] === "number" && Number.isFinite(entry[1]),
          ),
        )
      : {};
  const pool: LimitsPool = {
    considered: numberOrNull(rawPool.considered) ?? 0,
    candidates: numberOrNull(rawPool.candidates) ?? 0,
    refused: numberOrNull(rawPool.refused) ?? 0,
    listed: numberOrNull(rawPool.listed) ?? 0,
    truncated: rawPool.truncated === true,
    gates,
  };

  const candidates = Array.isArray(data.candidates)
    ? data.candidates.flatMap((item): LimitsCandidate[] => {
        if (!item || typeof item !== "object") return [];
        const row = item as Record<string, unknown>;
        if (typeof row.model !== "string") return [];
        return [
          {
            model: row.model,
            provider: stringOr(row.provider, "?"),
            quality: numberOrNull(row.quality),
            cost_per_mtok: numberOrNull(row.cost_per_mtok),
            context_window: numberOrNull(row.context_window),
            tools: row.tools === true,
            reasoning: row.reasoning === true,
          },
        ];
      })
    : [];

  const refused = Array.isArray(data.refused)
    ? data.refused.flatMap((item): LimitsRefusal[] => {
        if (!item || typeof item !== "object") return [];
        const row = item as Record<string, unknown>;
        if (typeof row.model !== "string") return [];
        return [
          {
            model: row.model,
            gate: stringOr(row.gate, "?"),
            reason: stringOr(row.reason, ""),
          },
        ];
      })
    : [];

  return {
    providers,
    unconfigured: Array.isArray(data.unconfigured)
      ? data.unconfigured.filter((item): item is string => typeof item === "string")
      : [],
    quota,
    cooling_down: cooling,
    pool,
    candidates,
    refused,
  };
}

function baseUrl(value: string | undefined): string {
  return (value && value.trim() ? value.trim() : DEFAULT_URL).replace(/\/+$/, "");
}

export function useLimits(
  options: {
    url?: string;
    token?: string;
    pollMs?: number;
    fullMs?: number;
  } = {},
): UseLimits {
  const base = baseUrl(options.url ?? process.env.TAMIAS_URL);
  const token = options.token ?? process.env.TAMIAS_TOKEN ?? process.env.TAMIAS_API_KEY;
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;
  const fullMs = options.fullMs ?? DEFAULT_FULL_MS;

  const [snapshot, setSnapshot] = createSignal<LimitsSnapshot | null>(null);
  const [catalogue, setCatalogue] = createSignal<Map<string, CatalogueEntry>>(new Map());
  const [live, setLive] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);

  let lastFullAttempt = Date.now();
  let busy = false;
  let disposed = false;

  async function fetchSnapshot(full: boolean): Promise<void> {
    if (busy || disposed) return;
    busy = true;
    try {
      const response = await fetch(`${base}/limits?models=${full ? FULL_MODELS : 0}`, {
        headers: token ? { authorization: `Bearer ${token}` } : undefined,
        signal: AbortSignal.timeout(2500),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = parseSnapshot(await response.json());
      if (disposed) return;

      setSnapshot(next);
      setLive(true);
      setError(null);

      // Merge into the catalogue rather than replacing it: a light poll has no
      // candidates, and a truncated full poll must not forget a model the panel
      // is still showing.
      const map = new Map(catalogue());
      for (const candidate of next.candidates) {
        map.set(candidate.model, {
          provider: candidate.provider,
          cost: candidate.cost_per_mtok,
        });
      }
      for (const cooling of next.cooling_down) {
        if (!map.has(cooling.model)) {
          map.set(cooling.model, { provider: cooling.provider, cost: null });
        }
      }
      setCatalogue(map);
    } catch (cause) {
      if (disposed) return;
      setLive(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      busy = false;
    }
  }

  const refresh = () => {
    // An empty catalogue means the provider/tier map is still unknown: force a
    // full fetch even if the minute has not elapsed, so a manual refresh after
    // tamias comes up does not leave every provider cell reading "?".
    const full = catalogue().size === 0 || Date.now() - lastFullAttempt >= fullMs;
    if (full) lastFullAttempt = Date.now();
    void fetchSnapshot(full);
  };

  void fetchSnapshot(true);

  const timer = setInterval(() => {
    const full = Date.now() - lastFullAttempt >= fullMs;
    if (full) lastFullAttempt = Date.now();
    void fetchSnapshot(full);
  }, pollMs);

  onCleanup(() => {
    disposed = true;
    clearInterval(timer);
  });

  const providerOf = (model: string): string | null => {
    const entry = catalogue().get(model);
    if (entry) return entry.provider;
    return null;
  };

  const tierOf = (model: string): LimitsTier => {
    const entry = catalogue().get(model);
    if (!entry || entry.cost === null) return "unknown";
    return entry.cost === 0 ? "free" : "paid";
  };

  return { snapshot, live, error, providerOf, tierOf, refresh };
}
