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
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.request import build_opener, urlopen

from tamias.pricing import PriceSheet, compute_cost, load_price_sheet
from tamias.pricing_fetch import (
    OPENROUTER_MODELS_URL,
    fetch_models,
    render_sheet,
)
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.router_config import load_router_config
from tamias.store import Store
from tamias.types import Usage

#: Default model-feed URL. Two argument parsers below refer to it; the constant
#: itself now lives in :mod:`tamias.pricing_fetch`, alongside the Decimal-exact
#: fetch implementation that replaced the older float one.
DEFAULT_URL = OPENROUTER_MODELS_URL

ESTIMATE_LABEL = "estimate; ignores cache rebuild cost; not measured"
ROUTER_MODES = ("shadow", "active", "off")
SIMULATED_LABEL = "SIMULATED PRICES, NOT REAL SAVINGS"
RUN_START_TIMEOUT_SECONDS = 15
RUN_STOP_TIMEOUT_SECONDS = 5
DOCTOR_ONLINE_TIMEOUT_SECONDS = 2.0
PRICE_SHEET_STALE_DAYS = 30
# Base URL of an OpenAI-compatible upstream, given without /v1/chat/completions,
# so `run` can default to a real provider when the flag is omitted.
DEFAULT_UPSTREAM = "https://openrouter.ai/api"
DEFAULT_PRICES_NAME = "prices.toml"
UPSTREAM_ENV_VAR = "TAMIAS_UPSTREAM"

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
    # Both saving lines print UNKNOWN here rather than an amount, so this is
    # the one place the simulated banner can still be lost.  Put the warning
    # back on the line: the banner when the rows declare simulated prices, and
    # the provenance wording when the log cannot say which sheet priced them.
    bases_warning = suffix or "  [price provenance unknown]"
    if bases_differ:
        print(
            "estimated saving: UNKNOWN "
            "(stored costs and the --prices sheet use different price bases)"
            f"{bases_warning}"
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
            f"{bases_warning}"
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
    router_config: RouterConfig | None = None,
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
    config = router_config or RouterConfig(
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
        router_config=_serve_router_config(args),
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


def agent_config(agent: str, port: int, project: str | None) -> str:
    """Return a paste-only local proxy snippet; this never writes configuration."""
    prefix = f"/p/{project}" if project else ""
    root = f"http://127.0.0.1:{port}{prefix}"
    if agent == "claude-code":
        return f"export ANTHROPIC_BASE_URL={root}"
    if agent == "codex":
        return f'[model_providers.tamias]\nbase_url = "{root}/v1"\nwire_api = "responses"'
    return json.dumps(
        {
            "provider": {
                "tamias": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": f"{root}/v1"},
                }
            }
        },
        separators=(",", ":"),
    )


def doctor(
    *,
    prices: Path,
    db: Path,
    port: int,
    online: bool,
    upstream: str,
    today: date | None = None,
    opener: Callable[..., Any] = urlopen,
    port_checker: Callable[[int], bool] | None = None,
) -> int:
    """Print local readiness checks without contacting an upstream by default."""
    blocking = False
    python_version = (sys.version_info.major, sys.version_info.minor)
    if python_version >= (3, 11):
        print(f"OK Python: {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print(f"FAIL Python: {sys.version_info.major}.{sys.version_info.minor}; requires 3.11+")
        blocking = True

    try:
        sheet = load_price_sheet(prices)
        published = date.fromisoformat(sheet.date)
        age = (today or date.today()) - published
        if age.days < 0 or age.days > PRICE_SHEET_STALE_DAYS:
            print(f"WARN Price sheet: {prices} ({age.days} days old)")
        else:
            print(f"OK Price sheet: {prices} ({age.days} days old)")
    except (OSError, ValueError) as exc:
        print(f"WARN Price sheet: {exc}")

    db_directory = db.parent
    if db_directory.is_dir() and os.access(db_directory, os.W_OK):
        print(f"OK DB directory: {db_directory} writable")
    else:
        print(f"FAIL DB directory: {db_directory} is not writable")
        blocking = True

    try:
        available = port_checker(port) if port_checker else _port_is_free(port)
        if not available:
            raise OSError
        print(f"OK Port: {port} free")
    except OSError:
        print(f"FAIL Port: {port} in use")
        blocking = True

    if os.getenv("OPENROUTER_API_KEY"):
        print("OK OPENROUTER_API_KEY: set")
    else:
        print("WARN OPENROUTER_API_KEY: missing")

    if online:
        try:
            with opener(upstream, timeout=DOCTOR_ONLINE_TIMEOUT_SECONDS):
                pass
            print(f"OK Upstream: {upstream} reachable")
        except OSError as exc:
            print(f"WARN Upstream: {upstream} unreachable ({exc})")
    return int(blocking)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))
    return True


