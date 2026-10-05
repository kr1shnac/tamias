"""A local web page over the proxy's sqlite log: what ran, and what it cost.

The proxy already writes one metadata row per request.  This module reads that
log back -- read-only, so it can never disturb a log the proxy is still writing
-- prices every row with the same :mod:`tamias.pricing` arithmetic ``tamias
report`` uses, and serves the result as one self-contained page: no CDN, no
external font, no fetched script.

Two lines are drawn for every run.  The *baseline* re-prices each request on the
model that was asked for, and the *routed* line prices it on the model that
actually answered.  Both use the row's own token counts, so the gap between them
is what the routing decision was worth and nothing else.

An amount that cannot be computed is never shown as zero.  A row whose token
counts were never reported is counted as a request, marked "not priced", and left
out of the totals; a percentage whose denominator is zero is reported as n/a
rather than as 0%.

Run it with ``python -m tamias.dashboard --db LOG --prices SHEET``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from tamias.pricing import PriceSheet, compute_cost, load_price_sheet
from tamias.types import Usage

__all__ = [
    "build_rows",
    "summary",
    "create_dashboard_app",
    "build_parser",
    "main",
]

TABLE = "requests"

# The columns the page shows.  A log written before one of them existed simply
# does not have it: PRAGMA says so, the query leaves it out, and the value comes
# back as None.  An absent column is UNKNOWN, never a zero and never an error.
COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("id", ("id",)),
    ("session", ("session_id",)),
    ("model_requested", ("model_requested",)),
    ("model_used", ("model_used",)),
    ("decision", ("decision_action", "decision")),
    ("effort_requested", ("effort_requested",)),
    ("effort_used", ("effort_used",)),
    ("input_tokens", ("input_tokens",)),
    ("output_tokens", ("output_tokens",)),
    ("cached_input_tokens", ("cached_input_tokens",)),
    ("cache_write_tokens", ("cache_write_tokens",)),
    ("price_sheet", ("price_sheet",)),
    ("price_simulated", ("price_simulated",)),
    ("provider_cost_usd", ("provider_cost_usd",)),
    ("latency_ms", ("latency_ms",)),
    ("status", ("status",)),
    ("decision_target_model", ("decision_target_model",)),
    ("decision_reason", ("decision_reason",)),
)

TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")

SIMULATED_BANNER = "SIMULATED PRICES - NOT REAL SAVINGS"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the log read-only.

    ``mode=ro`` is the whole point: the dashboard watches a file another
    process is appending to, and a reader that could write would race the
    proxy for the page.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _int_or_none(value: Any) -> int | None:
    """An INTEGER column, or UNKNOWN.  A bool is not a token count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_rows(db_path: str | Path, sheet: PriceSheet) -> list[dict[str, Any]]:
    """One dict per logged request, oldest first, priced with *sheet*.

    Reads only the columns the file actually has (PRAGMA table_info decides),
    so an older log loads and its missing optional columns -- latency_ms, the
    effort columns -- come back as None rather than raising.

    Each row carries ``actual_cost`` (the model that answered),
    ``baseline_cost`` (the model that was asked for, on the same tokens),
    ``saved`` (their difference), ``switched`` (whether the two models differ)
    and ``priced`` (whether either cost could be computed at all).
    """
    path = Path(db_path)
    if not path.is_file():
        return []

    with _connect_ro(path) as conn:
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")}

        selected: list[str] = []
        sources: dict[str, str] = {}
        for name, aliases in COLUMNS:
            for alias in aliases:
                if alias in present:
                    if alias not in selected:
                        selected.append(alias)
                    sources[name] = alias
                    break

        if not selected:
            return []

        order = "id" if "id" in present else "rowid"
        order_column = order if order in selected else selected[0]
        quoted = ", ".join(f'"{name}"' for name in selected)
        statement = f'SELECT {quoted} FROM "{TABLE}" ORDER BY "{order_column}"'
        fetched = [dict(row) for row in conn.execute(statement)]

    rows: list[dict[str, Any]] = []
    for raw in fetched:
        # Every declared column gets a key whether or not the log has it, so a
        # caller asking for latency_ms on an old log reads UNKNOWN instead of
        # tripping over a missing key.
        row: dict[str, Any] = {name: raw.get(alias) for name, alias in sources.items()}
        for name, _aliases in COLUMNS:
            row.setdefault(name, None)
        for column in TOKEN_COLUMNS:
            row[column] = _int_or_none(row[column])
        rows.append(_price(row, sheet))
    return rows


