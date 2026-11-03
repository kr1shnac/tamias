/**
 * Theme-aware palette.
 *
 * OpenCode resolves a theme for the active mode (dark or light) and exposes it
 * as `context.theme`. Every colour the dashboard paints is taken from that
 * object, so the panel follows the user's theme instead of shipping a second,
 * hard-coded one. The hex values below are only a fallback for a host that
 * does not publish a theme; they are never preferred when a token exists.
 */

import type { RGBA } from "@opentui/core";

export type ThemeColor = RGBA | string;

export interface ThemeLike {
  readonly background?: {
    readonly base?: ThemeColor;
    readonly raised?: {
      readonly base?: ThemeColor;
      readonly high?: ThemeColor;
      readonly max?: ThemeColor;
    };
  };
  readonly border?: { readonly base?: ThemeColor };
  readonly text?: {
    readonly base?: ThemeColor;
    readonly muted?: ThemeColor;
    readonly faint?: ThemeColor;
    readonly feedback?: {
      readonly success?: { readonly base?: ThemeColor };
      readonly warning?: { readonly base?: ThemeColor };
      readonly error?: { readonly base?: ThemeColor };
      readonly info?: { readonly base?: ThemeColor };
    };
  };
  readonly hue?: {
    readonly accent?: Readonly<Record<number, ThemeColor>>;
    readonly interactive?: Readonly<Record<number, ThemeColor>>;
    readonly neutral?: Readonly<Record<number, ThemeColor>>;
  };
}

export interface Palette {
  /** Page background. */
  bg: ThemeColor;
  /** Panel and card fill. */
  panel: ThemeColor;
  /** A raised surface, for headers and highlighted rows. */
  panelHigh: ThemeColor;
  /** Table header fill. */
  header: ThemeColor;
  /** Selected row fill. */
  selected: ThemeColor;
  border: ThemeColor;
  borderStrong: ThemeColor;
  text: ThemeColor;
  muted: ThemeColor;
  faint: ThemeColor;
  accent: ThemeColor;
  interactive: ThemeColor;
  good: ThemeColor;
  warn: ThemeColor;
  bad: ThemeColor;
  info: ThemeColor;
  /** Tier colours: free is good, paid is a warning, unknown is faint. */
  free: ThemeColor;
  paid: ThemeColor;
  unknown: ThemeColor;
}

const DARK = {
  bg: "#0b1220",
  panel: "#0f172a",
  panelHigh: "#111827",
  header: "#111827",
  selected: "#1e293b",
  border: "#334155",
  borderStrong: "#475569",
  text: "#e2e8f0",
  muted: "#94a3b8",
  faint: "#64748b",
  accent: "#38bdf8",
  interactive: "#38bdf8",
  good: "#4ade80",
  warn: "#fbbf24",
  bad: "#f87171",
  info: "#38bdf8",
} as const;

const LIGHT = {
  bg: "#f8fafc",
  panel: "#ffffff",
  panelHigh: "#f1f5f9",
  header: "#f1f5f9",
  selected: "#e2e8f0",
  border: "#cbd5e1",
  borderStrong: "#94a3b8",
  text: "#0f172a",
  muted: "#475569",
  faint: "#94a3b8",
  accent: "#0284c7",
  interactive: "#2563eb",
  good: "#15803d",
  warn: "#b45309",
  bad: "#b91c1c",
  info: "#0369a1",
} as const;

export function paletteFor(
  theme: ThemeLike | undefined,
  mode: "dark" | "light" = "dark",
): Palette {
  const fallback = mode === "light" ? LIGHT : DARK;
  const raised = theme?.background?.raised;
  const feedback = theme?.text?.feedback;

  const good = feedback?.success?.base ?? fallback.good;
  const warn = feedback?.warning?.base ?? fallback.warn;
  const bad = feedback?.error?.base ?? fallback.bad;

  return {
    bg: theme?.background?.base ?? fallback.bg,
    panel: raised?.base ?? fallback.panel,
    panelHigh: raised?.high ?? fallback.panelHigh,
    header: raised?.high ?? fallback.header,
    selected: raised?.max ?? fallback.selected,
    border: theme?.border?.base ?? fallback.border,
    borderStrong: theme?.hue?.neutral?.[600] ?? fallback.borderStrong,
    text: theme?.text?.base ?? fallback.text,
    muted: theme?.text?.muted ?? fallback.muted,
    faint: theme?.text?.faint ?? fallback.faint,
    accent: theme?.hue?.accent?.[500] ?? fallback.accent,
    interactive: theme?.hue?.interactive?.[500] ?? fallback.interactive,
    good,
    warn,
    bad,
    info: feedback?.info?.base ?? fallback.info,
    free: good,
    paid: warn,
    unknown: theme?.text?.faint ?? fallback.faint,
  };
}
