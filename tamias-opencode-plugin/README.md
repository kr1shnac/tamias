# tamias-opencode-plugin

Live routing and cost panel for [tamias](../), rendered inside the OpenCode
TUI.

`/tamias` opens a `session.panel` contribution beside the chat: the request log
on one side, the chat on the other. `f` promotes it to full screen, `esc` closes
it. Nothing is a separate route any more, so the panel never navigates you away
from the conversation you were reading.

## What it shows

The panel reads two independent sources and keeps them distinct:

| Source | What it answers |
| --- | --- |
| `~/.tamias/requests.db` (read-only) | what actually ran, on which model, what it cost, what it saved |
| `GET /limits` on the running server | which providers are configured now, which models are eligible, what is cooling down |

- **KPI line** — requests, spent, switches, free share. Every count is
  qualified when the window is truncated.
- **Savings bar** — one labelled bar instead of a cumulative chart:
  `100% saved · $2.10 of $2.10 baseline (est.)`. A baseline for a model that was
  never run is marked `(est.)`; it is arithmetic, not a measurement.
- **Top models** — the models that actually carried the requests in scope.
- **Providers** — every configured provider, with unconfigured ones marked
  `(no key)` rather than hidden.
- **Pool** — how many models pass the capability/supply/health gates right now,
  and which gate refused the rest.
- **Cooling** — rate-limited models with a readable time until they return.
- **Request table** — `tier`, used model, asked model, provider, cost, saved,
  tokens, time, latency, status. `●` is free, `$` is paid, `?` is unknown; the
  glyphs mean the table still reads in a monochrome terminal.
- **Sessions sidebar** — appears at 110 columns or wider; step through it with
  `j`/`k` and confirm with `enter`.

Only real data is shown. When tamias is not running the panel degrades to
`○ log only` and keeps rendering the request log with no invented provider rows.
When the log or price sheet is missing, the panel says which one and what it
costs you, rather than drawing an empty chart.

## Install

Add the directory to the TUI plugin list in `~/.config/opencode/cli.json`:

```json
{
  "plugins": ["/absolute/path/to/tamias-opencode-plugin"]
}
```

Restart OpenCode, open a session, then run `/tamias`. The plugin is TUI-only:
the package has no `exports` field, so the host probes root file names, finds
`tui.tsx`, and loads no `server`/`index` part.

## Keys

| Key | Action |
| --- | --- |
| `esc` | close the panel |
| `f` | toggle full screen / split |
| `a` | toggle latest session / all sessions |
| `j` `k` (`↓` `↑`) | move the session cursor |
| `enter` | show the session under the cursor |
| `r` | refresh the log and `/limits` now |

`esc` is always printed in the panel footer, and the first open in a process
also shows a one-line toast: `esc closes · f full screen`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TAMIAS_DB` | `~/.tamias/requests.db` | request log, opened read-only |
| `TAMIAS_PRICES` | `~/.tamias/prices.toml` | price sheet, for tier and baseline estimates |
| `TAMIAS_HOME` | `~/.tamias` | directory for the two defaults above |
| `TAMIAS_URL` | `http://127.0.0.1:8000` | running tamias server for `/limits` |
| `TAMIAS_TOKEN` | unset | bearer token, when the server has auth enabled |
| `TAMIAS_API_KEY` | unset | same token under the more familiar name; `TAMIAS_TOKEN` wins |

The panel never reads provider credentials. `/limits` reports a provider as
configured or not; the keys themselves stay with the tamias server and the
provider configuration.

The log is opened read-only and polled on a timer, because two writers on one
SQLite file is a good way to break the one that matters. The panel never
creates the database, never migrates it, and keeps showing the last good
snapshot if a read fails mid-flight.

## Verify

```sh
npm install
npx tsc --noEmit
bun scripts/verify.ts
```

`bun` is only the runtime for the harness; dependencies come from the committed
`package-lock.json`.

`scripts/verify.ts` reproduces the host end to end: it resolves entrypoints the
way OpenCode does, imports `tui.tsx`, runs `setup` against a stub context,
mounts the `session.panel` contribution with OpenTUI's test renderer against a
temporary SQLite fixture and a stub `/limits` server, and asserts the things
that broke before — theme-derived colours, tier and provider columns,
latest/all scoping, every panel key, reflow at 80 columns, and the log-only
degradation when the server is gone.

Set `TAMIAS_VERIFY_DUMP=1` to print the rendered 120x44 and 80x30 frames.

`bunfig.toml` preloads `scripts/preload.ts`, which installs OpenTUI's Solid
transform. Without it, Bun resolves `solid-js` to its non-reactive SSR build and
the panel renders exactly once — a harness artifact, not a plugin bug.
