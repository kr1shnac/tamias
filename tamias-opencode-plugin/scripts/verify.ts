/**
 * Verification harness for the tamias TUI plugin.
 *
 * Reproduces OpenCode's plugin host behaviour end to end without restarting the
 * editor:
 *   1. resolve entrypoints the way `Host.resolve` does (server/tui/rpc)
 *   2. import the tui entrypoint exactly as the TUI runtime would
 *   3. run `setup` against a stub context and capture the `/tamias` command
 *   4. open the dialog the command shows, and render it with OpenTUI's test
 *      renderer against a temporary SQLite fixture and a stub `/limits` server
 *
 * The checks defend the pop-up contract: no session is required, no panel slot
 * is claimed, the dialog is centred and large, Escape closes it, and the body
 * stays a compact answer rather than the full dashboard.
 *
 * Run with: bun scripts/verify.ts
 */

import { Database } from "bun:sqlite";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const PLUGIN_DIR = path.resolve(import.meta.dir, "..");
const PLUGIN_NAME = "tamias-opencode-plugin";

const MISSING_CODES = new Set([
  "ENOENT",
  "ENOTDIR",
  "MODULE_NOT_FOUND",
  "ERR_MODULE_NOT_FOUND",
  "ERR_PACKAGE_PATH_NOT_EXPORTED",
  "ERR_UNSUPPORTED_DIR_IMPORT",
]);

function resolveEntry(subpaths: string[]): string | undefined {
  for (const subpath of subpaths) {
    const specifier = subpath ? `${PLUGIN_NAME}/${subpath}` : PLUGIN_NAME;
    try {
      return Bun.resolveSync(specifier, PLUGIN_DIR);
    } catch (error) {
      const code =
        error && typeof error === "object" && "code" in error
          ? String((error as { code: unknown }).code)
          : "";
      if (!MISSING_CODES.has(code)) throw error;
    }
  }
  return undefined;
}

interface Entrypoints {
  server?: string;
  tui?: string;
  rpc?: string;
}

function resolveEntrypoints(): Entrypoints {
  const pkg = JSON.parse(readFileSync(path.join(PLUGIN_DIR, "package.json"), "utf-8")) as {
    name?: string;
    exports?: unknown;
  };
  const named = typeof pkg.name === "string" && pkg.exports !== undefined;

  const viaPackage = (subpaths: string[]) => resolveEntry(subpaths);
  const viaFileName = (subpaths: string[]) => {
    for (const subpath of subpaths) {
      for (const extension of [".tsx", ".ts", ".js"]) {
        const candidate = path.join(PLUGIN_DIR, (subpath || "index") + extension);
        if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
      }
    }
    return undefined;
  };

  const entry = named ? viaPackage : viaFileName;
  return { server: entry(["server", ""]), tui: entry(["tui"]), rpc: entry(["rpc"]) };
}

function step(name: string, ok: boolean, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) process.exitCode = 1;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// --- 1. entrypoint resolution ------------------------------------------------

const entries = resolveEntrypoints();

step("resolves a tui entrypoint at the package root", Boolean(entries.tui), entries.tui ?? "none");
step("resolves no server entrypoint", !entries.server, entries.server ?? "(correct)");
step("resolves no rpc entrypoint", !entries.rpc, entries.rpc ?? "(correct)");

const tuiSpecifier = entries.tui;
if (!tuiSpecifier) {
  console.error("\nNo tui entrypoint found — nothing to verify.");
  process.exit(1);
}

// --- 2. a temporary log, price sheet, and /limits stub ----------------------

const TEMP = mkdtempSync(path.join("/tmp/opencode", "tamias-verify-"));
const DB_PATH = path.join(TEMP, "requests.db");
const PRICES_PATH = path.join(TEMP, "prices.toml");

