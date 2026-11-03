/**
 * The tamias session panel.
 *
 * This is a `session.panel` contribution, not a full-screen route: /tamias
 * opens it beside the chat, `f` promotes it to full screen, and Escape closes
 * it. The panel reads two sources and keeps them distinct:
 *
 *   - the request log (SQLite, read-only) for what actually ran and what it
 *     cost;
 *   - `GET /limits` for which providers are configured now, which models are
 *     eligible, and which are cooling down after a rate limit.
 *
 * Only the newest window of the log is summarised, and every claim about a
 * model that was *not* run is labelled as an estimate. When tamias is not
 * running the panel keeps working on the log alone and says so.
 */

import { For, Show, createEffect, createMemo, createSignal, on, onMount } from "solid-js";
import type { Plugin } from "@opencode/plugin/tui";
import { homedir } from "node:os";
import path from "node:path";
import { RequestTable, type TableRow } from "../components/RequestTable";
import { SessionSidebar, type SessionEntry } from "../components/SessionSidebar";
import {
  CoolingLine,
  KpiStrip,
  Notice,
  SavingsBar,
  TopModels,
} from "../components/Summary";
import { summarizeRows, useTamiasData, type TamiasRow } from "../hooks/useTamiasData";
import { useLimits } from "../hooks/useLimits";
import { useRendererSize, type RendererLike } from "../hooks/useRendererSize";
import { ThemeProvider, usePalette } from "../ui/theme";
import { clip, count, truncateSessionId } from "../utils/format";
import type { ThemeLike } from "../utils/palette";

/** Where `tamias serve` keeps its log and price sheet, overridable for tests. */
const DATA_DIR = process.env.TAMIAS_HOME ?? path.join(homedir(), ".tamias");
export const DB_PATH = process.env.TAMIAS_DB ?? path.join(DATA_DIR, "requests.db");
export const PRICES_PATH = process.env.TAMIAS_PRICES ?? path.join(DATA_DIR, "prices.toml");

const PANEL_NAME = "tamias";

export interface PanelInputLike {
  readonly name: string;
  readonly sessionID: string;
  readonly width: number;
  readonly presentation: "panel" | "fullscreen";
  readonly focused: boolean;
  focus: () => void;
  close: () => void;
  toggleFullscreen: () => void;
}

interface KeymapCommandLike {
  id?: string;
  title?: string;
  bind?: string;
  enabled?: boolean | (() => boolean);
  run: () => void | false | Promise<void>;
}

interface KeymapLayerLike {
  enabled?: boolean | (() => boolean);
  commands?: readonly KeymapCommandLike[];
}

export type KeymapLike = Pick<Plugin.Context["keymap"], "layer" | "shortcuts">;

export interface TamiasDashboardProps {
  input: PanelInputLike;
  renderer: RendererLike | undefined;
  theme: ThemeLike;
  mode: "dark" | "light";
  keymap: KeymapLike;
}

/** The public entry: installs the host palette for every child component. */
export function TamiasDashboard(props: TamiasDashboardProps) {
  return (
    <ThemeProvider theme={props.theme} mode={props.mode}>
      <DashboardBody
        input={props.input}
        renderer={props.renderer}
        keymap={props.keymap}
      />
    </ThemeProvider>
  );
}

interface NoticeLine {
  tone: "warn" | "bad" | "muted";
  text: string;
}

