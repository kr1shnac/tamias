/**
 * Minimal TOML reader for tamias price sheets (`~/.tamias/prices.toml`).
 *
 * The sheet is small and flat: top-level `date`/`simulated` scalars, a
 * `[provenance]` table we ignore, and one `["model-id"]` table per model with
 * numeric rates. A hand-rolled reader keeps the plugin dependency-free — a
 * real TOML parser would be overkill for this shape.
 *
 * This module deliberately does *not* try to reproduce tamias' full billing
 * arithmetic. The proxy stores the computed list price on every row; that
 * stored value is authoritative. The arithmetic here only estimates a
 * baseline for a model that was **not** run, which is necessarily an estimate
 * and is labelled as one in the UI.
 */

export interface ModelRates {
  input?: number;
  output?: number;
  cached_input?: number;
  cache_write?: number;
  cache_write_1h?: number;
  /** Tiered pricing the simple arithmetic cannot model; the cost stays unknown. */
  tiers?: string[];
}

export interface PriceSheet {
  date: string;
  simulated: boolean;
  models: Record<string, ModelRates>;
}

export type Tier = "free" | "paid" | "unknown";

const RATE_KEYS = new Set([
  "input",
  "output",
  "cached_input",
  "cache_write",
  "cache_write_1h",
]);

function unquote(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function stripInlineComment(value: string): string {
  let inQuotes = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === '"') inQuotes = !inQuotes;
    else if (ch === "#" && !inQuotes) return value.slice(0, i);
  }
  return value;
}

function sectionName(line: string): string | null {
  const match = line.match(/^\s*\[([^\]]*)\]\s*$/);
  return match ? unquote(match[1] ?? "") : null;
}

export function parsePriceSheet(content: string): PriceSheet {
  const models: Record<string, ModelRates> = {};
  let date = "";
  let simulated = false;
  let current: string | null = null;

  for (const rawLine of content.split("\n")) {
    const line = rawLine.trim();
    if (line.length === 0 || line.startsWith("#")) continue;

    const section = sectionName(line);
    if (section !== null) {
      current = section;
      continue;
    }

    const eq = line.indexOf("=");
    if (eq < 0) continue;

    const key = unquote(line.slice(0, eq));
    const rawValue = stripInlineComment(line.slice(eq + 1)).trim();

    if (current === null) {
      if (key === "date") date = unquote(rawValue);
      else if (key === "simulated") simulated = rawValue === "true";
      continue;
    }

    // `[provenance]` carries free-text metadata, not rates.
    if (current === "provenance") continue;

    const rates = (models[current] ??= {});
    if (key.toLowerCase().startsWith("tier")) {
      rates.tiers ??= [];
      rates.tiers.push(key);
      continue;
    }
    if (!RATE_KEYS.has(key)) continue;

    const parsed = Number(rawValue);
    if (!Number.isFinite(parsed) || parsed < 0) continue;
    (rates as Record<string, number | string[] | undefined>)[key] = parsed;
  }

  return { date, simulated, models };
}

export interface TokenUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cache_write_tokens: number | null;
}

/**
 * Free/paid from the quoted sheet rates, never from a row's total: a paid
 * model with zero tokens still costs a nonzero rate, and a free model stays
 * free even when a request carried no usage block.
 */
export function tierFor(
  model: string | null | undefined,
  sheet: PriceSheet | null | undefined,
): Tier {
  if (!model || !sheet) return "unknown";
  const rates = sheet.models[model];
  if (!rates || rates.tiers?.length) return "unknown";
  if (rates.input === undefined || rates.output === undefined) return "unknown";
  const quoted = [
    rates.input,
    rates.output,
    rates.cached_input,
    rates.cache_write,
    rates.cache_write_1h,
  ].filter((value): value is number => value !== undefined);
  return quoted.every((value) => value === 0) ? "free" : "paid";
}

/**
 * List-price arithmetic for one request, or null when the sheet cannot answer.
 *
 * The cache arithmetic matches the shape of tamias' rules: `input_tokens` is
 * the whole prompt, so cached and write buckets are carved out of it rather
 * than added on top. Missing rates and inconsistent counts return null instead
 * of inventing a number.
 */
export function listCost(
  model: string | null | undefined,
  usage: TokenUsage,
  sheet: PriceSheet | null | undefined,
): number | null {
  if (!model || !sheet) return null;
  const rates = sheet.models[model];
  if (!rates || rates.tiers?.length) return null;
  if (rates.input === undefined || rates.output === undefined) return null;

  const quoted = [
    rates.input,
    rates.output,
    rates.cached_input,
    rates.cache_write,
    rates.cache_write_1h,
  ].filter((value): value is number => value !== undefined);
  // A model whose quoted rates are all zero is exactly free, even when the
  // upstream omitted usage. No invented number is involved.
  if (quoted.every((value) => value === 0)) return 0;

  const input = usage.input_tokens;
  if (input === null || input < 0) return null;
  const output = usage.output_tokens;
  if (output === null && rates.output > 0) return null;
  if (output !== null && output < 0) return null;

  const cached = usage.cached_input_tokens;
  const cachedRate = rates.cached_input ?? rates.input;
  if (cached === null && cachedRate !== rates.input) return null;
  if (cached !== null && cached < 0) return null;

  const writes = usage.cache_write_tokens ?? 0;
  if (writes < 0) return null;
  if (writes > 0 && rates.cache_write === undefined) return null;

  const cachedTokens = cached ?? 0;
  if (cachedTokens + writes > input) return null;

  const writeRate = rates.cache_write ?? rates.input;
  const uncached = input - cachedTokens - writes;
  const cost =
    uncached * rates.input +
    cachedTokens * cachedRate +
    writes * writeRate +
    (output ?? 0) * rates.output;
  return cost / 1_000_000;
}

/**
 * What a switched request would have cost on the requested model with the
 * prompt cache already warm. The whole prompt is treated as a cache read and
 * no cache is rewritten, which is the most conservative baseline for a switch.
 */
export function warmBaselineCost(
  model: string | null | undefined,
  usage: TokenUsage,
  sheet: PriceSheet | null | undefined,
): number | null {
  if (usage.input_tokens === null) return listCost(model, usage, sheet);
  return listCost(
    model,
    {
      ...usage,
      cached_input_tokens: usage.input_tokens,
      cache_write_tokens: 0,
    },
    sheet,
  );
}