writeFileSync(
  PRICES_PATH,
  `date = "2026-11-02"
simulated = false

["acme/free-one"]
input = 0.0
output = 0.0
cached_input = 0.0

["acme/paid-two"]
input = 1.0
output = 2.0
cached_input = 0.1

["acme/old-three"]
input = 2.0
output = 3.0
cached_input = 0.2
`,
  "utf-8",
);

function makeLog() {
  const db = new Database(DB_PATH);
  db.exec(`
    CREATE TABLE requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      session_id TEXT NOT NULL,
      model_requested TEXT,
      model_used TEXT,
      input_tokens INTEGER,
      output_tokens INTEGER,
      cached_input_tokens INTEGER,
      cache_write_tokens INTEGER,
      cost_usd REAL,
      price_sheet_date TEXT,
      latency_ms INTEGER,
      status TEXT,
      decision_action TEXT,
      decision_target_model TEXT,
      decision_reason TEXT,
      price_sheet TEXT,
      price_simulated INTEGER,
      provider_cost_usd REAL,
      generation_id TEXT,
      project TEXT,
      effort_requested TEXT,
      effort_used TEXT,
      decision_target_effort TEXT,
      computed_list_cost_usd REAL,
      cost_source TEXT,
      failure_stage TEXT,
      failure_type TEXT,
      router_mode TEXT,
      route TEXT
    )
  `);
  const insert = db.prepare(`
    INSERT INTO requests (
      ts, session_id, model_requested, model_used, input_tokens, output_tokens,
      cached_input_tokens, cache_write_tokens, cost_usd, latency_ms, status,
      decision_action, decision_target_model, decision_reason, price_sheet,
      price_simulated, provider_cost_usd, computed_list_cost_usd, cost_source,
      router_mode, route
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
  `);

  const rows = [
    // A real switch: a paid prompt answered by a free model.  The baseline is
    // warm, so the saving is priced against a cache read, not a fresh prompt.
    {
      ts: "2026-11-03T10:03:00+00:00",
      sid: "auto-aaaaaaaaaaaa",
      req: "acme/paid-two",
      used: "acme/free-one",
      input: 1_000_000,
      output: 1_000_000,
      // The newest row is a real switch, and it is also where the prompt came
      // back warm: the cache share is what makes a cache-aware switch legible,
      // so the fixture must show a non-trivial one rather than an accidental 0.
      cached: 800_000,
      ms: 1200,
      status: "200",
      action: "SWITCH",
      target: "acme/free-one",
      reason: "cheaper capable model",
      billed: 0,
      listed: 0,
      source: "computed",
      route: "chat",
    },
    {
      ts: "2026-11-03T10:02:00+00:00",
      sid: "auto-aaaaaaaaaaaa",
      req: "acme/paid-two",
      used: "acme/paid-two",
      input: 1000,
      output: 500,
      cached: 100,
      ms: 900,
      status: "200",
      action: "STAY",
      target: null,
      reason: "requested model is capable",
      billed: null,
      listed: 0.001,
      source: "computed",
      route: "chat",
    },
    {
      ts: "2026-11-03T10:01:00+00:00",
      sid: "auto-aaaaaaaaaaaa",
      req: "acme/free-one",
      used: "acme/free-one",
      input: 2000,
      output: 300,
      cached: 0,
      ms: 700,
      status: "200",
      action: "STAY",
      target: null,
      reason: "requested model is capable",
      billed: 0,
      listed: 0,
      source: "computed",
      route: "chat",
    },
    // An older session.  The pop-up summarises it but has no table, so its
    // model must not leak into the latest-route line.
    {
      ts: "2026-11-03T09:02:00+00:00",
      sid: "auto-bbbbbbbbbbbb",
      req: "acme/old-three",
      used: "acme/old-three",
      input: 3000,
      output: 700,
      cached: 0,
      ms: 1500,
      status: "200",
      action: "STAY",
      target: null,
      reason: "requested model is capable",
      billed: null,
      listed: 0.004,
      source: "computed",
      route: "chat",
    },
    {
      ts: "2026-11-03T09:01:00+00:00",
      sid: "auto-bbbbbbbbbbbb",
      req: "acme/paid-two",
      used: "acme/old-three",
      input: 5000,
      output: 1000,
      cached: 0,
      ms: 1100,
      status: "200",
      action: "SWITCH",
      target: "acme/old-three",
      reason: "cheaper capable model",
      billed: 0,
      listed: 0,
      source: "computed",
      route: "chat",
    },
    {
      ts: "2026-11-03T09:00:00+00:00",
      sid: "auto-bbbbbbbbbbbb",
      req: "acme/paid-two",
      used: "acme/paid-two",
      input: 100,
      output: 50,
      cached: 0,
      ms: 400,
      status: "402",
      action: "STAY",
      target: null,
      reason: "credit exhausted",
      billed: null,
      listed: null,
      source: "unknown",
      route: "chat",
    },
    // Passthrough rows are count-only and must stay out of the summary.
    {
      ts: "2026-11-03T08:59:00+00:00",
      sid: "auto-cccccccccccc",
      req: "-",
      used: "-",
      input: null,
      output: null,
      cached: null,
      ms: 5,
      status: "404",
      action: "STAY",
      target: null,
      reason: "passthrough",
      billed: null,
      listed: null,
      source: "unknown",
      route: "passthrough",
    },
  ];

  // Insert oldest first so the autoincrement id follows time.  The reader
  // orders by id, exactly as it does against a live append-only log.
  for (const row of [...rows].reverse()) {
    insert.run(
      row.ts,
      row.sid,
      row.req,
      row.used,
      row.input,
      row.output,
      row.cached,
      0,
      row.ms,
      row.status,
      row.action,
      row.target,
      row.reason,
      PRICES_PATH,
      row.billed,
      row.listed,
      row.source,
      "active",
      row.route,
    );
  }
  db.close();
}

