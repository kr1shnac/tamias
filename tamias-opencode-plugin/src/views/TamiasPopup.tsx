/**
 * The `/tamias` pop-up.
 *
 * This is deliberately not a dashboard: a modal is for the question a user
 * asks mid-conversation -- *what did that just cost, which model actually ran,
 * and is the pool healthy?* -- not for browsing five hundred requests. The
 * request table and session sidebar were removed for 1.0.1; `tamias report`
 * (and `/tamias-report`) remain the place to read the full log. This surface
 * keeps the one-glance answer to six or seven lines.
 *
 * It reads two independent sources:
 *
 *   - the request log (SQLite, read-only) for what actually ran and what it
 *     cost;
 *   - `GET /limits` for provider health, the eligible pool and cooldowns.
 *
 * Every claim about a model that was *not* run is labelled as an estimate.
 * When tamias is not running the pop-up still works on the log alone and says
 * so; when the proxy is up but rejects the token, it names that instead of
 * pretending the proxy is down.
 */

import { Show, createMemo } from "solid-js";
import { homedir } from "node:os";
import path from "node:path";
import { KpiStrip, Notice, SavingsBar } from "../components/Summary";
import { useLimits } from "../hooks/useLimits";
import {
  summarizeRows,
  useTamiasData,
  windowMode,
  type TamiasRow,
} from "../hooks/useTamiasData";
import { useRendererSize, type RendererLike } from "../hooks/useRendererSize";
import { ThemeProvider, usePalette } from "../ui/theme";
import { clip, count, duration, shortModel } from "../utils/format";
import type { ThemeLike } from "../utils/palette";

const DATA_DIR = process.env.TAMIAS_HOME ?? path.join(homedir(), ".tamias");
export const DB_PATH = process.env.TAMIAS_DB ?? path.join(DATA_DIR, "requests.db");
export const PRICES_PATH = process.env.TAMIAS_PRICES ?? path.join(DATA_DIR, "prices.toml");

export interface TamiasPopupProps {
  renderer: RendererLike | undefined;
  theme: ThemeLike;
  mode: "dark" | "light";
}

/** The public entry: installs the host palette for every child component. */
export function TamiasPopup(props: TamiasPopupProps) {
  return (
    <ThemeProvider theme={props.theme} mode={props.mode}>
      <PopupBody renderer={props.renderer} />
    </ThemeProvider>
  );
}

