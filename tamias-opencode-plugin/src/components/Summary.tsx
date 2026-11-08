/**
 * The three summary lines above the request table.
 *
 * These replace the old KPI cards and cumulative sparkline. A card per number
 * spent four rows to say what one line can say, and two cumulative series were
 * hard to read without a legend. What is left is the question the dashboard
 * exists to answer: how much ran, what it cost, how much was saved, and how
 * much of it was free.
 */

import { createMemo } from "solid-js";
import { bar, clip, count, duration, money, percent, shortModel } from "../utils/format";
import { usePalette } from "../ui/theme";
import type { TamiasRow, TamiasSummary } from "../hooks/useTamiasData";

export interface KpiStripProps {
  summary: TamiasSummary;
  width: number;
}

export function KpiStrip(props: KpiStripProps) {
  const palette = usePalette();

  const rated = () => props.summary.n_free + props.summary.n_paid;
  const freeShare = () => {
    const known = rated();
    return known > 0 ? props.summary.n_free / known : null;
  };

  const spent = () => {
    if (props.summary.n_requests === 0) return "no requests yet";
    if (props.summary.n_priced === 0) return "cost unknown";
    // The tilde belongs to the amount that is estimated, not to every number
    // in the window.  A billed actual spend is not "~" just because a baseline
    // elsewhere came from the sheet.
    const prefix = props.summary.actual_estimated ? "~" : "";
    return `${prefix}${money(props.summary.actual_total)} spent`;
  };

  // Two lines, not one clipped line: a narrow terminal used to cut off
  // "failed" and "unpriced" first, which are the two facts a user must see.
  const primary = () => {
    const summary = props.summary;
    const parts = [count(summary.n_requests) + " req", spent()];
    if (summary.n_switched > 0) parts.push(`${count(summary.n_switched)} switched`);
    if (summary.n_failed > 0) parts.push(`${count(summary.n_failed)} failed`);
    return clip(parts.join(" · "), props.width);
  };

  const secondary = () => {
    const summary = props.summary;
    const parts: string[] = [];
    if (freeShare() !== null) {
      parts.push(
        `● ${percent(freeShare())} free (${count(summary.n_free)}/${count(rated())} rated)`,
      );
    }
    // The cache share is context for the savings bar, not a saving: a high
    // share is the case where a switch re-ingests the prompt and can cost more
    // than it keeps, which is why `report` prices the stay arm cache-warm.
    if (summary.cache_share !== null) {
      parts.push(`cache ${percent(summary.cache_share)} warm`);
    }
    if (summary.n_unpriced > 0) parts.push(`${count(summary.n_unpriced)} unpriced`);
    return clip(parts.join(" · "), props.width);
  };

  return (
    <box flexDirection="column" flexShrink={0}>
      <text wrapMode="none" fg={palette().muted}>{primary()}</text>
      <text wrapMode="none" fg={palette().muted}>{secondary()}</text>
    </box>
  );
}

export interface SavingsBarProps {
  summary: TamiasSummary;
  width: number;
}

export function SavingsBar(props: SavingsBarProps) {
  const palette = usePalette();
  const barWidth = () => Math.max(8, Math.min(22, Math.floor(props.width * 0.22)));
  const ratio = () => props.summary.saving_pct;
  const tone = () => {
    const value = ratio();
    if (value === null) return palette().warn;
    return value >= 0 ? palette().good : palette().bad;
  };

  const caption = () => {
    const summary = props.summary;
    if (summary.n_requests === 0) return "no requests in this scope yet";
    if (summary.n_priced === 0) return "savings unknown · no request carried a usable baseline";
    if (summary.baseline_total <= 0) return "requested models were free · nothing to save";
    if (summary.saved_total === 0) return "no list-price gap in this window";
    const value = ratio();
    if (value === null) return "savings unknown";
    // The baseline is what the requested model would have cost at list price.
    // It is a counterfactual, never a provider invoice, so this is "est." even
    // when the actual side is billed.  An invoice-proven number belongs to
    // `tamias reconcile --invoice`, not to this arithmetic.
    // One decimal so 99.75% does not round to a misleading "100%".
    if (value < 0) {
      return `est. ${percent(Math.abs(value), 1)} above list · ${money(-summary.saved_total)} more than baseline`;
    }
    return `est. ${percent(value, 1)} below list · ${money(summary.saved_total)} of ${money(summary.baseline_total)} baseline`;
  };

  const empty = () => props.summary.n_priced === 0 || props.summary.n_requests === 0;
  const width = () => Math.max(4, props.width - barWidth() - 1);

  return (
    <box flexDirection="row" gap={1} flexShrink={0}>
      <text wrapMode="none" fg={empty() ? palette().faint : tone()}>
        {bar(empty() ? null : ratio(), barWidth())}
      </text>
      <text wrapMode="none" fg={empty() ? palette().faint : palette().muted}>{clip(caption(), width())}</text>
    </box>
  );
}

export interface TopModelsProps {
  rows: readonly TamiasRow[];
  width: number;
}

export function TopModels(props: TopModelsProps) {
  const palette = usePalette();

  const top = createMemo(() => {
    const counts = new Map<string, number>();
    for (const row of props.rows) {
      if (row.model_used === "-") continue;
      counts.set(row.model_used, (counts.get(row.model_used) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 3);
  });

  const line = () => {
    if (top().length === 0) return "";
    const names = top()
      .map(([model, n]) => `${shortModel(model, 20)} ×${count(n)}`)
      .join(" · ");
    return clip(`top  ${names}`, props.width);
  };

  return line() ? <text wrapMode="none" fg={palette().muted}>{line()}</text> : null;
}

/** A small list of the newest cooling models, one line. */
export interface CoolingLineProps {
  models: ReadonlyArray<{ model: string; provider: string; remaining_seconds: number; status: string }>;
  total: number;
  width: number;
}

export function CoolingLine(props: CoolingLineProps) {
  const palette = usePalette();

  const line = () => {
    if (props.total === 0) return "";
    const first = props.models[0];
    if (!first) return `${props.total} model${props.total === 1 ? "" : "s"} cooling down`;
    const more = props.total > 1 ? ` +${props.total - 1}` : "";
    return clip(
      `cooling  ${shortModel(first.model, 24)} ${first.provider} ${first.status} · back in ${duration(first.remaining_seconds)}${more}`,
      props.width,
    );
  };

  return line() ? <text wrapMode="none" fg={palette().warn}>{line()}</text> : null;
}

/** A compact, non-card notice used for empty/blocked states. */
export interface NoticeProps {
  tone: "warn" | "bad" | "muted";
  text: string;
  width: number;
}

export function Notice(props: NoticeProps) {
  const palette = usePalette();
  const color = () =>
    props.tone === "warn" ? palette().warn : props.tone === "bad" ? palette().bad : palette().faint;
  return <text wrapMode="none" fg={color()}>{clip(props.text, props.width)}</text>;
}