makeLog();

let authorized = 0;
let limitsCalls = 0;
const server = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== "/limits") return new Response("not found", { status: 404 });
    if (request.headers.get("authorization") !== "Bearer verify-token") {
      return new Response("unauthorized", { status: 401 });
    }
    authorized += 1;
    limitsCalls += 1;
    const models = Number(url.searchParams.get("models") ?? "0");
    return Response.json({
      providers: [
        {
          name: "openrouter",
          base_url: "https://openrouter.ai/api/v1",
          configured: true,
          key_source: "environment",
        },
        {
          name: "nvidia",
          base_url: "https://integrate.api.nvidia.com/v1",
          configured: false,
          key_source: "none",
        },
      ],
      unconfigured: ["nvidia"],
      // Only openrouter publishes a quota; nvidia sends nothing, so it must
      // not appear in the line at all rather than appear as "?".
      quota: [
        { provider: "openrouter", remaining_requests: 41, resets_in: 120 },
      ],
      cooling_down: [
        {
          provider: "openrouter",
          model: "acme/paid-two",
          status: "429",
          reason: "rate limit",
          remaining_seconds: 45,
          strikes: 1,
        },
      ],
      pool: {
        considered: 5,
        candidates: 3,
        refused: 2,
        listed: models > 0 ? 3 : 0,
        truncated: false,
        gates: { health: 2 },
      },
      candidates:
        models > 0
          ? [
              {
                model: "acme/free-one",
                provider: "openrouter",
                quality: 60,
                cost_per_mtok: 0,
                context_window: 128000,
                tools: true,
                reasoning: false,
              },
              {
                model: "acme/paid-two",
                provider: "openrouter",
                quality: 80,
                cost_per_mtok: 1.4,
                context_window: 128000,
                tools: true,
                reasoning: true,
              },
              {
                model: "acme/old-three",
                provider: "openrouter",
                quality: 75,
                cost_per_mtok: 2.5,
                context_window: 64000,
                tools: true,
                reasoning: false,
              },
            ]
          : [],
      refused: [],
    });
  },
});