def _serve_router_config(args: argparse.Namespace) -> RouterConfig:
    config = load_router_config(args.router_config, args.router_profile)
    return RouterConfig(
        profile=config.profile,
        easy_tools=config.easy_tools,
        edit_tools=config.edit_tools,
        shell_tools=config.shell_tools,
        error_markers=config.error_markers,
        big_output_chars=config.big_output_chars,
        cheap_model=args.cheap_model or config.cheap_model,
        strong_model=args.strong_model or config.strong_model,
        min_gap=args.min_gap if args.min_gap is not None else config.min_gap,
    )


def _choose_port() -> int:
    """Ask the kernel for a loopback port, then release it for the proxy."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_proxy(process: subprocess.Popen[Any], port: int) -> bool:
    """Return once the proxy accepts loopback connections, within the fixed deadline."""
    deadline = time.monotonic() + RUN_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _stop_proxy(process: subprocess.Popen[Any]) -> None:
    """Stop the proxy without leaving a listener behind for a later command."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=RUN_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _reserve_run_db(db: str | None, project: str | None) -> str:
    """Create an empty, exclusive run database path for the proxy to populate."""
    if db is not None:
        path = Path(db)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing database: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)
        return str(path)

    stem = project or "run"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = Path("tamias-runs")
    directory.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        discriminator = "" if suffix == 0 else f"-{suffix}"
        path = directory / f"{stem}-{timestamp}{discriminator}.db"
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        return str(path)


def _resolve_prices_path(explicit: str | None) -> Path | None:
    """The sheet `run` uses: the flag, else ./prices.toml, else ~/.tamias/prices.toml."""
    if explicit is not None:
        path = Path(explicit)
        return path if path.is_file() else None
    home_sheet = Path.home() / ".tamias" / DEFAULT_PRICES_NAME
    for candidate in (Path(DEFAULT_PRICES_NAME), home_sheet):
        if candidate.is_file():
            return candidate
    return None


