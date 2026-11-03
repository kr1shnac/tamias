/**
 * Terminal-safe formatting helpers for the tamias TUI dashboard.
 *
 * Everything returns a plain string sized for a fixed-width cell, so the view
 * layer never pads or clips inline. Cost helpers keep the difference between
 * "unknown" (n/a) and "zero" ($0) visible: a missing usage block must never
 * read as a free request.
 */

/** Full precision where it matters, never more than a cell can show. */
export function money(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  if (value === 0) return "$0.00";
  const sign = value < 0 ? "-$" : "$";
  const abs = Math.abs(value);
  if (abs < 0.0001) return "<$0.0001";
  if (abs < 0.01) return sign + abs.toPrecision(2);
  if (abs < 1) return sign + abs.toFixed(4);
  return sign + abs.toFixed(2);
}

/** A table cell: short enough for 9 columns, still exact for tiny costs. */
export function moneyCompact(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  if (value === 0) return "$0";
  const sign = value < 0 ? "-$" : "$";
  const abs = Math.abs(value);
  if (abs >= 1000) return `${sign}${Math.round(abs / 1000)}k`;
  if (abs >= 100) return `${sign}${abs.toFixed(0)}`;
  if (abs >= 1) return `${sign}${abs.toFixed(2)}`;
  if (abs >= 0.0001) return `${sign}${abs.toPrecision(2)}`;
  // Keep the sign: a tiny negative saving is not the same as a tiny positive
  // one, and a table cell must not turn a loss into a gain.
  return sign === "-$" ? "-<$0.0001" : "<$0.0001";
}

export function count(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("en-US")
    : "n/a";
}

/** Compact token counts: 12.3k, 1.2M. Null stays unknown. */
export function compactCount(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  if (value < 0) return "-";
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 100_000) return `${Math.round(value / 1_000)}k`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/** A ratio (0.77) as a percentage. Defaults to whole percent for the panel. */
export function percent(value: number | null | undefined, digits = 0): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  return `${(value * 100).toFixed(digits)}%`;
}

export function latency(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 1000)}s`;
}

/** Local clock time. Precomputed per row, so the table does not rebuild dates. */
export function clock(ts: string | null | undefined): string {
  if (!ts) return "--:--:--";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** `10:10` for today, `Oct 31` for older rows. */
export function when(ts: string | null | undefined): string {
  if (!ts) return "--:--";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "--:--";
  const today = new Date().toDateString() === date.toDateString();
  return today
    ? date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function duration(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "now";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** A labelled-progress-bar body; the caption carries the meaning, not the bar. */
export function bar(ratio: number | null | undefined, width: number): string {
  const cells = Math.max(1, Math.floor(width));
  if (typeof ratio !== "number" || !Number.isFinite(ratio)) return "░".repeat(cells);
  const filled = Math.max(0, Math.min(cells, Math.round(ratio * cells)));
  return "█".repeat(filled) + "░".repeat(cells - filled);
}

export function isMissing(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}

/** `-`, empty, or whitespace only: tamias writes `-` for unmetered fields. */
export function isBlank(value: unknown): boolean {
  return isMissing(value) || (typeof value === "string" && value.trim() === "");
}

export function truncateSessionId(id: string, length = 10): string {
  return id.length > length ? id.slice(0, length - 1) + "…" : id;
}

/** Fit text to `width` cells, truncating with an ellipsis when it overflows. */
export function clip(value: string, width: number): string {
  if (width <= 0) return "";
  if (value.length <= width) return value;
  if (width === 1) return "…";
  return value.slice(0, width - 1) + "…";
}

/** Left-align text in a `width` cell. */
export function padEnd(value: string, width: number): string {
  return value.length >= width ? value : value + " ".repeat(width - value.length);
}

/** Right-align text in a `width` cell, for numeric columns. */
export function padStart(value: string, width: number): string {
  return value.length >= width ? value : " ".repeat(width - value.length) + value;
}

/** Pad a cell to `width` and clip it, optionally right-aligned. */
export function cell(value: string, width: number, align: "left" | "right" = "left"): string {
  return align === "right" ? padStart(clip(value, width), width) : padEnd(clip(value, width), width);
}

/** Last path segment of a model id, clipped to fit a table column. */
export function shortModel(model: string | null | undefined, width = 18): string {
  if (isBlank(model)) return "-";
  const text = String(model);
  const segments = text.split("/");
  const tail = segments[segments.length - 1] ?? text;
  return clip(tail, width);
}

export function plural(n: number, one: string, many = one + "s"): string {
  return `${count(n)} ${n === 1 ? one : many}`;
}