process.env.TAMIAS_DB = DB_PATH;
process.env.TAMIAS_PRICES = PRICES_PATH;
process.env.TAMIAS_URL = `http://127.0.0.1:${server.port}`;
process.env.TAMIAS_TOKEN = "verify-token";

// --- 3. import the entrypoint like the TUI runtime does ----------------------

const module = await import(pathToFileURL(tuiSpecifier).href);
const definition = module.default;

step("exports a default plugin definition", Boolean(definition), Object.keys(module).join(", ") || "default only");
step("definition has an id", typeof definition?.id === "string", definition?.id ?? "missing");
step("definition has a setup function", typeof definition?.setup === "function", typeof definition?.setup);

// --- 4. run setup against a stub context -------------------------------------

interface CapturedSlot {
  append?: string;
  render: (input: unknown) => unknown;
}
interface CapturedLayer {
  mode?: string;
  enabled?: boolean | (() => boolean);
  commands?: Array<Record<string, unknown>>;
}
interface CapturedDialog {
  render: () => unknown;
  onClose?: () => void;
}

const slots: CapturedSlot[] = [];
const layers: CapturedLayer[] = [];
const dialogs: CapturedDialog[] = [];
const toasts: Array<{ title?: string; message?: string; variant?: string }> = [];
let dialogOptions: unknown;
let shows = 0;
let clears = 0;

let liveRenderer:
  | {
      readonly width: number;
      readonly height: number;
      on(event: "resize", listener: (width: number, height: number) => void): unknown;
      off(event: "resize", listener: (width: number, height: number) => void): unknown;
    }
  | undefined;

const hostRenderer = {
  get width() {
    return liveRenderer?.width ?? 120;
  },
  get height() {
    return liveRenderer?.height ?? 44;
  },
  on(event: "resize", listener: (width: number, height: number) => void) {
    return liveRenderer?.on(event, listener);
  },
  off(event: "resize", listener: (width: number, height: number) => void) {
    return liveRenderer?.off(event, listener);
  },
};

const hintStore = { shown: false };

const theme = {
  background: {
    base: "#000000",
    raised: { base: "#111111", high: "#222222", max: "#333333" },
  },
  border: { base: "#444444" },
  text: {
    base: "#eeeeee",
    muted: "#aaaaaa",
    faint: "#666666",
    feedback: {
      success: { base: "#00ff00" },
      warning: { base: "#ffaa00" },
      error: { base: "#ff0000" },
      info: { base: "#00aaff" },
    },
  },
  hue: {
    accent: { 500: "#00ffff" },
    interactive: { 500: "#0000ff" },
    neutral: { 600: "#555555" },
  },
};

const context = {
  options: {},
  location: undefined,
  app: { version: "verify", channel: "test" },
  renderer: hostRenderer,
  client: {},
  data: {},
  attention: { notify: async () => ({ ok: true, notification: false, sound: false }) },
  theme,
  themeMode: "dark" as const,
  markdown: { registerCodeBlockRenderer: () => () => {} },
  keymap: {
    layer: (factory: () => unknown) => {
      layers.push(factory() as CapturedLayer);
      return () => {};
    },
    dispatch: () => {},
    shortcuts: () => [],
    commands: () => [],
    pending: () => [],
    active: () => [],
    mode: { current: () => "base", push: () => () => {} },
  },
  storage: {
    store: () => [{}, () => {}],
    memory: () => [hintStore, (mutation: (draft: { shown: boolean }) => void) => mutation(hintStore)],
  },
  ui: {
    dialog: {
      show: (render: () => unknown, onClose?: () => void) => {
        shows += 1;
        dialogs.push({ render, onClose });
      },
      set: (options: unknown) => {
        dialogOptions = options;
      },
      clear: () => {
        // The host owns the dialog: clearing it fires the close callback.  The
        // plugin relies on that to reset its "already open" flag, so the stub
        // must model it rather than only forgetting the render function.
        clears += 1;
        const last = dialogs.pop();
        last?.onClose?.();
      },
      alert: async () => {},
      confirm: async () => undefined,
      prompt: async () => undefined,
      select: async () => undefined,
    },
    toast: { show: (options: { title?: string; message?: string; variant?: string }) => toasts.push(options) },
    format: { path: (value: string) => value },
    router: {
      register: () => () => {},
      navigate: () => {},
      current: () => ({ type: "session", sessionID: "ses_verify" }),
    },
    panel: {
      open: () => false,
      close: () => {},
      current: () => undefined,
    },
    tabs: { enabled: () => false, list: () => [], open: () => false, focus: () => false, move: () => false, close: () => false },
    model: { current: () => undefined, variant: { list: () => [], set: () => false } },
    slot: (claim: CapturedSlot) => {
      slots.push(claim);
      return () => {};
    },
  },
};