def _usage(row: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cached_input_tokens=row.get("cached_input_tokens"),
        cache_write_tokens=row.get("cache_write_tokens"),
    )


def _price(row: dict[str, Any], sheet: PriceSheet) -> dict[str, Any]:
    """Add the cost fields to one row, leaving UNKNOWN as None."""
    requested = row.get("model_requested")
    used = row.get("model_used")
    usage = _usage(row)

    actual = compute_cost(used, usage, sheet).usd if used else None
    baseline = compute_cost(requested, usage, sheet).usd if requested else None
    priced = actual is not None and baseline is not None

    row["actual_cost"] = actual
    row["baseline_cost"] = baseline
    row["saved"] = baseline - actual if priced else None
    row["switched"] = bool(
        isinstance(requested, str) and isinstance(used, str) and requested != used
    )
    row["priced"] = priced
    simulated = row.get("price_simulated")
    row["price_simulated"] = (
        True if simulated in (1, True) else False if simulated in (0, False) else None
    )
    return row


def summary(
    rows: list[dict[str, Any]],
    simulated: bool = False,
    compare_rows: list[dict[str, Any]] | None = None,
    requested_price_sheet: str | None = None,
) -> dict[str, Any]:
    """Headline numbers and the two cumulative series the chart draws.

    Totals sum priced rows only.  The unpriced ones are still counted in
    ``n_requests`` and reported as ``n_unpriced``, so a partly-known log never
    passes for a whole session.  ``saving_pct`` is None whenever it cannot be
    computed -- an empty log, a log with nothing priced in it, or a baseline
    that legitimately totals zero, where the ratio would be 0/0.
    """
    n_requests = len(rows)
    n_priced = sum(1 for row in rows if row["priced"])
    n_switched = sum(1 for row in rows if row["switched"])

    actual_total = 0.0
    baseline_total = 0.0
    cumulative_actual: list[float] = []
    cumulative_baseline: list[float] = []
    for row in rows:
        if row["priced"]:
            actual = row["actual_cost"] or 0.0
            baseline = row["baseline_cost"] or 0.0
            actual_total += actual
            baseline_total += baseline
        cumulative_actual.append(actual_total)
        cumulative_baseline.append(baseline_total)

    saving_total = baseline_total - actual_total
    saving_pct = None
    if n_priced and baseline_total:
        saving_pct = saving_total / baseline_total

    report: dict[str, Any] = {
        "n_requests": n_requests,
        "n_priced": n_priced,
        "n_unpriced": n_requests - n_priced,
        "n_switched": n_switched,
        "share_cheap": (n_switched / n_requests) if n_requests else None,
        "actual_total": actual_total,
        "baseline_total": baseline_total,
        "saving_total": saving_total,
        "saving_pct": saving_pct,
        "cumulative_actual": cumulative_actual,
        "cumulative_baseline": cumulative_baseline,
    }

    reconciled = [
        (row["actual_cost"], float(row["provider_cost_usd"]))
        for row in rows
        if row["priced"]
        and isinstance(row.get("provider_cost_usd"), int | float)
        and not isinstance(row.get("provider_cost_usd"), bool)
    ]
    report["provider_rows"] = len(reconciled)
    report["provider_missing"] = n_requests - len(reconciled)
    if len(reconciled) == n_requests and reconciled:
        provider_computed = sum(computed for computed, _billed in reconciled)
        provider_billed = sum(billed for _computed, billed in reconciled)
        report["provider_computed_total"] = provider_computed
        report["provider_billed_total"] = provider_billed
        report["provider_difference"] = provider_computed - provider_billed

    priced_rows = [row for row in rows if row["priced"]]
    stored_simulated = [row.get("price_simulated") for row in priced_rows]
    # A legacy log has no provenance.  Its current sheet remains a clearly
    # labelled fallback, while new logs always use their own stored flag.
    row_simulated = any(value is True for value in stored_simulated) or (
        bool(priced_rows) and all(value is None for value in stored_simulated) and simulated
    )
    if row_simulated:
        report["simulated"] = True
    report["price_sheets"] = sorted(
        {
            value
            for row in priced_rows
            if isinstance((value := row.get("price_sheet")), str) and value
        }
    )
    report["provenance_unknown"] = sum(
        1 for row in priced_rows if row.get("price_simulated") is None or not row.get("price_sheet")
    )
    if report["price_sheets"] and requested_price_sheet not in report["price_sheets"]:
        report["price_sheet_warning"] = (
            f"WARNING: requested price sheet {requested_price_sheet} differs from "
            "stored price sheet "
            f"{', '.join(report['price_sheets'])}"
        )

    if compare_rows is not None:
        compare_priced = sum(1 for row in compare_rows if row["priced"])
        compare_actual = 0.0
        cumulative_compare: list[float] = []
        for row in compare_rows:
            if row["priced"]:
                compare_actual += row["actual_cost"] or 0.0
            cumulative_compare.append(compare_actual)
        report["compare"] = {
            "n_requests": len(compare_rows),
            "n_priced": compare_priced,
            "actual_total": compare_actual,
            "cumulative_actual": cumulative_compare,
        }
        report["compare_total"] = compare_actual

    return report


