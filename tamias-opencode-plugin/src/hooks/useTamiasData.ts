/**
 * Read-only view of the tamias request log.
 *
 * The hook keeps one SQLite connection open for the life of the panel and
 * reads only the newest window of chat rows, newest first. The old dashboard
 * re-opened the database and scanned the whole table every second; the log is
 * append-only and can grow without bound, so that work grew with history the
 * panel never showed.
 *
 * Cost provenance is preserved rather than flattened:
 *   - `provider_cost_usd` is what the provider said it billed (a measurement)
 *   - `computed_list_cost_usd` is tamias' list-price arithmetic for the used model
 *   - `cost_usd` is legacy data whose source cannot be proven
 * The routed cost prefers those stored values and only falls back to the
 * current price sheet for old rows. A baseline for a model that was *not* run
 * is necessarily an estimate from the current sheet, and is labelled as one.
 */

import { createSignal, onCleanup, onMount } from "solid-js";
import { Database } from "bun:sqlite";
import { existsSync, readFileSync, statSync } from "node:fs";
import {
  listCost,
  localSheetPath,
  mergePriceSheets,
  parsePriceSheet,
  tierFor,
  warmBaselineCost,
  type PriceSheet,
  type Tier,
  type TokenUsage,
} from "../utils/priceSheet";
import { clock } from "../utils/format";

export const DEFAULT_ROW_LIMIT = 500;

export interface TamiasRow {
  id: number;
  ts: string | null;
  session: string;
  project: string | null;
  model_requested: string;
  model_used: string;
  decision: string;
  decision_reason: string;
  decision_target_model: string | null;
  effort_requested: string | null;
  effort_used: string | null;
  router_mode: string | null;
  route: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cache_write_tokens: number | null;
  provider_cost_usd: number | null;
  computed_list_cost_usd: number | null;
  legacy_cost_usd: number | null;
  cost_source: string | null;
  price_sheet: string | null;
  price_simulated: boolean;
  latency_ms: number | null;
  status: string;

  // Derived for the table, so rendering never rebuilds a Date or a tier.
  time: string;
  switched: boolean;
  shadow: boolean;
  failed: boolean;
  actual: number | null;
  actual_source: "billed" | "list" | "legacy" | "computed" | null;
  baseline: number | null;
  baseline_source: "actual" | "sheet" | null;
  saved: number | null;
  priced: boolean;
  tier: Tier;
}

export interface TamiasSummary {
  n_requests: number;
  n_priced: number;
  n_unpriced: number;
  n_switched: number;
  n_ok: number;
  n_failed: number;
  n_free: number;
  n_paid: number;
  n_unknown_tier: number;
  actual_total: number;
  baseline_total: number;
  saved_total: number;
  saving_pct: number | null;
  estimated: boolean;
  latency_avg: number | null;
}

export interface TamiasSnapshot {
  rows: TamiasRow[];
  summary: TamiasSummary;
  /** Every row in the table, including passthrough. */
  total_rows: number;
  /** Count-only rows the proxy never meters. */
  passthrough_rows: number;
  /** Metered rows in the table (the window may be smaller). */
  chat_rows: number;
  /** Whether the newest window is smaller than the metered history. */
  truncated: boolean;
}

interface RawRow extends Record<string, unknown> {}

/** Column aliases, so a schema drift in tamias never breaks the reader. */
const COLUMNS: Array<[string, string[]]> = [
  ["id", ["id"]],
  ["ts", ["ts"]],
  ["session", ["session_id"]],
  ["model_requested", ["model_requested"]],
  ["model_used", ["model_used"]],
  ["decision", ["decision_action", "decision"]],
  ["decision_reason", ["decision_reason"]],
  ["decision_target_model", ["decision_target_model"]],
  ["effort_requested", ["effort_requested"]],
  ["effort_used", ["effort_used"]],
  ["router_mode", ["router_mode"]],
  ["route", ["route"]],
  ["input_tokens", ["input_tokens"]],
  ["output_tokens", ["output_tokens"]],
  ["cached_input_tokens", ["cached_input_tokens"]],
  ["cache_write_tokens", ["cache_write_tokens"]],
  ["provider_cost_usd", ["provider_cost_usd"]],
  ["computed_list_cost_usd", ["computed_list_cost_usd"]],
  ["legacy_cost_usd", ["cost_usd"]],
  ["cost_source", ["cost_source"]],
  ["latency_ms", ["latency_ms"]],
  ["status", ["status"]],
  ["price_sheet", ["price_sheet"]],
  ["price_simulated", ["price_simulated"]],
  ["project", ["project"]],
];

