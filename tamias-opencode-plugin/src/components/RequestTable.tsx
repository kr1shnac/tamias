/**
 * Request log table.
 *
 * Columns are chosen by importance, not by a right-edge pop: the used model,
 * tier, cost and status never disappear, and the first things dropped on a
 * narrow panel are the clock, the latency and the token counts -- not the
 * model that actually ran or the model that was asked for. Every cue is a
 * glyph or a word, so a monochrome terminal still reads correctly.
 */

import { For, createMemo } from "solid-js";
import {
  cell,
  clip,
  compactCount,
  latency,
  moneyCompact,
  shortModel,
} from "../utils/format";
import { usePalette, type Palette } from "../ui/theme";
import type { TamiasRow } from "../hooks/useTamiasData";
import type { Tier } from "../utils/priceSheet";

export interface TableRow extends TamiasRow {
  /** Provider that serves `model_used`, or "?" when the pool cannot say. */
  provider: string;
  /** Authoritative tier from `/limits`, falling back to the price sheet. */
  tier: Tier;
}

type Tone = "text" | "muted" | "faint" | "accent" | "good" | "warn" | "bad";

interface ColumnDef {
  key: string;
  header: string;
  width: number;
  align?: "left" | "right";
  tone: (row: TableRow) => Tone;
  render: (row: TableRow) => string;
  /** Smaller drops first. 0 is never dropped. */
  drop: number;
}

function tierGlyph(tier: Tier): string {
  return tier === "free" ? "●" : tier === "paid" ? "$" : "?";
}

function tierTone(tier: Tier): Tone {
  return tier === "free" ? "good" : tier === "paid" ? "warn" : "faint";
}

const COLUMNS: ColumnDef[] = [
  {
    key: "tier",
    header: "tier",
    width: 4,
    tone: (row) => tierTone(row.tier),
    render: (row) => tierGlyph(row.tier),
    drop: 0,
  },
  {
    key: "model",
    header: "model",
    width: 17,
    tone: () => "text",
    render: (row) => shortModel(row.model_used, 17),
    drop: 0,
  },
  {
    key: "asked",
    header: "asked",
    width: 17,
    tone: () => "muted",
    render: (row) => (row.switched ? shortModel(row.model_requested, 17) : "="),
    drop: 4,
  },
  {
    key: "provider",
    header: "provider",
    width: 10,
    tone: (row) => (row.provider === "?" ? "faint" : "muted"),
    render: (row) => clip(row.provider, 10),
    drop: 6,
  },
  {
    key: "cost",
    header: "cost",
    width: 9,
    align: "right",
    tone: (row) =>
      row.actual === null ? "faint" : row.actual_source === "billed" ? "text" : "muted",
    render: (row) => (row.actual === null ? "-" : moneyCompact(row.actual)),
    drop: 7,
  },
  {
    key: "saved",
    header: "saved",
    width: 9,
    align: "right",
    tone: (row) =>
      row.saved === null
        ? "faint"
        : row.saved > 0
          ? "good"
          : row.saved < 0
            ? "bad"
            : "faint",
    render: (row) => {
      if (row.saved === null) return "-";
      if (row.saved === 0) return "$0";
      // A switch is compared against the current price sheet for a model that
      // never ran, so its row-level saving is an estimate. Mark it, the same
      // way the summary does, instead of lending it measured precision.
      const estimate = row.baseline_source === "sheet" ? "~" : "";
      return row.saved > 0
        ? `${estimate}+${moneyCompact(row.saved)}`
        : `${estimate}${moneyCompact(row.saved)}`;
    },
    drop: 5,
  },
  {
    key: "tokens",
    header: "in/out",
    width: 13,
    align: "right",
    tone: () => "muted",
    render: (row) => {
      const input = compactCount(row.input_tokens);
      const output = compactCount(row.output_tokens);
      return input === "-" && output === "-" ? "-" : `${input}/${output}`;
    },
    drop: 3,
  },
  {
    key: "time",
    header: "time",
    width: 8,
    tone: () => "muted",
    render: (row) => row.time,
    drop: 1,
  },
  {
    key: "ms",
    header: "ms",
    width: 5,
    align: "right",
    tone: () => "muted",
    render: (row) => latency(row.latency_ms),
    drop: 2,
  },
  {
    key: "status",
    header: "st",
    width: 3,
    tone: (row) => (row.failed ? "bad" : "good"),
    render: (row) => row.status,
    drop: 0,
  },
];

function widthOf(columns: readonly ColumnDef[]): number {
  return columns.reduce((sum, column) => sum + column.width, 0) + Math.max(0, columns.length - 1);
}

/**
 * Fit the table to `width` by dropping the least important column first.
 * `drop` is a priority, not a width: 1 goes before 2, and 0 never goes.
 */
export function columnsFor(width: number): ColumnDef[] {
  const selected = new Set(COLUMNS.map((column) => column.key));
  const current = () => COLUMNS.filter((column) => selected.has(column.key));
  const total = () => widthOf(current());

  const dropOrder = COLUMNS.filter((column) => column.drop > 0).sort(
    (a, b) => a.drop - b.drop,
  );
  for (const column of dropOrder) {
    if (total() <= width) break;
    selected.delete(column.key);
  }

  return COLUMNS.filter((column) => selected.has(column.key));
}

function colorOf(palette: Palette, tone: Tone) {
  return palette[tone];
}

export interface RequestTableProps {
  rows: readonly TableRow[];
  width: number;
  /** Maximum data rows; the caller leaves room for the header and footer. */
  height: number;
}

export function RequestTable(props: RequestTableProps) {
  const palette = usePalette();
  const columns = createMemo(() => columnsFor(Math.max(20, props.width - 1)));
  const visible = createMemo(() => props.rows.slice(0, Math.max(1, props.height)));

  return (
    <box flexDirection="column" flexGrow={1} overflow="hidden">
      <box
        flexDirection="row"
        gap={1}
        flexShrink={0}
        backgroundColor={palette().header}
        paddingLeft={0}
        paddingRight={0}
      >
        <For each={columns()}>
          {(column) => (
            <text wrapMode="none" fg={palette().muted}>
              {cell(column.header, column.width, column.align)}
            </text>
          )}
        </For>
      </box>
      <box flexDirection="column" flexGrow={1} overflow="hidden">
        <For each={visible()}>
          {(row) => (
            <box flexDirection="row" gap={1} flexShrink={0}>
              <For each={columns()}>
                {(column) => (
                  <text wrapMode="none" fg={colorOf(palette(), column.tone(row))}>
                    {cell(column.render(row), column.width, column.align)}
                  </text>
                )}
              </For>
            </box>
          )}
        </For>
      </box>
      {props.rows.length === 0 ? (
        <text wrapMode="none" fg={palette().faint}>no requests in this scope yet</text>
      ) : null}
    </box>
  );
}