const cleanup = await definition.setup(context as never);

step("setup claims no panel slot", slots.length === 0, `${slots.length} slot(s)`);
step("setup registers a global keymap layer", layers.length === 1 && layers[0]?.mode === "global", `${layers.length} layer(s)`);

const globalCommands = layers[0]?.commands ?? [];
const openCommand = globalCommands.find((command) => command.id === "tamias.dashboard.open");
step(
  "the global layer declares /tamias",
  (openCommand?.slash as { name?: string } | undefined)?.name === "tamias" &&
    openCommand?.palette === true,
  JSON.stringify(openCommand?.slash ?? null),
);
step(
  "the command does not require a session",
  openCommand !== undefined && typeof openCommand.run === "function",
);

const run = (command: Record<string, unknown> | undefined) =>
  (command?.run as (() => void | false | Promise<void>) | undefined)?.();

await run(openCommand);
const firstDialog = dialogs[0];
step("the slash command opens a dialog", shows === 1 && Boolean(firstDialog), `${shows} show(s)`);
step(
  "the dialog is centred and large",
  JSON.stringify(dialogOptions) === JSON.stringify({ size: "large", centered: true }),
  JSON.stringify(dialogOptions ?? null),
);
step(
  "the first open explains Escape",
  toasts.at(-1)?.message?.includes("esc closes") === true,
  toasts.at(-1)?.message ?? "none",
);
const hintCount = toasts.filter((toast) => toast.message?.includes("esc closes")).length;

await run(openCommand);
step("running /tamias again clears the open dialog", clears === 1 && shows === 1);

await run(openCommand);
step(
  "the hint is shown once per process, not on every open",
  toasts.filter((toast) => toast.message?.includes("esc closes")).length === hintCount,
  `${hintCount} hint(s)`,
);

const secondDialog = dialogs[0];
secondDialog?.onClose?.();
step("the host's close callback resets the open flag", Boolean(secondDialog));

await run(openCommand);
step("Escape then /tamias opens it again", shows === 3, `${shows} show(s)`);

if (!firstDialog) {
  console.error("\nNo dialog was captured — nothing to render.");
  process.exit(1);
}

// --- 5. render the pop-up against the fixtures -------------------------------

const { createTestRenderer } = await import("@opentui/core/testing");
const { render } = await import("@opentui/solid");

function nonEmpty(frame: string): string[] {
  return frame.split("\n").filter((line) => line.trim().length > 0);
}