function DashboardBody(props: Omit<TamiasDashboardProps, "theme" | "mode">) {
  const palette = usePalette();
  const size = useRendererSize(props.renderer);
  const data = useTamiasData(DB_PATH, PRICES_PATH);
  const limits = useLimits();

  // undefined = follow the newest session; null = all sessions; string = pin.
  const [pinned, setPinned] = createSignal<string | null | undefined>(undefined);
  const [cursor, setCursor] = createSignal(0);
  const [tableHeight, setTableHeight] = createSignal(0);
  let tableHost: { height: number } | undefined;

  /** Every metered session still inside the panel's newest-row window. */
  const sessions = createMemo<SessionEntry[]>(() => {
    const grouped = new Map<string, { count: number; last: string }>();
    for (const row of data.rows()) {
      const last = row.ts ?? "";
      const existing = grouped.get(row.session);
      if (existing) {
        existing.count += 1;
        if (last > existing.last) existing.last = last;
      } else {
        grouped.set(row.session, { count: 1, last });
      }
    }
    return [...grouped.entries()]
      .map(([id, value]) => ({ id, count: value.count, last: value.last }))
      .sort((a, b) => b.last.localeCompare(a.last))
      .slice(0, 24);
  });

  const newest = createMemo(() => sessions()[0]?.id ?? null);
  const scope = createMemo<string | null>(() =>
    pinned() === undefined ? newest() : pinned() ?? null,
  );

  const options = createMemo<Array<string | null>>(() => [
    null,
    ...sessions().map((session) => session.id),
  ]);
  const cursorAt = () => Math.min(cursor(), Math.max(0, options().length - 1));

  // Keep the cursor on the selected scope whenever the scope changes (the
  // initial mount, `a`, or a click). Moving the cursor alone does not.
  createEffect(
    on(scope, (value) => {
      const index = options().indexOf(value);
      if (index >= 0) setCursor(index);
    }),
  );

  const rows = createMemo<TableRow[]>(() =>
    data.rows().map((row) => {
      const provider = limits.providerOf(row.model_used);
      const liveTier = limits.tierOf(row.model_used);
      return {
        ...row,
        provider: provider ?? "?",
        tier: liveTier === "unknown" ? row.tier : liveTier,
      };
    }),
  );

  const scoped = createMemo<TableRow[]>(() => {
    const selected = scope();
    return selected === null
      ? rows()
      : rows().filter((row) => row.session === selected);
  });

  const summary = createMemo(() => summarizeRows(scoped()));

  const width = createMemo(() => Math.max(24, props.input.width));
  const showSidebar = createMemo(() => width() >= 110 && sessions().length > 0);
  const sidebarWidth = 24;
  const contentWidth = createMemo(() =>
    Math.max(20, width() - 2 - (showSidebar() ? sidebarWidth + 1 : 0)),
  );
  const fullscreen = () => props.input.presentation === "fullscreen";

  /** Rows the table can draw: the host's layout first, a terminal estimate next. */
  const dataRows = createMemo(() => {
    const measured = tableHeight();
    if (measured > 0) return Math.max(1, measured - 1);
    const terminal = size.height();
    return fullscreen()
      ? Math.max(3, terminal - 11)
      : Math.max(3, Math.min(12, Math.floor(terminal / 2) - 5));
  });

  const scopeLabel = () => {
    if (scope() === null) return `all sessions · ${count(sessions().length)}`;
    const id = truncateSessionId(scope()!, 8);
    return scope() === newest() ? `latest · ${id}` : `session · ${id}`;
  };

  const scopeAction = () => (scope() === null ? "latest" : "all");

  const providersLine = createMemo(() => {
    const snapshot = limits.snapshot();
    if (!limits.live() || !snapshot) return "";
    if (snapshot.providers.length === 0) return "providers  none configured";
    const ready = snapshot.providers.filter((item) => item.configured).map((item) => item.name);
    const missing = snapshot.providers
      .filter((item) => !item.configured)
      .map((item) => item.name);
    const parts: string[] = [];
    if (ready.length > 0) parts.push(ready.join(", "));
    if (missing.length > 0) parts.push(`${missing.join(", ")} (no key)`);
    return `providers  ${parts.join(" · ")}`;
  });

  const poolLine = createMemo(() => {
    const snapshot = limits.snapshot();
    if (!limits.live() || !snapshot) return "";
    const gates = Object.entries(snapshot.pool.gates)
      .filter(([, value]) => value > 0)
      .map(([gate, value]) => `${count(value)} ${gate}`)
      .join(" · ");
    const capped = snapshot.pool.truncated ? " · capped" : "";
    return `pool  ${count(snapshot.pool.candidates)}/${count(
      snapshot.pool.considered,
    )} capable${gates ? ` · refused ${gates}` : ""}${capped}`;
  });

  const notices = createMemo<NoticeLine[]>(() => {
    const lines: NoticeLine[] = [];
    const snapshot = data.snapshot();

    if (data.dbState() === "missing") {
      lines.push({
        tone: "warn",
        text: `no request log at ${DB_PATH} · start tamias serve, then route a request`,
      });
    } else if (data.dbState() === "error" && data.error()) {
      lines.push({ tone: "bad", text: `log error: ${data.error()}` });
    }

    if (data.sheetState() === "missing") {
      lines.push({
        tone: "warn",
        text: "no price sheet · only costs already stored on a row can be shown",
      });
    } else if (data.sheetState() === "error" && data.sheetError()) {
      lines.push({ tone: "warn", text: `price sheet error: ${data.sheetError()}` });
    }

    if (snapshot.truncated) {
      lines.push({
        tone: "muted",
        text: `showing the newest ${count(snapshot.rows.length)} of ${count(
          snapshot.chat_rows,
        )} metered requests`,
      });
    }

    if (data.dbState() === "ok") {
      if (snapshot.total_rows === 0) {
        lines.push({
          tone: "muted",
          text: "request log is empty · route a request through tamias",
        });
      } else if (snapshot.chat_rows === 0) {
        lines.push({
          tone: "muted",
          text: `${count(snapshot.passthrough_rows)} passthrough rows · no metered requests yet`,
        });
      } else if (scoped().length === 0) {
        lines.push({ tone: "muted", text: "no metered requests in this scope · a shows all" });
      }
    }

    return lines.slice(0, 3);
  });

  const shortcut = (id: string, fallback: string) => {
    try {
      return props.keymap.shortcuts(id)[0] ?? fallback;
    } catch {
      return fallback;
    }
  };

  const moveCursor = (delta: number) => {
    const length = options().length;
    if (length === 0) return;
    setCursor((cursorAt() + delta + length) % length);
  };

  const selectCursor = () => {
    setPinned(options()[cursorAt()] ?? null);
  };

  const toggleScope = () => {
    setPinned(scope() === null ? undefined : null);
  };

  // Panel-local keys. The layer lives only while this component is mounted and
  // only while the panel owns focus, so Escape is not captured while the user
  // is typing in the chat beside it.
  props.keymap.layer(
    () =>
      ({
        enabled: () => props.input.focused,
        commands: [
          {
            id: "tamias.panel.close",
            title: "Close tamias panel",
            bind: "escape",
            run: () => props.input.close(),
          },
          {
            id: "tamias.panel.fullscreen",
            title: "Toggle tamias full screen",
            bind: "f",
            run: () => props.input.toggleFullscreen(),
          },
          {
            id: "tamias.panel.scope",
            title: "Toggle latest session / all sessions",
            bind: "a",
            run: toggleScope,
          },
          {
            id: "tamias.panel.refresh",
            title: "Refresh tamias data",
            bind: "r",
            run: () => {
              data.refresh();
              limits.refresh();
            },
          },
          {
            id: "tamias.panel.previous",
            title: "Previous tamias session",
            bind: "k,up",
            run: () => moveCursor(-1),
          },
          {
            id: "tamias.panel.next",
            title: "Next tamias session",
            bind: "j,down",
            run: () => moveCursor(1),
          },
          {
            id: "tamias.panel.select",
            title: "Show the selected tamias session",
            bind: "return",
            run: selectCursor,
          },
        ],
      }) as KeymapLayerLike,
  );

  onMount(() => {
    if (!props.input.focused) props.input.focus();
  });

  const footer = createMemo(() => {
    const parts = [
      `${shortcut("tamias.panel.close", "esc")} close`,
      `${shortcut("tamias.panel.fullscreen", "f")} ${fullscreen() ? "split" : "fullscreen"}`,
      `${shortcut("tamias.panel.scope", "a")} ${scopeAction()}`,
      "j/k session",
      "enter select",
      `${shortcut("tamias.panel.refresh", "r")} refresh`,
    ];
    return clip(parts.join(" · "), width() - 2);
  });

  return (
    <box
      flexDirection="column"
      width="100%"
      height="100%"
      minHeight={0}
      backgroundColor={palette().bg}
      paddingLeft={1}
      paddingRight={1}
      overflow="hidden"
    >
      <box flexDirection="row" width="100%" flexShrink={0}>
        <text wrapMode="none" fg={palette().accent}>tamias</text>
        <text wrapMode="none" fg={palette().muted}>
          {" · "}
          {clip(scopeLabel(), Math.max(2, width() - 21))}
        </text>
        <box flexGrow={1} minWidth={0} />
        <text wrapMode="none" fg={limits.live() ? palette().good : palette().faint}>
          {limits.live() ? "● live" : "○ log only"}
        </text>
      </box>

      <KpiStrip summary={summary()} width={width() - 2} />
      <SavingsBar summary={summary()} width={contentWidth()} />
      <TopModels rows={scoped()} width={contentWidth()} />

      <Show when={providersLine()}>
        <text wrapMode="none" fg={palette().muted}>{clip(providersLine(), contentWidth())}</text>
      </Show>
      <Show when={poolLine()}>
        <text wrapMode="none" fg={palette().muted}>{clip(poolLine(), contentWidth())}</text>
      </Show>
      <Show when={limits.live() && limits.snapshot()}>
        <CoolingLine
          models={limits.snapshot()?.cooling_down ?? []}
          total={limits.snapshot()?.cooling_down.length ?? 0}
          width={contentWidth()}
        />
      </Show>

      <For each={notices()}>
        {(line) => <Notice tone={line.tone} text={line.text} width={width() - 2} />}
      </For>

      <box flexDirection="row" flexGrow={1} minHeight={0} width="100%">
        <Show when={showSidebar()}>
          <SessionSidebar
            sessions={sessions()}
            scope={scope()}
            newest={newest()}
            cursor={cursorAt()}
            width={sidebarWidth}
            onCursor={setCursor}
            onSelect={setPinned}
          />
        </Show>

        <box
          flexDirection="column"
          flexGrow={1}
          minWidth={0}
          minHeight={0}
          marginLeft={showSidebar() ? 1 : 0}
          ref={(element) => {
            tableHost = element as unknown as { height: number };
          }}
          onSizeChange={() => setTableHeight(tableHost?.height ?? 0)}
        >
          <RequestTable rows={scoped()} width={contentWidth()} height={dataRows()} />
        </box>
      </box>

      <box width="100%" flexShrink={0}>
        <text wrapMode="none" fg={palette().faint}>{footer()}</text>
      </box>
    </box>
  );
}

export { PANEL_NAME };
