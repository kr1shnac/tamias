# tamias-opencode-plugin

A compact routing and cost pop-up for [tamias](../), rendered inside the
OpenCode TUI.

`/tamias` opens a centred dialog over the conversation. It answers the question
you ask mid-session — *what did that just cost, which model actually ran, and is
the pool healthy?* — in eight lines, then `esc` closes it. It is not a
full-screen route and not a side panel, so it never navigates you away from the
chat or squeezes the transcript.

The pop-up deliberately shows the summary, not the request table: a modal is
for one glance, and the log is still available through `tamias report` and
`/tamias-report`.

## What it shows

| Line | What it answers |
| --- | --- |
| header | `● live` when `/limits` answered, `○ log only` when it did not; `· shadow` when the newest row ran in shadow mode |
| savings bar | percent **realised**, dollars realised, baseline, and `(est.)` when any arm is an estimate |
| KPI line | requests, spent, applied switches, free share, failures, unpriced rows |
| latest route | requested model → used model, free/paid/unknown glyph, decision |
| reason | the router's own reason for that decision |
| pool | capable/considered candidates, refused gates, capped, providers with no key |
| cooling | rate-limited models with status and time until they return |
| footer | `esc close · log 2s · live 5s` |

Only real data is shown. The pop-up reads two independent sources and keeps
them distinct:

| Source | What it answers |
| --- | --- |
| `~/.tamias/requests.db` (read-only) | what actually ran, on which model, what it cost, what it saved |
| `GET /limits` on the running server | which providers are configured now, which models are eligible, what is cooling down |

When tamias is not running the pop-up degrades to `○ log only` and keeps
rendering the log with no invented provider rows. The live line says
`unavailable · showing the log alone` and puts the raw transport error last, so
a narrow terminal clips the detail rather than the claim. When the log or price
sheet is missing, the pop-up names the missing file instead of drawing an empty
chart.

Free and paid stay visibly different: `●` is free, `$` is paid, `?` is unknown.
A model the price sheet does not quote is unknown, never free by assumption. A
baseline for a model that was not run is marked `(est.)`; it is arithmetic over
list prices, not a provider invoice.

The bar counts only rows the proxy actually rewrote, which is why it says
*realised*. In shadow mode nothing is rewritten, so it stays at zero and the
header says `· shadow`; `tamias report` (and `/tamias-report`) is where the
hypothetical `estimated saving` for shadow decisions is printed. The two
numbers answer different questions and are labelled accordingly.

## Install

`tamias opencode setup` registers this directory in `~/.config/opencode/cli.json`
when it can find it: pass `--plugin /absolute/path/to/tamias-opencode-plugin`,
set `$TAMIAS_PLUGIN_DIR`, or run setup from a source checkout. To do it by hand:

```json
{
  "plugins": ["/absolute/path/to/tamias-opencode-plugin"]
}
```

Restart OpenCode, then run `/tamias` from any session — or from no session at
all. The plugin is TUI-only: the package has no `exports` field, so the host
probes root file names, finds `tui.tsx`, and loads no `server`/`index` part.

The plugin is not published to npm. A `pip install tamias` does not carry it;
the source checkout does, and `bun install` in this directory is only needed for
type-checking and the verify harness, not for OpenCode to load the pop-up.

`tamias opencode install` writes the report command. With the plugin present it
uses `tamias-report.md`, so `/tamias` belongs to the pop-up and
`/tamias-report` injects `tamias report` output into the model.

## Keys

| Key | Action |
| --- | --- |
| `esc` | close the pop-up |

The dialog is host-owned, so `esc`, a click-away, or another plugin replacing
the dialog all fire the same close callback. `/tamias` while it is open closes
it. The first open in a process shows a one-line hint: `esc closes · /tamias
opens it again`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TAMIAS_DB` | `~/.tamias/requests.db` | request log, opened read-only |
| `TAMIAS_PRICES` | `~/.tamias/prices.toml` | price sheet, for tier and baseline estimates |
| `TAMIAS_HOME` | `~/.tamias` | directory for the two defaults above |
| `TAMIAS_URL` | `http://127.0.0.1:8000` | running tamias server for `/limits` |
| `TAMIAS_TOKEN` | unset | bearer token, when the server has auth enabled |
| `TAMIAS_API_KEY` | unset | same token under the more familiar name; `TAMIAS_TOKEN` wins |

The pop-up never reads provider credentials. `/limits` reports a provider as
configured or not; the keys themselves stay with the tamias server and the
provider configuration.

The log is opened read-only and polled every two seconds, because two writers on
one SQLite file is a good way to break the one that matters. The plugin never
creates the database, never migrates it, and keeps showing the last good
snapshot if a read fails mid-flight.

## Verify

```sh
bun install
bun run typecheck
bun scripts/verify.ts
```

`bun scripts/verify.ts` reproduces the host end to end: it resolves entrypoints
the way OpenCode does, imports `tui.tsx`, runs `setup` against a stub context,
opens the dialog the `/tamias` command shows, and renders the pop-up with
OpenTUI's test renderer against a temporary SQLite fixture and a stub `/limits`
server. It asserts the contract that matters:

- the plugin claims no `session.panel` slot and needs no session;
- `/tamias` opens a `large`, centred dialog and a second invocation clears it;
- `esc` and the host close callback reset the open flag;
- the pop-up is compact (eight non-empty lines, no table or sidebar);
- the latest route, savings bar, pool, cooling line and footer carry real
  fixture values;
- a narrow 80-column terminal reflows without clipping the shell;
- an unreachable `/limits` degrades to the log without inventing providers.

Set `TAMIAS_VERIFY_DUMP=1` to print the rendered 120x44, 80x30 and offline
frames.

`bunfig.toml` preloads `scripts/preload.ts`, which installs OpenTUI's Solid
transform. Without it, Bun resolves `solid-js` to its non-reactive SSR build and
the pop-up renders exactly once — a harness artifact, not a plugin bug.