let mounted: Awaited<ReturnType<typeof createTestRenderer>> | undefined;
try {
  mounted = await createTestRenderer({ width: 120, height: 44 });
  liveRenderer = mounted.renderer as never;

  await render(() => firstDialog.render() as never, mounted!.renderer);
  await sleep(400);
  await mounted.flush();
  let frame = mounted.captureCharFrame();
  const lines = nonEmpty(frame);

  step("the pop-up renders", frame.includes("tamias"), `${lines.length} non-empty lines`);
  step(
    "the pop-up is compact, not the full dashboard",
    lines.length <= 12 && !frame.includes("sessions") && !frame.includes("all sessions"),
    `${lines.length} lines`,
  );
  step("the header reports the live proxy", frame.includes("● live"));
  step("the latest route names both requested and used models", frame.includes("latest") && frame.includes("paid-two") && frame.includes("free-one"));
  step("the latest route says what the router decided", frame.includes("SWITCH") && frame.includes("cheaper capable model"));
  step("the latest route marks a free model", frame.includes("● free"));
  step(
    "the savings bar reports realised savings as an estimate",
    frame.includes("realised") &&
      frame.includes("$2.10") &&
      frame.includes("baseline") &&
      frame.includes("(est.)"),
  );
  step("an active window is not labelled shadow", !frame.includes("all recent · shadow"));
  step("the KPI line counts requests and switches", frame.includes("6 req") && frame.includes("switched"));
  step(
    "the KPI line reports the cache share from the log",
    frame.includes("cache") && /\bcache\s+\d+%/.test(frame),
    frame.match(/\bcache\s+\S+/)?.join("") ?? "absent",
  );
  step("the pool line shows capable models and refused gates", frame.includes("3/5 capable") && frame.includes("2 health"));
  step("the pool line names a provider without a key", frame.includes("no key: nvidia"));
  step("the cooling line names the model and the wait", frame.includes("cooling") && frame.includes("back in 45s") && frame.includes("429"));
  step(
    "the quota line shows only what the provider published",
    frame.includes("quota") && frame.includes("openrouter 41") && frame.includes("resets 2m") && !frame.includes("nvidia ?"),
  );
  step("the footer makes Escape and the two refresh clocks discoverable", frame.includes("esc close") && frame.includes("log 2s") && frame.includes("live 5s"));
  step("the pop-up does not leak the older session's model", !frame.includes("old-three"));

  // Reflow: the host emits resize and the pop-up must fit, not clip the shell.
  mounted.resize(80, 30);
  await sleep(250);
  await mounted.flush();
  const smallFrame = mounted.captureCharFrame();
  const smallLines = nonEmpty(smallFrame);
  step(
    "the pop-up reflows at 80 columns",
    smallLines.every((line) => line.length <= 80) && smallFrame.includes("tamias"),
    `widest ${Math.max(...smallLines.map((line) => line.length))}`,
  );
  step("the compact answer survives the narrow layout", smallFrame.includes("latest") && smallFrame.includes("3/5 capable"));

  if (process.env.TAMIAS_VERIFY_DUMP) {
    console.log("\n--- popup at 120x44 ---");
    console.log(frame);
    console.log("--- popup at 80x30 ---");
    console.log(smallFrame);
    console.log("--- end ---\n");
  }
} catch (error) {
  step("the pop-up renders through OpenTUI", false, error instanceof Error ? error.message : String(error));
}

step("the plugin authenticated to /limits", authorized > 0, `${authorized} authorized call(s)`);
step("the plugin polled /limits", limitsCalls > 0, `${limitsCalls} call(s)`);

// --- 6. unit checks that do not need a live host ----------------------------

const { paletteFor } = await import("../src/utils/palette");
const mapped = paletteFor(theme, "dark");
step(
  "the palette reads background, text and feedback from the host theme",
  String(mapped.bg) === "#000000" &&
    String(mapped.text) === "#eeeeee" &&
    String(mapped.good) === "#00ff00" &&
    String(mapped.accent) === "#00ffff",
);
step(
  "a host without a theme still gets a complete light/dark fallback",
  String(paletteFor(undefined, "light").bg) !== String(paletteFor(undefined, "dark").bg),
);

const { localSheetPath, mergePriceSheets, parsePriceSheet, tierFor } = await import(
  "../src/utils/priceSheet"
);
const sheet = parsePriceSheet(readFileSync(PRICES_PATH, "utf-8"));
step("a zero-rated model is free", tierFor("acme/free-one", sheet) === "free");
step("a positive-rated model is paid", tierFor("acme/paid-two", sheet) === "paid");
step("an unquoted model is unknown, not free", tierFor("acme/missing", sheet) === "unknown");

