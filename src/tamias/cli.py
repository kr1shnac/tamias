"""Command line entry point for tamias.

``tamias serve`` runs the proxy in front of an OpenAI-compatible upstream and
writes one metadata row per request to a sqlite log.  ``tamias report`` reads
that log read-only using the standard library, prices a request with the same
``tamias.pricing`` arithmetic the proxy used, and prints the request count, the
cost total, how many requests shadow mode would have switched, and a saving
estimate.  Every estimate is labelled: it is arithmetic over logged token
counts, not a measurement.  A price sheet that declares itself simulated has
every amount it produces labelled too, so invented rates are never presented as
money.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from tamias.pricing import PriceSheet, compute_cost, load_price_sheet
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.store import Store
from tamias.types import Usage

ESTIMATE_LABEL = "estimate; ignores cache rebuild cost; not measured"
ROUTER_MODES = ("shadow", "active", "off")
SIMULATED_LABEL = "SIMULATED PRICES, NOT REAL SAVINGS"

# store.Store owns the schema; these are the columns the report reads, with a
# little tolerance for a renamed table or column.
REQUEST_TABLES = ("requests",)
COST_COLUMNS = ("cost_usd", "cost", "usd")
PRICE_SHEET_COLUMNS = ("price_sheet",)
PRICE_SIMULATED_COLUMNS = ("price_simulated",)
DECISION_COLUMNS = ("decision_action", "decision")
MODEL_USED_COLUMNS = ("model_used", "model")


def _money(value: float) -> str:
    if value == 0:
        return "$0.00"
    return f"${value:.6f}" if abs(value) < 1 else f"${value:.4f}"


def _cost_suffix(simulated: bool) -> str:
    """The simulated-prices banner, on every line that quotes an amount.

    A sheet that declares ``simulated = true`` is making its rates up, so an
    amount derived from it is an illustration and not money.  The banner rides
    with the figure rather than being printed once at the top, so a line copied
    out of the report on its own still says which kind of number it is.
    """
    return f"  [{SIMULATED_LABEL}]" if simulated else ""


def _pick(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _read_rows(db_path: str) -> list[dict[str, Any]]:
    """Read the request log read-only, oldest first."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"no such database: {db_path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        table = next((name for name in REQUEST_TABLES if name in names), None)
        if table is None:
            table = names[0] if names else None
        if table is None:
            raise ValueError(f"{db_path}: no tables")
        quoted = '"' + table.replace('"', '""') + '"'
        return [dict(row) for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")]


def _stored_cost(row: dict[str, Any]) -> float | None:
    """The cost the proxy logged, or None when it is UNKNOWN.

    A NULL cost_usd is UNKNOWN, never zero: the request may well have been
    billed, we just cannot prove how much.
    """
    raw = _pick(row, COST_COLUMNS)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def _stored_simulated(row: dict[str, Any]) -> bool | None:
    """Whether this row's stored number was made with simulated prices."""
    raw = _pick(row, PRICE_SIMULATED_COLUMNS)
    if raw in (0, False):
        return False
    if raw in (1, True):
        return True
    return None


def _stored_price_sheet(row: dict[str, Any]) -> str | None:
    raw = _pick(row, PRICE_SHEET_COLUMNS)
    return raw if isinstance(raw, str) and raw else None


def _would_switch(row: dict[str, Any]) -> bool:
    """True when the recorded shadow decision was SWITCH.

    The proxy persists the decision it made even when it was not acted on, so
    a SWITCH here means shadow mode would have switched this request.
    """
    action = _pick(row, DECISION_COLUMNS)
    return isinstance(action, str) and action.strip().upper() == "SWITCH"


def _count(row: dict[str, Any], name: str) -> int | None:
    raw = row.get(name)
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return int(raw)


def _usage(row: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=_count(row, "input_tokens"),
        output_tokens=_count(row, "output_tokens"),
        cached_input_tokens=_count(row, "cached_input_tokens"),
        cache_write_tokens=_count(row, "cache_write_tokens"),
    )


def _has_counts(row: dict[str, Any]) -> bool:
    """Whether the row logged the token counts a cost could be built from.

    Input and output are the two counts every rate needs.  A row missing either
    has no arithmetic behind it, so it can contribute to a total only as
    UNKNOWN — never as zero, and never as an assumed number.
    """
    return _count(row, "input_tokens") is not None and _count(row, "output_tokens") is not None


def _cheap_model(sheet: PriceSheet) -> str | None:
    """The model a switched request is assumed to have been able to use.

    A price sheet says nothing about which model is "cheap", so the report
    assumes the cheapest one in the sheet, ranked by the mean of the input and
    output rates.  A rate of 0 is a real price, not a missing one, so a local
    free model ranks cheapest exactly as it should.
    """
    ranked = [
        ((price.input + price.output) / 2, name)
        for name, price in sheet.models.items()
        if price.input is not None and price.output is not None
    ]
    if not ranked:
        return None
    return min(ranked)[1]


def _saving(row: dict[str, Any], sheet: PriceSheet, cheap: str | None) -> float | None:
    """What this request would have cost less, had the switch been acted on."""
    if cheap is None:
        return None
    used = _pick(row, MODEL_USED_COLUMNS)
    if not isinstance(used, str) or not used:
        return None
    usage = _usage(row)
    actual = compute_cost(used, usage, sheet).usd
    alternative = compute_cost(cheap, usage, sheet).usd
    if actual is None or alternative is None:
        return None
    return max(0.0, actual - alternative)


def report(db_path: str, prices_path: str) -> None:
    """Print the cost and shadow-routing summary for one database.

    Every line that quotes an amount carries a banner when the price sheet
    declares itself simulated, so a made-up rate cannot be read as a real bill.
    """
    sheet = load_price_sheet(prices_path)
    rows = _read_rows(db_path)
    cheap = _cheap_model(sheet)

    known_total = 0.0
    unknown_costs = 0
    switched = 0
    saved = 0.0
    unpriced = 0
    with_counts = 0

    free_count = sum(
        1
        for row in rows
        if isinstance(row.get("model_requested"), str) and row["model_requested"].endswith(":free")
    )

    realised_saved = 0.0
    realised_count = 0
    realised_unpriced = 0

    for row in rows:
        if _has_counts(row):
            with_counts += 1
        cost = _stored_cost(row)
        if cost is None:
            unknown_costs += 1
        else:
            known_total += cost
        if not _would_switch(row):
            pass
        else:
            switched += 1
            delta = _saving(row, sheet, cheap)
            if delta is None:
                unpriced += 1
            else:
                saved += delta

        model_used = _pick(row, MODEL_USED_COLUMNS)
        if model_used is not None and model_used != row["model_requested"]:
            usage = _usage(row)
            cost_requested = compute_cost(row["model_requested"], usage, sheet).usd
            cost_used = compute_cost(model_used, usage, sheet).usd
            if cost_requested is not None and cost_used is not None:
                realised_saved += max(0.0, cost_requested - cost_used)
                realised_count += 1
            else:
                realised_unpriced += 1

    priced_rows = [row for row in rows if _stored_cost(row) is not None]
    stored_flags = [_stored_simulated(row) for row in priced_rows]
    # Logs written before provenance existed cannot prove which sheet priced a
    # number.  Preserve the legacy simulated warning when the only available
    # information is a simulated report sheet, while also printing the explicit
    # ``provenance unknown`` line below.
    simulated = any(flag is True for flag in stored_flags) or (
        bool(priced_rows) and all(flag is None for flag in stored_flags) and sheet.simulated
    )
    suffix = _cost_suffix(simulated)
    stored_sheets = sorted(
        {sheet_name for row in priced_rows if (sheet_name := _stored_price_sheet(row))}
    )
    unknown_provenance = sum(
        1
        for row in priced_rows
        if _stored_simulated(row) is None or _stored_price_sheet(row) is None
    )
    print(f"requests: {len(rows)}")
    # Determine provenance qualifier for total cost
    stored_rows_simulated = any(flag is True for flag in stored_flags)
    is_legacy = unknown_provenance > 0 or (
        bool(priced_rows) and all(flag is None for flag in stored_flags)
    )
    if unknown_costs:
        if stored_rows_simulated:
            print(
                f"total cost: UNKNOWN "
                f"(known part: {_money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; SIMULATED prices)"
            )
        elif is_legacy:
            print(
                f"total cost: UNKNOWN "
                f"(known part: {_money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; price provenance unknown)"
            )
        else:
            stored_sheet_str = ", ".join(stored_sheets) if stored_sheets else str(prices_path)
            print(
                f"total cost: UNKNOWN "
                f"(known part: {_money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; list prices from {stored_sheet_str})"
            )
    else:
        if stored_rows_simulated:
            print(f"total cost (computed, not billed; SIMULATED prices): {_money(known_total)}")
        elif is_legacy:
            print(
                "total cost (computed, not billed; price provenance unknown): "
                f"{_money(known_total)}"
            )
        else:
            stored_sheet_str = ", ".join(stored_sheets) if stored_sheets else str(prices_path)
            print(
                "total cost (computed, not billed; list prices from "
                f"{stored_sheet_str}): {_money(known_total)}"
            )
    if free_count > 0:
        print(
            f"free-model rows: {free_count} of {len(rows)}; "
            "real billed cost for those is $0, so dollar figures above are hypothetical"
        )
    print(f"requests shadow would have switched: {switched}")

    billed_values: list[float] = []
    reconciled: list[tuple[float, float]] = []
    for row in rows:
        computed = _stored_cost(row)
        billed = row.get("provider_cost_usd")
        if isinstance(billed, int | float) and not isinstance(billed, bool):
            billed_value = float(billed)
            billed_values.append(billed_value)
            if computed is not None:
                reconciled.append((computed, billed_value))
    if billed_values:
        print(
            f"spent (billed by OpenRouter): {_money(sum(billed_values))} over "
            f"{len(billed_values)} of {len(rows)} requests"
        )
    else:
        print("billed cost: UNKNOWN")
    if reconciled:
        computed_total = sum(computed for computed, _billed in reconciled)
        billed_total = sum(billed for _computed, billed in reconciled)
        stored_label = ", ".join(stored_sheets) if stored_sheets else "provenance unknown"
        print(
            f"computed cost: {_money(computed_total)} (computed from list prices "
            f"({stored_label})) over {len(reconciled)} rows{suffix}"
        )
        print(
            f"difference (billed - computed): {_money(billed_total - computed_total)} over "
            f"{len(reconciled)} rows{suffix}"
        )

    if unknown_provenance:
        print(
            f"price provenance: provenance unknown "
            f"({unknown_provenance} of {len(priced_rows)} priced rows)"
        )
    elif stored_sheets:
        print(f"price provenance: {', '.join(stored_sheets)}")
    if stored_sheets and str(prices_path) not in stored_sheets:
        print(
            f"WARNING: requested price sheet {prices_path} differs from stored price sheet "
            f"{', '.join(stored_sheets)}"
        )

    # A saving is arithmetic over token counts.  If nothing was switched the
    # saving is a definite zero, but if something was switched and not one row
    # logged counts then the saving is unknown -- and `$0.00` would read as
    # "the switch would have saved nothing", which is a claim about money rather
    # than about missing data. A partly-known log gets the number plus the
    # count it rests on, so a subset is never quoted as the whole session.
    bases_differ = (
        (stored_rows_simulated or unknown_provenance > 0 or is_legacy)
        and stored_sheets
        and str(prices_path) not in stored_sheets
    )
    if bases_differ:
        print(
            "estimated saving: UNKNOWN "
            "(stored costs and the --prices sheet use different price bases)"
        )
    elif switched and not with_counts:
        print(f"estimated saving: UNKNOWN (0 of {len(rows)} requests have token counts){suffix}")
    else:
        line = f"estimated saving: {_money(saved)} ({ESTIMATE_LABEL}"
        if with_counts < len(rows):
            line += f"; based on {with_counts} of {len(rows)} requests with token counts"
        if unpriced:
            line += f"; {unpriced} of {switched} switched requests not priced"
        line += f"){suffix}"
        print(line)

    # --- realised saving (rows the proxy actually rewrote) ---
    # Rows where model_used differs from model_requested: the proxy actually
    # rewrote the request to a cheaper model. Baseline = cost of model_requested
    # on the row's own usage; effective model = model_used.
    if bases_differ:
        line1 = (
            "realised saving: UNKNOWN "
            "(stored costs and the --prices sheet use different price bases)"
        )
    elif realised_count > 0 or realised_unpriced > 0:
        if realised_count > 0:
            line1 = (
                f"realised saving (rows the proxy actually rewrote): "
                f"{_money(realised_saved)} over {realised_count} rows "
                f"({ESTIMATE_LABEL}"
            )
            if realised_unpriced and realised_count > realised_unpriced:
                line1 += f"; {realised_unpriced} of {realised_count} not priced"
            line1 += f"){suffix}"
        else:
            line1 = (
                f"realised saving: UNKNOWN over {realised_unpriced + realised_count} "
                f"rows ({ESTIMATE_LABEL}){suffix}"
            )
    else:
        line1 = ""

    print(line1)
    print(f"cheap model assumed: {cheap or 'unknown'}")
    print(
        "sheet passed on the command line (used only for savings estimates): "
        f"{prices_path} ({sheet.date})"
    )


def build_serve_app(
    upstream: str,
    prices: str,
    db: str,
    router_mode: str = "shadow",
    *,
    cheap_model: str = "",
    strong_model: str = "",
    min_gap: int = 3,
    inject_usage: bool = False,
    request_usage_cost: bool = False,
    transport: Any | None = None,
) -> Any:
    """Build the proxy ASGI app for ``tamias serve``, without starting a server.

    Kept separate from :func:`serve` so the wiring (price sheet, sqlite log,
    router config) can be exercised without binding a port.  ``transport`` is the
    httpx transport the upstream is reached through; ``serve`` leaves it as
    ``None`` for a real socket, and a test passes the suite's in-process one.
    """
    from tamias import proxy

    sheet = load_price_sheet(prices)
    store = Store(db)
    config = RouterConfig(
        easy_tools=EASY_TOOLS,
        cheap_model=cheap_model,
        strong_model=strong_model,
        min_gap=min_gap,
    )
    app = proxy.create_app(
        upstream,
        store,
        sheet,
        router_mode,
        config=config,
        inject_usage=inject_usage,
        request_usage_cost=request_usage_cost,
        transport=transport,
    )
    app.state.store = store
    return app


def serve(args: argparse.Namespace) -> int:
    """Run the proxy under uvicorn until interrupted."""
    import uvicorn

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_serve_app(
        args.upstream,
        args.prices,
        args.db,
        args.router_mode,
        cheap_model=args.cheap_model,
        strong_model=args.strong_model,
        min_gap=args.min_gap,
        inject_usage=args.inject_usage,
        request_usage_cost=args.request_usage_cost,
    )
    print(
        f"tamias serve: {args.router_mode} mode, "
        f"http://{args.host}:{args.port}{proxy_path()} -> {args.upstream}{proxy_path()}",
        file=sys.stderr,
    )
    print(f"tamias serve: request log {args.db}, prices {args.prices}", file=sys.stderr)
    if args.inject_usage:
        print(
            "tamias serve: --inject-usage is ON: streaming requests are re-encoded "
            "with stream_options.include_usage=true, which adds a final chunk "
            "carrying usage and empty choices",
            file=sys.stderr,
        )
    if args.request_usage_cost:
        print(
            "tamias serve: --request-usage-cost is ON: requests include usage.include=true",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    store: Store | None = getattr(app.state, "store", None)
    if store is not None:
        store.close()
    return 0


def proxy_path() -> str:
    """The chat path the proxy answers on, for the startup banner."""
    from tamias.proxy import CHAT_PATH

    return CHAT_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tamias", description="Local chat API proxy.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="run the proxy in front of an upstream")
    serve_parser.add_argument(
        "--upstream",
        required=True,
        help="base URL of the OpenAI-compatible upstream, without /v1/chat/completions",
    )
    serve_parser.add_argument("--prices", required=True, help="path to the TOML price sheet")
    serve_parser.add_argument("--db", required=True, help="path to the sqlite request log")
    serve_parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    serve_parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    serve_parser.add_argument(
        "--router-mode",
        choices=ROUTER_MODES,
        default="shadow",
        help="shadow records the routing decision, active also applies it (default: shadow)",
    )
    serve_parser.add_argument(
        "--cheap-model",
        default="",
        help="model a SWITCH would move mechanical work to; without it a shadow "
        "decision has no target and no measurable saving",
    )
    serve_parser.add_argument(
        "--strong-model", default="", help="model the agent is expected to start on"
    )
    serve_parser.add_argument(
        "--min-gap",
        type=int,
        default=3,
        help="requests that must pass after the last switch before switching again",
    )
    serve_parser.add_argument(
        "--inject-usage",
        action="store_true",
        help="ask the upstream for a usage chunk on streaming requests even when "
        "the client did not (default: off, which forwards the body byte-for-byte). "
        "Adds one final chunk with usage and empty choices",
    )
    serve_parser.add_argument(
        "--request-usage-cost",
        action="store_true",
        help="ask OpenRouter to include usage and provider-reported cost "
        "(default: off; off forwards the body byte-for-byte)",
    )
    serve_parser.add_argument("--verbose", action="store_true", help="log every proxy event")

    report_parser = sub.add_parser("report", help="summarise cost and shadow routing")
    report_parser.add_argument("--db", required=True, help="path to the proxy sqlite log")
    report_parser.add_argument(
        "--prices",
        required=True,
        help="path to the TOML price sheet used for pricing; a sheet with "
        "`simulated = true` labels every amount it produces as simulated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        try:
            report(args.db, args.prices)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            print(f"tamias report: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "serve":
        try:
            return serve(args)
        except (OSError, ValueError) as exc:
            print(f"tamias serve: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