# --- the page -------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tamias live routing</title>
<style>
:root {
  --ink: #16181d;
  --muted: #5f6672;
  --line: #dfe3ea;
  --panel: #ffffff;
  --page: #f6f7f9;
  --accent: #2f6fd0;
  --cheap: #1f8a5f;
  --warn-bg: #fdecec;
  --warn-ink: #9b1c1c;
  --warn-line: #f0b4b4;
  --switch: #fff6e8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--page);
  color: var(--ink);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
h1 { margin: 0 0 4px; font-size: 20px; }
.sub { margin: 0 0 18px; color: var(--muted); font-size: 13px; }
.banner {
  background: var(--warn-bg);
  color: var(--warn-ink);
  border: 1px solid var(--warn-line);
  border-radius: 6px;
  font-weight: 700;
  padding: 10px 14px;
  margin-bottom: 18px;
  text-align: center;
}
.banner[hidden] { display: none; }
.cards {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  margin-bottom: 18px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 14px;
}
.card .label {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: .04em;
}
.card .value { font-size: 22px; font-weight: 650; margin-top: 4px; }
.card .note { color: var(--muted); font-size: 12px; margin-top: 2px; }
.na { color: var(--muted); font-weight: 400; font-style: italic; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 18px;
}
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.controls .group { display: flex; gap: 6px; align-items: center; }
.controls .sep { width: 1px; height: 22px; background: var(--line); }
button {
  font: inherit;
  color: var(--ink);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 5px 11px;
  cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
button:disabled { opacity: .45; cursor: default; }
button[aria-pressed="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px; font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 20px; height: 0; border-top-width: 2px; border-top-style: solid; }
.chart { width: 100%; height: 260px; display: block; }
.chart .axis { stroke: var(--line); stroke-width: 1; }
.chart text { fill: var(--muted); font-size: 10px; }
.chart .empty { fill: var(--muted); font-size: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
th { color: var(--muted); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.switched td { background: var(--switch); }
tr.switched td.used { color: var(--cheap); font-weight: 600; }
tbody tr.new td { animation: flash 900ms ease-out; }
@keyframes flash { from { background: #dbe9ff; } to { background: transparent; } }
.not-priced { color: var(--muted); font-style: italic; }
.status { color: var(--muted); font-size: 12px; margin: 10px 2px 0; min-height: 18px; }
</style>
</head>
<body>
<h1>Tamias live routing</h1>
<p class="sub" id="subtitle">reading the request log</p>

<p class="banner" id="sim-banner" hidden>SIMULATED PRICES - NOT REAL SAVINGS</p>

<div class="cards">
  <div class="card">
    <div class="label">Routed cost</div>
    <div class="value" id="card-routed">n/a</div>
    <div class="note" id="card-routed-note">priced requests only</div>
  </div>
  <div class="card">
    <div class="label">Baseline cost</div>
    <div class="value" id="card-baseline">n/a</div>
    <div class="note" id="card-baseline-note">same tokens, requested model</div>
  </div>
  <div class="card">
    <div class="label">Saving</div>
    <div class="value" id="card-saving">n/a</div>
    <div class="note" id="card-saving-note">baseline minus routed</div>
  </div>
  <div class="card">
    <div class="label">On the cheaper model</div>
    <div class="value" id="card-share">n/a</div>
    <div class="note" id="card-share-note">of requests</div>
  </div>
</div>

<div class="panel">
  <div class="controls">
    <div class="group">
      <button id="mode-live" aria-pressed="true">Live</button>
      <button id="mode-replay" aria-pressed="false">Replay</button>
    </div>
    <span class="sep"></span>
    <div class="group">
      <button id="replay-play">Play</button>
      <button id="replay-pause" disabled>Pause</button>
      <button id="replay-restart" disabled>Restart</button>
    </div>
    <span class="sep"></span>
    <div class="group" id="speeds">
      <button data-speed="0.5" aria-pressed="false">0.5x</button>
      <button data-speed="1" aria-pressed="true">1x</button>
      <button data-speed="2" aria-pressed="false">2x</button>
      <button data-speed="5" aria-pressed="false">5x</button>
    </div>
  </div>
  <svg class="chart" id="chart" viewBox="0 0 720 260" preserveAspectRatio="none"
       role="img" aria-label="Cumulative cost per request"></svg>
  <div class="legend">
    <span><i class="swatch" style="border-top-color:#9aa2b1"></i>baseline (requested model)</span>
    <span><i class="swatch" style="border-top-color:#2f6fd0"></i>routed (model that answered)</span>
    <span id="legend-compare" hidden><i class="swatch"
      style="border-top-color:#1f8a5f;border-top-style:dashed"></i>actual baseline run</span>
  </div>
</div>

<div class="panel">
  <table>
    <thead>
      <tr>
        <th class="num">#</th>
        <th>Session</th>
        <th>Requested model</th>
        <th>Used model</th>
        <th>Decision</th>
        <th>Effort</th>
        <th class="num">In</th>
        <th class="num">Out</th>
        <th class="num">Cached</th>
        <th class="num">Cost</th>
        <th class="num">Saved</th>
        <th class="num">Latency</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <p class="status" id="status"></p>
</div>

<script>
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var COLORS = { baseline: "#9aa2b1", routed: "#2f6fd0", compare: "#1f8a5f" };
  var BASE_INTERVAL_MS = 1000;
  var speeds = [0.5, 1, 2, 5];

  var el = {
    banner: document.getElementById("sim-banner"),
    subtitle: document.getElementById("subtitle"),
    cardRouted: document.getElementById("card-routed"),
    cardRoutedNote: document.getElementById("card-routed-note"),
    cardBaseline: document.getElementById("card-baseline"),
    cardSaving: document.getElementById("card-saving"),
    cardSavingNote: document.getElementById("card-saving-note"),
    cardShare: document.getElementById("card-share"),
    cardShareNote: document.getElementById("card-share-note"),
    modeLive: document.getElementById("mode-live"),
    modeReplay: document.getElementById("mode-replay"),
    play: document.getElementById("replay-play"),
    pause: document.getElementById("replay-pause"),
    restart: document.getElementById("replay-restart"),
    speeds: document.getElementById("speeds"),
    chart: document.getElementById("chart"),
    legendCompare: document.getElementById("legend-compare"),
    tbody: document.getElementById("rows"),
    status: document.getElementById("status")
  };

  var state = {
    mode: "live",
    speed: 1,
    rows: [],
    revealed: 0,
    timer: null,
    inflight: false,
    poll: null,
    lastTotal: null
  };

  // Every value that reaches the page goes through textContent.  A model id or
  // a session id is text from the outside world; innerHTML would let it become
  // markup, so nothing here ever assigns markup.
  function put(node, value) {
    node.textContent = value === null || value === undefined ? "" : String(value);
  }

  function isMissing(value) {
    return value === null || value === undefined || value === "";
  }

  function money(value) {
    if (typeof value !== "number") {
      return null;
    }
    var abs = Math.abs(value);
    if (abs === 0) {
      return "$0.00";
    }
    return "$" + (abs < 1 ? value.toFixed(6) : value.toFixed(4));
  }

  function count(value) {
    return typeof value === "number" ? value.toLocaleString() : null;
  }

  function percent(value) {
    if (typeof value !== "number") {
      return null;
    }
    return (value * 100).toFixed(1) + "%";
  }

  function show(node, text, known) {
    if (known === false || text === null) {
      node.textContent = "n/a";
      node.className = "value na";
      return;
    }
    node.textContent = text;
    node.className = "value";
  }

  function svg(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, String(attrs[key]));
      }
    }
    return node;
  }

  function fetchJson(path) {
    return fetch(path, { headers: { accept: "application/json" } }).then(function (res) {
      if (!res.ok) {
        throw new Error(path + " responded " + res.status);
      }
      return res.json();
    });
  }

  // --- the table ---------------------------------------------------------

  var COLUMNS = [
    { key: "id", numeric: true },
    { key: "session", numeric: false },
    { key: "model_requested", numeric: false },
    { key: "model_used", numeric: false, className: "used" },
    { key: "decision", numeric: false },
    { key: "effort_used", numeric: false },
    { key: "input_tokens", numeric: true },
    { key: "output_tokens", numeric: true },
    { key: "cached_input_tokens", numeric: true },
    { key: "actual_cost", numeric: true, money: true },
    { key: "saved", numeric: true, money: true },
    { key: "latency_ms", numeric: true }
  ];

  function cellText(row, column) {
    var value = row[column.key];
    if (column.key === "actual_cost" || column.key === "saved") {
      return row.priced ? money(value) : "not priced";
    }
    if (column.key === "effort_used" || column.key === "effort_requested") {
      return isMissing(value) ? null : value;
    }
    if (isMissing(value)) {
      return column.key === "latency_ms" ? null : "unknown";
    }
    if (column.key === "input_tokens" || column.key === "output_tokens" ||
        column.key === "cached_input_tokens") {
      return count(value);
    }
    if (column.key === "latency_ms") {
      return count(value) + " ms";
    }
    return String(value);
  }

  function makeRow(row, fresh) {
    var tr = document.createElement("tr");
    if (row.switched) {
      tr.className = "switched";
    }
    if (fresh) {
      tr.className += " new";
    }
    for (var i = 0; i < COLUMNS.length; i += 1) {
      var column = COLUMNS[i];
      var td = document.createElement("td");
      if (column.numeric) {
        td.className = "num";
      }
      if (column.className) {
        td.className += " " + column.className;
      }
      var text = cellText(row, column);
      if (text === "not priced" || text === "unknown") {
        td.className += " not-priced";
      }
      // textContent, never innerHTML.
      td.textContent = text === null ? "" : text;
      tr.appendChild(td);
    }
    return tr;
  }

  function renderRows(limit, freshFrom) {
    var fragment = document.createDocumentFragment();
    for (var i = 0; i < limit && i < state.rows.length; i += 1) {
      fragment.appendChild(makeRow(state.rows[i], i >= freshFrom));
    }
    el.tbody.replaceChildren(fragment);
  }

  // --- the chart ---------------------------------------------------------

  var PLOT = { left: 58, right: 12, top: 14, bottom: 26 };

  function drawChart(report) {
    var width = 720;
    var height = 260;
    var innerW = width - PLOT.left - PLOT.right;
    var innerH = height - PLOT.top - PLOT.bottom;

    while (el.chart.firstChild) {
      el.chart.removeChild(el.chart.firstChild);
    }

    var series = [
      { points: report.cumulative_baseline, color: COLORS.baseline, dash: "" },
      { points: report.cumulative_actual, color: COLORS.routed, dash: "" }
    ];
    if (report.compare && report.compare.cumulative_actual.length) {
      series.push({
        points: report.compare.cumulative_actual,
        color: COLORS.compare,
        dash: "6 4"
      });
      el.legendCompare.hidden = false;
    } else {
      el.legendCompare.hidden = true;
    }

    var count_n = report.n_requests;
    var peak = 0;
    for (var s = 0; s < series.length; s += 1) {
      var points = series[s].points;
      for (var p = 0; p < points.length; p += 1) {
        if (points[p] > peak) {
          peak = points[p];
        }
      }
    }

    el.chart.appendChild(svg("line", {
      x1: PLOT.left, y1: PLOT.top, x2: PLOT.left, y2: PLOT.top + innerH, class: "axis"
    }));
    el.chart.appendChild(svg("line", {
      x1: PLOT.left, y1: PLOT.top + innerH, x2: PLOT.left + innerW,
      y2: PLOT.top + innerH, class: "axis"
    }));

    if (!count_n) {
      var note = svg("text", {
        x: PLOT.left + innerW / 2,
        y: PLOT.top + innerH / 2,
        "text-anchor": "middle",
        class: "empty"
      });
      note.textContent = "no requests logged yet";
      el.chart.appendChild(note);
      return;
    }

    function xAt(index) {
      if (count_n === 1) {
        return PLOT.left + innerW / 2;
      }
      return PLOT.left + (index / (count_n - 1)) * innerW;
    }

    function yAt(value) {
      if (peak <= 0) {
        return PLOT.top + innerH;
      }
      return PLOT.top + innerH - (value / peak) * innerH;
    }

    var yLabel = svg("text", {
      x: PLOT.left - 8, y: PLOT.top + 4, "text-anchor": "end"
    });
    yLabel.textContent = "$" + peak.toFixed(4);
    el.chart.appendChild(yLabel);

    var zeroLabel = svg("text", {
      x: PLOT.left - 8, y: PLOT.top + innerH + 3, "text-anchor": "end"
    });
    zeroLabel.textContent = "$0";
    el.chart.appendChild(zeroLabel);

    var firstX = svg("text", { x: PLOT.left, y: height - 8, "text-anchor": "start" });
    firstX.textContent = "request 1";
    el.chart.appendChild(firstX);

    if (count_n > 1) {
      var lastX = svg("text", { x: PLOT.left + innerW, y: height - 8, "text-anchor": "end" });
      lastX.textContent = "request " + count_n;
      el.chart.appendChild(lastX);
    }

    for (var d = 0; d < series.length; d += 1) {
      var line = series[d];
      var coords = [];
      for (var i = 0; i < line.points.length; i += 1) {
        coords.push(xAt(i).toFixed(2) + "," + yAt(line.points[i]).toFixed(2));
      }
      if (line.points.length === 1) {
        // One request is one point, not a line.  A zero-length path would draw
        // nothing at all, so the single value gets a marker instead.
        var only = coords[0].split(",");
        el.chart.appendChild(svg("circle", {
          cx: only[0], cy: only[1], r: 3.5, fill: line.color
        }));
        continue;
      }
      var attrs = {
        x1: 0, y1: 0, x2: 0, y2: 0,
        fill: "none",
        stroke: line.color,
        "stroke-width": line.dash ? "1.75" : "2",
        "stroke-linejoin": "round",
        "stroke-linecap": "round"
      };
      if (line.dash) {
        attrs["stroke-dasharray"] = line.dash;
      }
      attrs.d = "M" + coords.join("L");
      el.chart.appendChild(svg("path", attrs));
    }
  }

  // --- headline ----------------------------------------------------------

  function renderSummary(report) {
    var simulated = report.simulated === true;
    el.banner.hidden = !simulated;

    var billing = report.provider_billed_total === undefined
      ? "; billed: UNKNOWN (" + report.provider_missing + " of " + report.n_requests + ")"
      : "; computed vs billed: " + money(report.provider_computed_total) + " vs " +
        money(report.provider_billed_total) + "; difference: " + money(report.provider_difference);
    var warning = report.price_sheet_warning ? "; " + report.price_sheet_warning : "";
    var provenance = report.provenance_unknown
      ? "; provenance unknown for " + report.provenance_unknown + " priced rows"
      : report.price_sheets && report.price_sheets.length
        ? "; prices stored from " + report.price_sheets.join(", ")
        : "; provenance unknown";
    put(el.subtitle, report.n_requests
      ? report.n_requests + (report.n_requests === 1 ? " request logged, " : " requests logged, ") +
        report.n_priced + " priced, " + report.n_unpriced + " not priced" +
        provenance + billing + warning
      : "no requests logged yet");

    show(el.cardRouted, money(report.actual_total));
    var routedProv = "";
    if (report.simulated) {
      routedProv = " (SIMULATED prices)";
    } else if (report.provenance_unknown > 0) {
      routedProv = " (provenance unknown)";
    } else if (report.price_sheets && report.price_sheets.length) {
      routedProv = " (list prices from " + report.price_sheets.join(", ") + ")";
    }
    put(el.cardRoutedNote, report.n_priced
      ? "over " + report.n_priced + " priced requests" + routedProv
      : "no priced requests");

    show(el.cardBaseline, money(report.baseline_total));

    if (report.saving_pct === null || report.saving_pct === undefined) {
      show(el.cardSaving, null, false);
      put(el.cardSavingNote, report.n_priced
        ? "baseline totals zero"
        : "nothing priced yet");
    } else {
      show(el.cardSaving, percent(report.saving_pct) + " (" + money(report.saving_total) + ")");
      put(el.cardSavingNote, "baseline minus routed");
    }

    if (report.share_cheap === null || report.share_cheap === undefined) {
      show(el.cardShare, null, false);
      put(el.cardShareNote, "no requests yet");
    } else {
      show(el.cardShare, percent(report.share_cheap));
      put(el.cardShareNote, report.n_switched + " of " + report.n_requests + " requests");
    }

    drawChart(report);
  }

  // --- live and replay ---------------------------------------------------

  function stopTimer() {
    if (state.timer !== null) {
      window.clearTimeout(state.timer);
      state.timer = null;
    }
  }

  function stopPolling() {
    if (state.poll !== null) {
      window.clearInterval(state.poll);
      state.poll = null;
    }
  }

  function schedule() {
    stopTimer();
    if (state.mode !== "replay" || state.revealed >= state.rows.length) {
      return;
    }
    state.timer = window.setTimeout(step, BASE_INTERVAL_MS / state.speed);
  }

  function step() {
    state.timer = null;
    if (state.mode !== "replay" || state.revealed >= state.rows.length) {
      finishReplay();
      return;
    }
    state.revealed += 1;
    renderRows(state.revealed, state.revealed - 1);
    put(el.status, "replay " + state.revealed + " of " + state.rows.length);
    schedule();
  }

  function finishReplay() {
    state.revealed = state.rows.length;
    renderRows(state.revealed, state.revealed);
    put(el.status, "replay finished");
    syncReplayButtons();
  }

  function syncReplayButtons() {
    var has = state.rows.length > 0;
    var replaying = state.mode === "replay";
    el.play.disabled = !replaying || !has || state.revealed >= state.rows.length;
    el.pause.disabled = !replaying || !has || state.revealed >= state.rows.length;
    el.restart.disabled = !replaying || !has;
  }

  function setMode(mode) {
    state.mode = mode;
    el.modeLive.setAttribute("aria-pressed", mode === "live" ? "true" : "false");
    el.modeReplay.setAttribute("aria-pressed", mode === "replay" ? "true" : "false");
    stopTimer();
    stopPolling();

    if (mode === "live") {
      state.revealed = state.rows.length;
      refresh(true);
      state.poll = window.setInterval(refresh, BASE_INTERVAL_MS);
      put(el.status, "live: polling every second");
    } else {
      state.revealed = 0;
      renderRows(0, 0);
      put(el.status, "replay paused at 0 of " + state.rows.length);
      syncReplayButtons();
    }
  }

  function refresh(first) {
    if (state.inflight) {
      return;
    }
    state.inflight = true;
    fetchJson("/api/rows").then(function (rows) {
      state.inflight = false;
      var previous = state.lastTotal;
      state.rows = Array.isArray(rows) ? rows : [];
      state.lastTotal = state.rows.length;
      if (state.mode === "live") {
        state.revealed = state.rows.length;
        var fresh = previous === null ? state.revealed : previous;
        renderRows(state.revealed, first ? state.revealed : fresh);
      } else if (state.revealed === 0 && first) {
        renderRows(0, 0);
      } else {
        renderRows(state.revealed, state.revealed);
      }
      return fetchJson("/api/summary").then(renderSummary).catch(function (err) {
        put(el.status, String(err));
      });
    }).catch(function (err) {
      state.inflight = false;
      put(el.status, String(err));
    });
  }

  el.modeLive.addEventListener("click", function () { setMode("live"); });
  el.modeReplay.addEventListener("click", function () { setMode("replay"); });
  el.play.addEventListener("click", function () {
    if (state.revealed >= state.rows.length) {
      state.revealed = 0;
      renderRows(0, 0);
    }
    put(el.status, "replaying");
    syncReplayButtons();
    step();
  });
  el.pause.addEventListener("click", function () {
    stopTimer();
    syncReplayButtons();
    put(el.status, "replay paused at " + state.revealed + " of " + state.rows.length);
  });
  el.restart.addEventListener("click", function () {
    stopTimer();
    state.revealed = 0;
    renderRows(0, 0);
    syncReplayButtons();
    put(el.status, "replay restarted");
    step();
  });

  el.speeds.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-speed]");
    if (!button) {
      return;
    }
    var value = parseFloat(button.getAttribute("data-speed"));
    if (isNaN(value)) {
      return;
    }
    state.speed = value;
    var all = el.speeds.querySelectorAll("button[data-speed]");
    for (var i = 0; i < all.length; i += 1) {
      all[i].setAttribute("aria-pressed", all[i] === button ? "true" : "false");
    }
    if (state.mode === "replay") {
      schedule();
    }
  });

  // One seed fetch, then live polling.  An empty log is a normal state: the
  // page renders its zeros rather than treating them as an error.
  refresh(true);
  state.poll = window.setInterval(refresh, BASE_INTERVAL_MS);
  put(el.status, "live: polling every second");
})();
</script>
</body>
</html>
"""


def create_dashboard_app(
    db_path: str | Path,
    prices_path: str | Path,
    compare_db: str | Path | None = None,
) -> Any:
    """Build the dashboard ASGI app, without starting a server.

    The price sheet is loaded once, at startup: it is the arithmetic the whole
    page is built on, and a half-loaded one would make every number on it
    wrong in a way nobody could see.  Each request re-reads the log instead, so
    live mode sees rows written after the server started.

    *compare_db* is a second log to draw as "actual baseline run": the cost a
    real, unrouted session actually incurred, against this run's estimate.
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    sheet = load_price_sheet(prices_path)
    compare_path = Path(compare_db) if compare_db is not None else None
    has_compare = compare_path is not None

    app = FastAPI(title="Tamias live routing", docs_url=None, redoc_url=None)

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(content=PAGE)

    @app.get("/api/summary", include_in_schema=False)
    async def api_summary() -> dict[str, Any]:
        compare_rows = build_rows(compare_path, sheet) if has_compare else None
        return summary(
            build_rows(db_path, sheet),
            simulated=sheet.simulated,
            compare_rows=compare_rows,
            requested_price_sheet=str(prices_path),
        )

    @app.get("/api/rows", include_in_schema=False)
    async def api_rows() -> list[dict[str, Any]]:
        return build_rows(db_path, sheet)

    return app