const { priceRow, windowMode } = await import("../src/hooks/useTamiasData");
const shadowRow = priceRow(
  {
    id: 1,
    ts: "2026-11-03T10:04:00+00:00",
    session_id: "auto-dddddddddddd",
    model_requested: "acme/paid-two",
    model_used: "acme/paid-two",
    input_tokens: 1_000_000,
    output_tokens: 0,
    cached_input_tokens: 0,
    cache_write_tokens: 0,
    status: "200",
    decision_action: "SWITCH",
    decision_target_model: "acme/free-one",
    router_mode: "shadow",
  },
  sheet,
);
step(
  "a shadow decision is not counted as an applied switch",
  shadowRow.shadow && !shadowRow.switched && shadowRow.saved === 0,
  `shadow=${shadowRow.shadow} switched=${shadowRow.switched} saved=${shadowRow.saved}`,
);
step(
  "a shadow-only window is labelled shadow",
  windowMode([shadowRow]) === "shadow",
  String(windowMode([shadowRow])),
);

const { cacheShare } = await import("../src/hooks/useTamiasData");
function cacheRow(input: number | null, cached: number | null) {
  return { ...shadowRow, input_tokens: input, cached_input_tokens: cached };
}
step(
  "the cache share is the cached tokens over the input tokens",
  cacheShare([cacheRow(1000, 250), cacheRow(1000, 0)]) === 0.125,
  String(cacheShare([cacheRow(1000, 250), cacheRow(1000, 0)])),
);
step(
  "a window with no input tokens has an unknown share, not a warm zero",
  cacheShare([cacheRow(0, 0), cacheRow(null, null)]) === null,
  String(cacheShare([cacheRow(0, 0), cacheRow(null, null)])),
);
step(
  "a share is capped at fully warm however the provider counts cache",
  cacheShare([cacheRow(1000, 4000)]) === 1,
  String(cacheShare([cacheRow(1000, 4000)])),
);

const overlayPath = path.join(TEMP, "unit.local.toml");
writeFileSync(overlayPath, `["acme/overlay-only"]\ninput = 0.0\noutput = 0.0\n`);
const mergedSheet = mergePriceSheets(sheet, parsePriceSheet(readFileSync(overlayPath, "utf-8")));
step("the local overlay prices a model the sheet omits", tierFor("acme/overlay-only", mergedSheet) === "free");
step("the overlay keeps the generated rates", tierFor("acme/paid-two", mergedSheet) === "paid");
step(
  "the overlay path sits beside the sheet",
  localSheetPath("/x/prices.toml") === "/x/prices.local.toml",
);

// --- 7. the log-only degradation when tamias is not running -----------------

try {
  server.stop(true);
  process.env.TAMIAS_URL = "http://127.0.0.1:1";
  const offline = await createTestRenderer({ width: 100, height: 34 });
  liveRenderer = offline.renderer as never;
  await render(() => firstDialog.render() as never, offline.renderer);
  await sleep(350);
  await offline.flush();
  const frame = offline.captureCharFrame();
  step(
    "the pop-up degrades to the log when /limits is unreachable",
    frame.includes("log only") && frame.includes("free-one"),
    nonEmpty(frame)[0] ?? "",
  );
  step("the offline pop-up says the live view is unavailable", frame.includes("unavailable") && frame.includes("showing the log alone"));
  step("the offline pop-up does not invent provider data", !frame.includes("openrouter") && !frame.includes("nvidia"));
  if (process.env.TAMIAS_VERIFY_DUMP) {
    console.log("--- popup with /limits unreachable ---");
    console.log(frame);
    console.log("--- end ---");
  }
  await offline.dispose();
} catch (error) {
  step("the pop-up degrades to the log when /limits is unreachable", false, error instanceof Error ? error.message : String(error));
}

if (mounted) {
  await mounted.dispose();
}
if (typeof cleanup === "function") cleanup();
rmSync(TEMP, { recursive: true, force: true });