function PopupBody(props: Pick<TamiasPopupProps, "renderer">) {
  const palette = usePalette();
  const size = useRendererSize(props.renderer);
  const data = useTamiasData(DB_PATH, PRICES_PATH);
  const limits = useLimits();

  // The modal has no sidebar to scope by session, so it summarises the newest
  // window of the log.  The "latest route" line below keeps the current turn
  // visible without turning the pop-up into a table.
  const summary = createMemo(() => summarizeRows(data.rows()));
  const latest = createMemo<TamiasRow | undefined>(() => data.rows()[0]);
  const width = createMemo(() => Math.max(36, Math.min(76, size.width() - 12)));

  const headerRight = () => (limits.live() ? "● live" : "○ log only");
  // Shadow mode rewrites nothing, so a zero savings bar is the honest answer,
  // not a failure.  Naming the mode beside "all recent" explains the zero
  // without pretending the counterfactual was earned.
  const modeLabel = createMemo(() =>
    windowMode(data.rows()) === "shadow" ? " · shadow" : "",
  );

  const latestLine = createMemo(() => {
    const row = latest();
    if (!row) return "latest  no metered request yet";
    const tier = row.tier === "free" ? "● free" : row.tier === "paid" ? "$ paid" : "? price";
    return clip(
      `latest  ${shortModel(row.model_requested, 22)} → ${shortModel(
        row.model_used,
        22,
      )}  ${tier}  ${row.decision}`,
      width(),
    );
  });

  const latestReason = createMemo(() => {
    const row = latest();
    if (!row || !row.decision_reason) return "";
    return clip(`        ${row.decision_reason}`, width());
  });

  const poolLine = createMemo(() => {
    if (!limits.live() || !limits.snapshot()) {
      const detail = limits.error();
      // Put the user-facing claim before the raw transport error: at a narrow
      // width the clip must never cut off "showing the log alone".
      return clip(
        detail
          ? `live  unavailable · showing the log alone · ${detail}`
          : "live  unavailable · showing the log alone",
        width(),
      );
    }
    const snapshot = limits.snapshot()!;
    const parts = [
      `pool  ${count(snapshot.pool.candidates)}/${count(snapshot.pool.considered)} capable`,
    ];
    const refused = Object.entries(snapshot.pool.gates)
      .filter(([, value]) => value > 0)
      .map(([gate, value]) => `${count(value)} ${gate}`);
    if (refused.length > 0) parts.push(`refused ${refused.join(" · ")}`);
    if (snapshot.pool.truncated) parts.push("capped");
    const noKey = snapshot.providers.filter((provider) => !provider.configured);
    if (noKey.length > 0) {
      parts.push(`no key: ${noKey.map((provider) => provider.name).join(", ")}`);
    }
    return clip(parts.join(" · "), width());
  });

  const coolingLine = createMemo(() => {
    const snapshot = limits.snapshot();
    if (!limits.live() || !snapshot || snapshot.cooling_down.length === 0) return "";
    const first = snapshot.cooling_down[0];
    if (!first) return "";
    const more = snapshot.cooling_down.length > 1 ? ` +${snapshot.cooling_down.length - 1}` : "";
    return clip(
      `cooling  ${shortModel(first.model, 22)} ${first.provider} ${first.status} · back in ${duration(
        first.remaining_seconds,
      )}${more}`,
      width(),
    );
  });

  const notices = createMemo(() => {
    const lines: string[] = [];
    if (data.dbState() === "missing") {
      lines.push(`no request log at ${DB_PATH} · start tamias serve, then route a request`);
    } else if (data.dbState() === "error" && data.error()) {
      lines.push(`log error: ${data.error()}`);
    }
    if (data.sheetState() === "missing") {
      lines.push("no price sheet · only costs already stored on a row can be shown");
    } else if (data.sheetState() === "error" && data.sheetError()) {
      lines.push(`price sheet error: ${data.sheetError()}`);
    }
    if (data.dbState() === "ok" && data.rows().length === 0) {
      lines.push("request log is empty · route a request through tamias");
    }
    if (data.snapshot().truncated) {
      lines.push(
        `savings over the newest ${count(data.snapshot().rows.length)} of ${count(
          data.snapshot().chat_rows,
        )} requests`,
      );
    }
    return lines.slice(0, 2);
  });

  return (
    <box
      flexDirection="column"
      width="100%"
      backgroundColor={palette().panel}
      paddingLeft={1}
      paddingRight={1}
      gap={0}
    >
      <box flexDirection="row" width="100%">
        <text wrapMode="none" fg={palette().accent}>tamias</text>
        <text wrapMode="none" fg={palette().muted}> · all recent{modeLabel()}</text>
        <box flexGrow={1} minWidth={0} />
        <text wrapMode="none" fg={limits.live() ? palette().good : palette().faint}>
          {headerRight()}
        </text>
      </box>

      <SavingsBar summary={summary()} width={width()} />
      <KpiStrip summary={summary()} width={width()} />

      <text wrapMode="none" fg={palette().text}>{latestLine()}</text>
      <Show when={latestReason()}>
        <text wrapMode="none" fg={palette().faint}>{latestReason()}</text>
      </Show>

      <text wrapMode="none" fg={palette().muted}>{poolLine()}</text>
      <Show when={(limits.snapshot()?.cooling_down.length ?? 0) > 0}>
        <text wrapMode="none" fg={palette().warn}>{coolingLine()}</text>
      </Show>

      {notices().map((line) => (
        <Notice tone="muted" text={line} width={width()} />
      ))}

      <text wrapMode="none" fg={palette().faint}>
        esc close · log 2s · live 5s
      </text>
    </box>
  );
}