def build_parser() -> argparse.ArgumentParser:
    """The command line for ``python -m tamias.dashboard``."""
    parser = argparse.ArgumentParser(
        prog="python -m tamias.dashboard",
        description="Serve a local page over the proxy's request log.",
    )
    parser.add_argument("--db", required=True, help="path to the proxy sqlite log, read read-only")
    parser.add_argument(
        "--prices", required=True, help="path to the TOML price sheet used to price it"
    )
    parser.add_argument(
        "--compare-db",
        default=None,
        help="a second log to draw as the actual baseline run",
    )
    parser.add_argument("--port", type=int, default=8010, help="port to listen on (default: 8010)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind; loopback only by default",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Serve the dashboard until interrupted."""
    args = build_parser().parse_args(argv)

    # The log holds one session's traffic and the page exposes its cost.  That
    # belongs on loopback; binding it wider should be a deliberate act, so a
    # non-loopback host has to be asked for by name.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"tamias dashboard: refusing to bind {args.host}: the request log is "
            "private to this machine. Pass --host 0.0.0.0 if you mean it.",
            file=sys.stderr,
        )
        return 1

    try:
        sheet = load_price_sheet(args.prices)
        app = create_dashboard_app(args.db, args.prices, compare_db=args.compare_db)
    except (OSError, ValueError) as exc:
        print(f"tamias dashboard: {exc}", file=sys.stderr)
        return 1

    if not Path(args.db).is_file():
        print(
            f"tamias dashboard: no log at {args.db}; serving an empty page until the "
            "proxy creates it",
            file=sys.stderr,
        )

    print(
        f"tamias dashboard: http://{args.host}:{args.port}\n"
        f"tamias dashboard: request log {args.db}, prices {args.prices} ({sheet.date})",
        file=sys.stderr,
    )
    if sheet.simulated:
        print(
            f"tamias dashboard: this price sheet declares itself simulated, so every "
            f"amount on the page is labelled: {SIMULATED_BANNER}",
            file=sys.stderr,
        )
    if args.compare_db:
        print(f"tamias dashboard: comparing against {args.compare_db}", file=sys.stderr)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