const TOKEN_COLUMNS = [
  "input_tokens",
  "output_tokens",
  "cached_input_tokens",
  "cache_write_tokens",
];

function intOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function usageOf(row: RawRow): TokenUsage {
  return {
    input_tokens: intOrNull(row.input_tokens),
    output_tokens: intOrNull(row.output_tokens),
    cached_input_tokens: intOrNull(row.cached_input_tokens),
    cache_write_tokens: intOrNull(row.cache_write_tokens),
  };
}

export function priceRow(raw: RawRow, sheet: PriceSheet | null): TamiasRow {
  const row: RawRow = { ...raw };
  for (const col of TOKEN_COLUMNS) row[col] = intOrNull(row[col]);

  const requested = strOrNull(row.model_requested) ?? "-";
  const used = strOrNull(row.model_used) ?? "-";
  const usage = usageOf(row);

  const billed = numOrNull(row.provider_cost_usd);
  const listed = numOrNull(row.computed_list_cost_usd);
  const legacy = numOrNull(row.legacy_cost_usd);
  const computed = listCost(used, usage, sheet);
  const actual = billed ?? listed ?? legacy ?? computed;
  const actual_source: TamiasRow["actual_source"] =
    billed !== null
      ? "billed"
      : listed !== null
        ? "list"
        : legacy !== null
          ? "legacy"
          : computed !== null
            ? "computed"
            : null;

  const switched = requested !== "-" && used !== "-" && requested !== used;
  const baseline = switched ? warmBaselineCost(requested, usage, sheet) : actual;
  const baseline_source: TamiasRow["baseline_source"] = switched
    ? baseline !== null
      ? "sheet"
      : null
    : actual !== null
      ? "actual"
      : null;

  const saved =
    actual === null || baseline === null ? null : switched ? baseline - actual : 0;

  return {
    id: Number(row.id ?? 0),
    ts: strOrNull(row.ts),
    session: strOrNull(row.session) ?? "(unassigned)",
    project: strOrNull(row.project),
    model_requested: requested,
    model_used: used,
    decision: strOrNull(row.decision) ?? "-",
    decision_reason: strOrNull(row.decision_reason) ?? "",
    decision_target_model: strOrNull(row.decision_target_model),
    effort_requested: strOrNull(row.effort_requested),
    effort_used: strOrNull(row.effort_used),
    router_mode: strOrNull(row.router_mode),
    route: strOrNull(row.route),
    input_tokens: usage.input_tokens,
    output_tokens: usage.output_tokens,
    cached_input_tokens: usage.cached_input_tokens,
    cache_write_tokens: usage.cache_write_tokens,
    provider_cost_usd: billed,
    computed_list_cost_usd: listed,
    legacy_cost_usd: legacy,
    cost_source: strOrNull(row.cost_source),
    price_sheet: strOrNull(row.price_sheet),
    price_simulated: row.price_simulated === 1 || row.price_simulated === true,
    latency_ms: intOrNull(row.latency_ms),
    status: strOrNull(row.status) ?? "-",
    time: clock(strOrNull(row.ts)),
    switched,
    shadow: strOrNull(row.router_mode) === "shadow",
    failed: strOrNull(row.status) !== "200",
    actual,
    actual_source,
    baseline,
    baseline_source,
    saved,
    priced: actual !== null && baseline !== null,
    tier: tierFor(used, sheet),
  };
}