def run(args: argparse.Namespace) -> int:
    """Run one command through a short-lived loopback proxy, then print its report."""
    child_command = args.child_command
    if child_command[:1] == ["--"]:
        child_command = child_command[1:]
    if not child_command:
        print("tamias run: COMMAND is required after --", file=sys.stderr)
        return 2

    prices_path = _resolve_prices_path(args.prices)
    if prices_path is None:
        target = args.prices if args.prices is not None else f"./{DEFAULT_PRICES_NAME}"
        print(f"no price sheet found; run: tamias prices fetch --out {target}", file=sys.stderr)
        return 2
    upstream = args.upstream or os.environ.get(UPSTREAM_ENV_VAR) or DEFAULT_UPSTREAM

    try:
        db = _reserve_run_db(args.db, args.project)
    except OSError as exc:
        print(f"tamias run: {exc}", file=sys.stderr)
        return 1

    port = _choose_port()
    serve_command = [
        sys.executable,
        "-m",
        "tamias.cli",
        "serve",
        "--upstream",
        upstream,
        "--prices",
        str(prices_path),
        "--db",
        db,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--router-mode",
        args.mode,
        "--cheap-model",
        args.cheap_model,
        "--strong-model",
        args.strong_model,
    ]
    if args.router_config:
        serve_command.extend(("--router-config", args.router_config))

    try:
        proxy_process = subprocess.Popen(serve_command)
    except OSError as exc:
        print(f"tamias run: could not start proxy: {exc}", file=sys.stderr)
        return 1

    try:
        if not _wait_for_proxy(proxy_process, port):
            print(
                f"tamias run: proxy did not accept connections within "
                f"{RUN_START_TIMEOUT_SECONDS} seconds",
                file=sys.stderr,
            )
            return 1

        prefix = f"/p/{args.project}" if args.project else ""
        root = f"http://127.0.0.1:{port}{prefix}"
        child_env = os.environ.copy()
        child_env["OPENAI_BASE_URL"] = f"{root}/v1"
        child_env["ANTHROPIC_BASE_URL"] = root
        try:
            child_process = subprocess.Popen(child_command, env=child_env)
        except OSError as exc:
            print(f"tamias run: could not start COMMAND: {exc}", file=sys.stderr)
            return 127
        try:
            child_code = child_process.wait()
        except KeyboardInterrupt:
            child_code = child_process.poll()
            if child_code is None:
                child_process.send_signal(signal.SIGINT)
                try:
                    child_code = child_process.wait(timeout=RUN_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    child_process.terminate()
                    try:
                        child_code = child_process.wait(timeout=RUN_STOP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        child_process.kill()
                        child_code = child_process.wait()
            if child_code is None:
                child_code = 130
        _stop_proxy(proxy_process)
        try:
            report(db, str(prices_path))
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            print(f"tamias report: {exc}", file=sys.stderr)
        return child_code if child_code >= 0 else 128 - child_code
    except KeyboardInterrupt:
        return 130
    finally:
        _stop_proxy(proxy_process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tamias", description="Local chat API proxy.")
    sub = parser.add_subparsers(dest="command", required=True)

    prices_parser = sub.add_parser("prices", help="manage price sheets")
    prices_sub = prices_parser.add_subparsers(dest="prices_command", required=True)
    fetch_parser = prices_sub.add_parser("fetch", help="fetch an OpenRouter model price sheet")
    fetch_parser.add_argument("--out", required=True, help="new TOML price sheet path")
    fetch_parser.add_argument("--url", default=DEFAULT_URL, help="model-feed URL")
    fetch_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )

    serve_parser = sub.add_parser("serve", help="run the proxy in front of an upstream")
    serve_parser.add_argument(
        "--upstream",
        required=True,
        help="base URL of the OpenAI-compatible upstream, without /v1/chat/completions",
    )
    serve_parser.add_argument("--router-profile", choices=("generic", "legacy"), default="generic")
    serve_parser.add_argument("--router-config", help="TOML router configuration path")
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
        default=None,
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

    run_parser = sub.add_parser("run", help="run one command through a short-lived proxy")
    run_parser.add_argument("--mode", choices=ROUTER_MODES, default="shadow")
    run_parser.add_argument("--project")
    run_parser.add_argument("--cheap-model", default="")
    run_parser.add_argument("--strong-model", default="")
    run_parser.add_argument("--db", help="new sqlite request log path")
    run_parser.add_argument(
        "--prices",
        default=None,
        help=f"path to the TOML price sheet (default: ./{DEFAULT_PRICES_NAME}, else "
        f"~/.tamias/{DEFAULT_PRICES_NAME})",
    )
    run_parser.add_argument(
        "--upstream",
        default=None,
        help="base URL of the OpenAI-compatible upstream, without /v1/chat/completions "
        f"(default: ${UPSTREAM_ENV_VAR} if set, else {DEFAULT_UPSTREAM})",
    )
    run_parser.add_argument("--router-config", help="TOML router configuration path")
    run_parser.add_argument("child_command", nargs=argparse.REMAINDER, metavar="COMMAND")

    report_parser = sub.add_parser("report", help="summarise cost and shadow routing")
    report_parser.add_argument("--db", required=True, help="path to the proxy sqlite log")
    report_parser.add_argument(
        "--prices",
        required=True,
        help="path to the TOML price sheet used for pricing; a sheet with "
        "`simulated = true` labels every amount it produces as simulated",
    )
    dashboard_parser = sub.add_parser("dashboard", help="serve the local run dashboard")
    dashboard_parser.add_argument("--db", required=True, help="path to the proxy sqlite log")
    dashboard_parser.add_argument("--prices", required=True, help="path to the TOML price sheet")
    dashboard_parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    agent_parser = sub.add_parser("agent-config", help="print an agent configuration snippet")
    agent_parser.add_argument("agent", choices=("claude-code", "codex", "opencode"))
    agent_parser.add_argument("--port", type=int, default=8000)
    agent_parser.add_argument("--project")
    doctor_parser = sub.add_parser("doctor", help="check local tamias readiness")
    doctor_parser.add_argument(
        "--prices", default="prices.toml", help="path to the TOML price sheet"
    )
    doctor_parser.add_argument("--db", default="requests.db", help="path to the sqlite request log")
    doctor_parser.add_argument("--port", type=int, default=8000, help="port to check")
    doctor_parser.add_argument("--upstream", default=DEFAULT_URL, help="upstream URL for --online")
    doctor_parser.add_argument(
        "--online", action="store_true", help="also check upstream reachability"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prices" and args.prices_command == "fetch":
        output = Path(args.out)
        if output.exists() and not args.force:
            print(
                f"tamias prices fetch: refusing to overwrite {output}; pass --force",
                file=sys.stderr,
            )
            return 1
        try:
            fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
            result = fetch_models(build_opener(), args.url)
            output.write_text(
                render_sheet(result.models, fetched_at, args.url), encoding="utf-8"
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"tamias prices fetch: {exc}", file=sys.stderr)
            return 1
        reasons = Counter(entry.reason for entry in result.skipped)
        skipped = ", ".join(
            f"{reason}: {count}" for reason, count in sorted(reasons.items())
        )
        free = sum(
            1
            for price in result.models.values()
            if price.input == 0 and price.output == 0
        )
        print(f"models: {len(result.models)}")
        print(f"free: {free}")
        print(f"skipped by reason: {skipped or 'none'}")
        return 0
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
    if args.command == "run":
        return run(args)
    if args.command == "dashboard":
        from tamias.dashboard import main as dashboard_main

        return dashboard_main(
            [
                "--db",
                args.db,
                "--prices",
                args.prices,
                "--host",
                args.host,
                "--port",
                str(args.port),
            ]
        )
    if args.command == "agent-config":
        print(agent_config(args.agent, args.port, args.project))
        return 0
    if args.command == "doctor":
        return doctor(
            prices=Path(args.prices),
            db=Path(args.db),
            port=args.port,
            online=args.online,
            upstream=args.upstream,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