export function summarizeRows(rows: readonly TamiasRow[]): TamiasSummary {
  const priced = rows.filter((row) => row.priced);
  const actual_total = priced.reduce((sum, row) => sum + (row.actual ?? 0), 0);
  const baseline_total = priced.reduce((sum, row) => sum + (row.baseline ?? 0), 0);
  const saved_total = baseline_total - actual_total;

  const latencies = rows
    .map((row) => row.latency_ms)
    .filter((value): value is number => typeof value === "number");

  let n_free = 0;
  let n_paid = 0;
  let n_unknown_tier = 0;
  for (const row of rows) {
    if (row.tier === "free") n_free += 1;
    else if (row.tier === "paid") n_paid += 1;
    else n_unknown_tier += 1;
  }

  return {
    n_requests: rows.length,
    n_priced: priced.length,
    n_unpriced: rows.length - priced.length,
    n_switched: rows.filter((row) => row.switched).length,
    n_ok: rows.filter((row) => !row.failed).length,
    n_failed: rows.filter((row) => row.failed).length,
    n_free,
    n_paid,
    n_unknown_tier,
    actual_total,
    baseline_total,
    saved_total,
    saving_pct: baseline_total > 0 ? saved_total / baseline_total : null,
    estimated: priced.some(
      (row) =>
        row.actual_source !== "billed" ||
        (row.switched && row.baseline_source === "sheet"),
    ),
    latency_avg:
      latencies.length > 0
        ? latencies.reduce((sum, value) => sum + value, 0) / latencies.length
        : null,
  };
}

function emptySnapshot(): TamiasSnapshot {
  return {
    rows: [],
    summary: summarizeRows([]),
    total_rows: 0,
    passthrough_rows: 0,
    chat_rows: 0,
    truncated: false,
  };
}

function countOf(db: Database, sql: string): number {
  const row = db.query(sql).get() as { n?: number } | null;
  return typeof row?.n === "number" && Number.isFinite(row.n) ? row.n : 0;
}

/**
 * Read the newest chat rows from an open database.
 *
 * Passthrough rows are filtered in SQL so they neither fill the window nor
 * inflate the summary; their count is still reported separately.
 */
export function readSnapshot(
  db: Database,
  sheet: PriceSheet | null,
  limit = DEFAULT_ROW_LIMIT,
): TamiasSnapshot {
  const present = new Set(
    (db.query('PRAGMA table_info("requests")').all() as Array<{ name: string }>).map(
      (column) => column.name,
    ),
  );
  if (present.size === 0) return emptySnapshot();

  const aliases = new Map<string, string>();
  for (const [name, candidates] of COLUMNS) {
    for (const candidate of candidates) {
      if (present.has(candidate)) {
        aliases.set(name, candidate);
        break;
      }
    }
  }
  if (!aliases.has("model_used") || !aliases.has("ts")) return emptySnapshot();

  const projections = [...aliases.entries()].map(
    ([name, column]) => `"${column}" AS "${name}"`,
  );
  const orderColumn = aliases.has("id") ? '"id"' : "rowid";
  const routeColumn = aliases.get("route");
  const chatFilter = routeColumn
    ? `WHERE "${routeColumn}" IS NULL OR "${routeColumn}" <> 'passthrough'`
    : "";

  const total_rows = countOf(db, 'SELECT COUNT(*) AS n FROM "requests"');
  const passthrough_rows = routeColumn
    ? countOf(
        db,
        `SELECT COUNT(*) AS n FROM "requests" WHERE "${routeColumn}" = 'passthrough'`,
      )
    : 0;
  const chat_rows = Math.max(0, total_rows - passthrough_rows);

  const rowsLimit = Math.max(1, Math.trunc(limit));
  const sql = `SELECT ${projections.join(", ")} FROM "requests" ${chatFilter} ORDER BY ${orderColumn} DESC LIMIT ?`;
  const raw = db.query(sql).all(rowsLimit) as RawRow[];
  const rows = raw.map((row) => priceRow(row, sheet));

  return {
    rows,
    summary: summarizeRows(rows),
    total_rows,
    passthrough_rows,
    chat_rows,
    truncated: chat_rows > rows.length,
  };
}

/** Open the log read-only, or return null when tamias has not created it yet. */
export function openReadonly(dbPath: string): Database | null {
  if (!existsSync(dbPath)) return null;
  const db = new Database(dbPath, { readonly: true });
  try {
    db.exec("PRAGMA busy_timeout = 500");
  } catch {
    // A pragma failure is not fatal; the read can still succeed.
  }
  return db;
}

export type SourceState = "loading" | "ok" | "missing" | "error";

export interface UseTamiasData {
  rows: () => TamiasRow[];
  summary: () => TamiasSummary;
  sheet: () => PriceSheet | null;
  sheetState: () => SourceState;
  sheetError: () => string | null;
  dbState: () => SourceState;
  error: () => string | null;
  loading: () => boolean;
  lastUpdated: () => Date | null;
  snapshot: () => TamiasSnapshot;
  refresh: () => void;
}

export function useTamiasData(
  dbPath: string,
  pricesPath: string,
  options: { pollMs?: number; limit?: number } = {},
): UseTamiasData {
  const pollMs = options.pollMs ?? 2000;
  const limit = options.limit ?? DEFAULT_ROW_LIMIT;

  const [rows, setRows] = createSignal<TamiasRow[]>([]);
  const [summary, setSummary] = createSignal<TamiasSummary>(summarizeRows([]));
  const [sheet, setSheet] = createSignal<PriceSheet | null>(null);
  const [sheetState, setSheetState] = createSignal<SourceState>("loading");
  const [sheetError, setSheetError] = createSignal<string | null>(null);
  const [dbState, setDbState] = createSignal<SourceState>("loading");
  const [error, setError] = createSignal<string | null>(null);
  const [loading, setLoading] = createSignal(true);
  const [lastUpdated, setLastUpdated] = createSignal<Date | null>(null);
  const [snapshot, setSnapshot] = createSignal<TamiasSnapshot>(emptySnapshot());

  let db: Database | null = null;
  let sheetStamp = "";
  let busy = false;
  let disposed = false;

  function loadSheet(): PriceSheet | null {
    const localPath = localSheetPath(pricesPath);
    let stamp: string;
    try {
      const stat = statSync(pricesPath);
      const local = existsSync(localPath) ? statSync(localPath) : null;
      stamp = `${stat.mtimeMs}:${stat.size}|${
        local ? `${local.mtimeMs}:${local.size}` : "-"
      }`;
    } catch {
      sheetStamp = "";
      setSheet(null);
      setSheetError(null);
      setSheetState("missing");
      return null;
    }

    if (stamp === sheetStamp) {
      setSheetState(sheet() ? "ok" : "missing");
      return sheet();
    }

    try {
      const parsed = parsePriceSheet(readFileSync(pricesPath, "utf-8"));
      // The hand-written overlay prices models the generated sheet cannot:
      // a free development tier, a local model, a provider the catalogue omits.
      const merged = existsSync(localPath)
        ? mergePriceSheets(parsed, parsePriceSheet(readFileSync(localPath, "utf-8")))
        : parsed;
      sheetStamp = stamp;
      setSheet(merged);
      setSheetError(null);
      setSheetState("ok");
      return merged;
    } catch (cause) {
      // Keep the previous good sheet: a partial rewrite during one poll must
      // not turn every baseline in the panel into "unknown".
      setSheetError(cause instanceof Error ? cause.message : String(cause));
      setSheetState("error");
      return sheet();
    }
  }

  function openDb(): Database | null {
    if (db) return db;
    try {
      db = openReadonly(dbPath);
      return db;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setDbState("error");
      return null;
    }
  }

  function refresh() {
    if (busy || disposed) return;
    busy = true;
    try {
      const currentSheet = loadSheet();
      const connection = openDb();
      if (!connection) {
        setRows([]);
        setSummary(summarizeRows([]));
        setSnapshot(emptySnapshot());
        if (dbState() !== "error") setDbState("missing");
        setLastUpdated(new Date());
        return;
      }

      try {
        const next = readSnapshot(connection, currentSheet, limit);
        setRows(next.rows);
        setSummary(next.summary);
        setSnapshot(next);
        setError(null);
        setDbState("ok");
        setLastUpdated(new Date());
      } catch (cause) {
        // A transient lock or a replaced file: drop the connection so the next
        // poll reopens, and keep the last good rows on screen.
        try {
          connection.close();
        } catch {
          // already closed
        }
        db = null;
        setError(cause instanceof Error ? cause.message : String(cause));
        setDbState("error");
      }
    } finally {
      busy = false;
      setLoading(false);
    }
  }

  void refresh();

  let timer: ReturnType<typeof setInterval> | null = null;
  onMount(() => {
    timer = setInterval(refresh, pollMs);
  });
  onCleanup(() => {
    disposed = true;
    if (timer) clearInterval(timer);
    timer = null;
    if (db) {
      try {
        db.close();
      } catch {
        // already closed
      }
      db = null;
    }
  });

  return {
    rows,
    summary,
    sheet,
    sheetState,
    sheetError,
    dbState,
    error,
    loading,
    lastUpdated,
    snapshot,
    refresh,
  };
}
