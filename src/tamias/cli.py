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
import importlib.metadata
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

from tamias.config import (
    PROVIDER_PRESETS,
    Config,
    ConfigError,
    load_config,
    pick,
    preset_names,
    preset_upstream,
)
from tamias.effort import EFFORT_MODES
from tamias.keys import KeysError, KeyStore
from tamias.pricing import PriceSheet, compute_cost, load_price_sheet
from tamias.pricing_fetch import (
    OPENROUTER_MODELS_URL,
    fetch_models,
    render_sheet,
)
from tamias.registry import Registry, default_registry_path, load_registry
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.router_config import load_router_config
from tamias.store import Store
from tamias.types import Usage

#: Default model-feed URL. Two argument parsers below refer to it; the constant
#: itself now lives in :mod:`tamias.pricing_fetch`, alongside the Decimal-exact
#: fetch implementation that replaced the older float one.
DEFAULT_URL = OPENROUTER_MODELS_URL

ESTIMATE_LABEL = "estimate; ignores cache rebuild cost; not measured"
ROUTER_MODES = ("shadow", "off")
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

# Serve hardening (G1/G2/G3). All are opt-in knobs: the token protects a
# non-loopback bind, the body cap and concurrency limit bound a runaway agent,
# and doctor's size check nudges toward `tamias prune --keep-days`.
AUTH_TOKEN_ENV = "TAMIAS_AUTH_TOKEN"
MAX_BODY_BYTES_ENV = "TAMIAS_MAX_BODY_BYTES"
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_CONCURRENCY_ENV = "TAMIAS_MAX_CONCURRENCY"
DEFAULT_MAX_CONCURRENCY = 64
DOCTOR_DB_SIZE_WARN_MB = 500

# Company keys live outside the request log by default: the log is
# append-only metering, the key table is mutable control state, and a
# deployment may want to keep (or discard) the two separately.  ``serve``
# does not read this default - key authentication is opt-in via --keys-db -
# so no server silently starts demanding keys.
DEFAULT_KEYS_DB = "keys.db"
KEYS_DB_ENV = "TAMIAS_KEYS_DB"

# store.Store owns the schema; these are the columns the report reads, with a
# little tolerance for a renamed table or column.
REQUEST_TABLES = ("requests",)
COST_COLUMNS = ("computed_list_cost_usd", "cost_usd", "cost", "usd")
PRICE_SHEET_COLUMNS = ("price_sheet",)
PRICE_SIMULATED_COLUMNS = ("price_simulated",)
DECISION_COLUMNS = ("decision_action", "decision")
MODEL_USED_COLUMNS = ("model_used", "model")


def _warn_if_exposed(host: str) -> None:
    """Warn when a service is bound beyond loopback: it has no authentication."""
    if host not in ("127.0.0.1", "localhost", "::1") and not host.startswith("127."):
        print(
            f"tamias: WARNING — binding to {host!r}; this service has no "
            f"authentication, keep it on loopback unless you are on a trusted network",
            file=sys.stderr,
        )


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
    """The computed list-price cost the proxy logged, or None when UNKNOWN.

    Only the named ``computed_list_cost_usd`` column is trusted.  A row that
    only carries the pre-split generic ``cost_usd`` is legacy data: its source
    cannot be proven, so it remains UNKNOWN and is excluded from totals and
    reconciliation.
    """
    raw = row.get("computed_list_cost_usd")
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
    router_modes = {
        mode
        for row in rows
        if isinstance(row, dict)
        for mode in (row.get("router_mode"),)
        if isinstance(mode, str) and mode
    }
    if router_modes == {"active"}:
        print(f"requests routed to the cheap model: {switched}")
    elif len(router_modes) > 1:
        print(
            "requests shadow mode would have switched: "
            f"{switched} (mixed/historical router modes)"
        )
    else:
        print(f"requests shadow mode would have switched: {switched}")

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
            if realised_unpriced:
                # Mirror the estimated line above: whenever any rewritten row
                # went unpriced, say so, and count against every rewritten row.
                # Hiding the caveat once unpriced rows outnumber priced ones
                # would quote the priced subset as if it were the whole set.
                line1 += (
                    f"; {realised_unpriced} of "
                    f"{realised_count + realised_unpriced} rewritten rows not priced"
                )
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
    effort_style: str = "openrouter",
    effort_mode: str = "off",
    registry: Registry | None = None,
    keys_db: str | None = None,
    auth_token: str | None = None,
    max_body_bytes: int | None = None,
    max_concurrency: int | None = None,
    request_ceiling_usd: object = None,
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
    key_store = KeyStore(keys_db) if keys_db else None
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
        effort_style=effort_style,
        effort_mode=effort_mode,
        registry=registry,
        key_store=key_store,
        auth_token=auth_token,
        max_body_bytes=max_body_bytes,
        max_concurrency=max_concurrency,
        request_ceiling_usd=request_ceiling_usd,
    )
    app.state.store = store
    return app


def _load_effective_config(args: argparse.Namespace) -> Config:
    """Read the config file named on the command line (or the default)."""
    return load_config(getattr(args, "config", None))


def _flag_given(args: argparse.Namespace, flag: str) -> bool:
    """Whether ``flag`` appeared on the command line (vs. only its default).

    argparse cannot tell an explicit ``--host 127.0.0.1`` from the built-in
    default, so the raw argv recorded by :func:`main` is consulted.
    """
    for token in getattr(args, "_argv", []):
        if token == flag or token.startswith(flag + "="):
            return True
    return False


def _resolve_upstream(args: argparse.Namespace, cfg: Config) -> str | None:
    """Precedence: --upstream > $TAMIAS_UPSTREAM > --provider > config upstream/provider."""
    if getattr(args, "upstream", None):
        return str(args.upstream)
    env_upstream = os.environ.get(UPSTREAM_ENV_VAR)
    if env_upstream:
        return env_upstream
    if getattr(args, "provider", None):
        return preset_upstream(str(args.provider))
    return cfg.resolved_upstream()


def _resolve_router_mode(args: argparse.Namespace, cfg: Config) -> str:
    if _flag_given(args, "--router-mode"):
        mode: object = args.router_mode
    elif cfg.router_mode is not None:
        mode = cfg.router_mode
    else:
        mode = args.router_mode
    if mode not in ROUTER_MODES:
        allowed = ", ".join(ROUTER_MODES)
        raise ConfigError(f"router_mode {mode!r} is not one of: {allowed}")
    return str(mode)


def _resolve_tls(cert: str | None, key: str | None) -> tuple[str | None, str | None]:
    """Validate the TLS certificate/key pair. Both or neither, and both exist."""
    if cert is None and key is None:
        return None, None
    if cert is None or key is None:
        raise ConfigError("--tls-cert and --tls-key must be given together")
    for label, path in (("cert", cert), ("key", key)):
        if not Path(path).exists():
            raise ConfigError(f"--tls-{label} file not found: {path}")
    return cert, key


def config_command(args: argparse.Namespace) -> int:
    """``tamias config path|show|providers``."""
    if args.config_command == "providers":
        for name in preset_names():
            preset = PROVIDER_PRESETS[name]
            print(f"{name}\t{preset['upstream']}\t{preset['description']}")
        return 0
    try:
        cfg = _load_effective_config(args)
    except ConfigError as exc:
        print(f"tamias config: {exc}", file=sys.stderr)
        return 1
    if args.config_command == "path":
        print(cfg.path)
        return 0
    # show: report the file path plus every key that was set.
    print(f"# {cfg.path}")
    for key in (
        "upstream",
        "provider",
        "prices",
        "db",
        "router_mode",
        "cheap_model",
        "strong_model",
        "effort_style",
        "effort_mode",
        "host",
        "port",
        "min_gap",
        "inject_usage",
    ):
        value = getattr(cfg, key)
        if value is not None:
            print(f"{key} = {value!r}")
    return 0


def _default_keys_db() -> str:
    """The key store ``tamias admin`` uses when ``--keys-db`` is omitted."""
    configured = os.environ.get(KEYS_DB_ENV)
    if configured:
        return configured
    return str(Path.home() / ".tamias" / DEFAULT_KEYS_DB)


def _describe_budget(key: Any) -> str:
    if key.budget_usd is None:
        return "(none)"
    if key.period_days is None:
        return f"${key.budget_usd} total"
    return f"${key.budget_usd} per {key.period_days} days"


def _describe_caps(key: Any) -> str:
    if not key.model_caps:
        return "-"
    return ",".join(f"{name}=${cap}" for name, cap in key.model_caps)


def _fmt_ceiling(value: object) -> str:
    from tamias.budget import DEFAULT_REQUEST_CEILING_USD, parse_usd

    parsed = parse_usd(value)
    return f"${DEFAULT_REQUEST_CEILING_USD if parsed is None else parsed}"


def admin_command(args: argparse.Namespace) -> int:
    """``tamias admin key issue|list|revoke``.

    The token is printed here and nowhere else: only its SHA-256 hash is
    stored, so no later command can recover it, and ``list`` never shows it.
    """
    store = KeyStore(args.keys_db)
    try:
        if args.key_command == "issue":
            caps: list[tuple[str, str]] = []
            for item in args.model_cap:
                model, separator, amount = str(item).partition("=")
                if not separator or not model.strip() or not amount.strip():
                    raise KeysError(f"--model-cap wants MODEL=USD, got {item!r}")
                caps.append((model.strip(), amount.strip()))
            key, token = store.issue(
                label=args.label,
                project=args.project,
                models=args.model,
                budget_usd=args.budget_usd,
                period_days=args.period_days,
                rpm=args.rpm,
                tpm=args.tpm,
                model_caps=caps,
            )
            print(f"key_id:  {key.key_id}")
            print(f"token:   {token}")
            print(f"label:   {key.label or '-'}")
            print(f"project: {key.project or '-'}")
            print(f"models:  {', '.join(key.models) if key.models else '(all)'}")
            print(f"budget:  {_describe_budget(key)}")
            print(f"rpm:     {key.rpm if key.rpm is not None else '-'}")
            print(f"tpm:     {key.tpm if key.tpm is not None else '-'}")
            print(f"caps:    {_describe_caps(key)}")
            print(
                "Store the token now: only its hash is kept, so it cannot "
                "be shown again.",
                file=sys.stderr,
            )
            return 0
        if args.key_command == "list":
            keys = store.list()
            if not keys:
                print("no keys")
                return 0
            for key in keys:
                state = "revoked" if key.revoked else "active"
                models = ", ".join(key.models) if key.models else "(all)"
                print(
                    f"{key.key_id}\t{state}\t{key.label or '-'}\t"
                    f"project={key.project or '-'}\tmodels={models}\t"
                    f"budget={_describe_budget(key)}\trpm={key.rpm or '-'}\t"
                    f"tpm={key.tpm or '-'}\tcaps={_describe_caps(key)}"
                )
            return 0
        if args.key_command == "revoke":
            if store.revoke(args.key_id):
                print(f"revoked {args.key_id}")
                return 0
            print(f"tamias admin: no active key {args.key_id!r}", file=sys.stderr)
            return 1
    finally:
        store.close()
    return 2


def serve(args: argparse.Namespace) -> int:
    """Run the proxy under uvicorn until interrupted."""
    import uvicorn

    from tamias.security import is_loopback, refuse_non_loopback

    cfg = _load_effective_config(args)
    upstream = _resolve_upstream(args, cfg)
    if not upstream:
        print(
            "tamias serve: an upstream is required; pass --upstream, set "
            f"${UPSTREAM_ENV_VAR}, or set upstream/provider in the config file",
            file=sys.stderr,
        )
        return 2
    args.upstream = upstream
    args.router_mode = _resolve_router_mode(args, cfg)
    args.effort_mode = _resolve_effort_mode(args, cfg)
    if not _flag_given(args, "--host") and cfg.host is not None:
        args.host = cfg.host
    if not _flag_given(args, "--port") and cfg.port is not None:
        args.port = cfg.port

    # Refuse a wider bind before any server-side work starts.
    refused = refuse_non_loopback("serve", args.host, args.allow_non_loopback)
    if refused:
        return refused

    prices_arg = pick(args.prices, cfg.prices, None)
    prices_path = _resolve_prices_path(prices_arg)
    if prices_path is None:
        target = prices_arg if prices_arg is not None else f"./{DEFAULT_PRICES_NAME}"
        print(
            f"tamias serve: no price sheet found; run: tamias prices fetch --out {target}",
            file=sys.stderr,
        )
        return 2
    args.prices = str(prices_path)
    args.db = str(pick(args.db, cfg.db, "requests.db"))
    if not _flag_given(args, "--effort-style") and cfg.effort_style is not None:
        args.effort_style = cfg.effort_style
    if not _flag_given(args, "--cheap-model") and cfg.cheap_model is not None:
        args.cheap_model = cfg.cheap_model
    if not _flag_given(args, "--strong-model") and cfg.strong_model is not None:
        args.strong_model = cfg.strong_model

    auth_token = args.auth_token or os.environ.get(AUTH_TOKEN_ENV)
    # A wider bind needs *some* authentication.  Company keys count: requiring
    # the shared --auth-token here would force every deployment beyond loopback
    # to hand out an identity-less token that bypasses the model allowlist.
    if not is_loopback(args.host) and auth_token is None and not args.keys_db:
        print(
            f"tamias serve: refusing to bind {args.host!r}: a non-loopback bind "
            f"requires authentication. Set --auth-token or ${AUTH_TOKEN_ENV}, "
            f"or configure company keys with --keys-db.",
            file=sys.stderr,
        )
        return 1
    try:
        tls_cert, tls_key = _resolve_tls(args.tls_cert, args.tls_key)
    except ConfigError as exc:
        print(f"tamias serve: {exc}", file=sys.stderr)
        return 2
    scheme = "https" if tls_cert else "http"
    max_body_bytes = args.max_body_bytes
    if max_body_bytes is None:
        max_body_bytes = _env_positive_int(MAX_BODY_BYTES_ENV, DEFAULT_MAX_BODY_BYTES)
    max_concurrency = args.max_concurrency
    if max_concurrency is None:
        max_concurrency = _env_positive_int(MAX_CONCURRENCY_ENV, DEFAULT_MAX_CONCURRENCY)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not _flag_given(args, "--inject-usage") and cfg.inject_usage is not None:
        args.inject_usage = cfg.inject_usage
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
        effort_style=args.effort_style,
        effort_mode=args.effort_mode,
        registry=_load_registry(args),
        keys_db=args.keys_db,
        auth_token=auth_token,
        max_body_bytes=max_body_bytes,
        max_concurrency=max_concurrency,
        request_ceiling_usd=args.request_ceiling_usd,
    )
    print(
        f"tamias serve: {args.router_mode} mode, "
        f"{scheme}://{args.host}:{args.port}{proxy_path()} -> {args.upstream}{proxy_path()}",
        file=sys.stderr,
    )
    print(f"tamias serve: request log {args.db}, prices {args.prices}", file=sys.stderr)
    if tls_cert:
        print(
            f"tamias serve: TLS is ON (cert {tls_cert}); clients must use {scheme}://",
            file=sys.stderr,
        )
    if auth_token is not None:
        print(
            "tamias serve: bearer auth is ON: clients must send "
            "Authorization: Bearer <token>",
            file=sys.stderr,
        )
    print(
        f"tamias serve: request body cap {_fmt_bytes(max_body_bytes)}, "
        f"max concurrent requests {max_concurrency}",
        file=sys.stderr,
    )
    if args.keys_db:
        print(
            "tamias serve: company-key auth is ON: clients must send "
            "Authorization: Bearer <vk-...> and may reach only the models "
            f"their key lists (store {args.keys_db})",
            file=sys.stderr,
        )
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
    _warn_if_exposed(args.host)
    ssl_kwargs = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key} if tls_cert else {}
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", **ssl_kwargs)
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


def _env_positive_int(name: str, default: int) -> int | None:
    """An int from env ``name`` in service-default units, or ``None`` for 0/absent."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def _fmt_bytes(size: int | None) -> str:
    if size is None:
        return "unlimited"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):g} MiB"
    return f"{size} bytes"


def prune(args: argparse.Namespace) -> int:
    """Delete old rows from a request log; ``--dry-run`` only reports."""
    store = Store(args.db)
    try:
        result = store.prune(
            keep_days=args.keep_days,
            keep_rows=args.keep_rows,
            dry_run=args.dry_run,
        )
    finally:
        store.close()
    verb = "would delete" if args.dry_run else "deleted"
    print(
        f"tamias prune: {verb} {result['deleted']} rows; "
        f"{result['remaining']} remain ({args.db})"
    )
    return 0


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

    if db.exists():
        size_mb = db.stat().st_size / (1024 * 1024)
        if size_mb > DOCTOR_DB_SIZE_WARN_MB:
            print(
                f"WARN DB size: {db} is {size_mb:.1f} MB (over {DOCTOR_DB_SIZE_WARN_MB} MB); "
                f"run `tamias prune --db {db} --keep-days 90`"
            )
        else:
            print(f"OK DB size: {db} is {size_mb:.1f} MB")
    else:
        print(f"OK DB: {db} not present yet (created on first request)")

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
        effort_policy=args.effort_mode != "off",
        cheap_model=args.cheap_model or config.cheap_model,
        strong_model=args.strong_model or config.strong_model,
        min_gap=args.min_gap if args.min_gap is not None else config.min_gap,
    )


def _load_registry(args: argparse.Namespace) -> Registry:
    """The registry named by ``--registry``, else the default path.

    An explicitly named file must exist (a typo should not silently disable
    apply mode); the default path may be absent, which yields an empty
    registry - and apply mode then sends no effort, by design.
    """
    named = getattr(args, "registry", None)
    if named:
        return load_registry(named)
    return load_registry(default_registry_path(), missing_ok=True)


def _resolve_effort_mode(args: argparse.Namespace, cfg: Config) -> str:
    """Precedence: --effort-mode > config file > the built-in default."""
    if _flag_given(args, "--effort-mode"):
        mode: object = args.effort_mode
    elif cfg.effort_mode is not None:
        mode = cfg.effort_mode
    else:
        mode = args.effort_mode
    if mode not in EFFORT_MODES:
        allowed = ", ".join(EFFORT_MODES)
        raise ConfigError(f"effort_mode {mode!r} is not one of: {allowed}")
    return str(mode)


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

    cfg = _load_effective_config(args)
    prices_path = _resolve_prices_path(pick(args.prices, cfg.prices, None))
    if prices_path is None:
        target = pick(args.prices, cfg.prices, None) or f"./{DEFAULT_PRICES_NAME}"
        print(f"no price sheet found; run: tamias prices fetch --out {target}", file=sys.stderr)
        return 2
    upstream = _resolve_upstream(args, cfg) or DEFAULT_UPSTREAM
    db_override = pick(args.db, cfg.db, None)

    try:
        db = _reserve_run_db(db_override, args.project)
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
        "--cheap-model",
        args.cheap_model,
        "--strong-model",
        args.strong_model,
    ]
    if args.mode:
        serve_command.extend(("--router-mode", args.mode))
    if args.router_config:
        serve_command.extend(("--router-config", args.router_config))
    if args.effort_mode:
        serve_command.extend(("--effort-mode", args.effort_mode))
    if args.registry:
        serve_command.extend(("--registry", args.registry))
    if args.keys_db:
        serve_command.extend(("--keys-db", args.keys_db))
    if getattr(args, "request_ceiling_usd", None) is not None:
        serve_command.extend(("--request-ceiling-usd", args.request_ceiling_usd))

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
    parser.add_argument(
        "--version",
        action="version",
        version=f"tamias {package_version()}",
        help="print the tamias version and exit",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="TOML config file (default: $TAMIAS_CONFIG, else ~/.tamias/config.toml)",
    )
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
        default=None,
        help="base URL of the OpenAI-compatible upstream, without /v1/chat/completions "
        f"(default: ${UPSTREAM_ENV_VAR}, else the config/provider preset)",
    )
    serve_parser.add_argument(
        "--provider",
        choices=preset_names(),
        default=None,
        help="named upstream preset, used when --upstream/upstream is not set "
        f"(one of: {', '.join(preset_names())})",
    )
    serve_parser.add_argument("--router-profile", choices=("generic", "legacy"), default="generic")
    serve_parser.add_argument("--router-config", help="TOML router configuration path")
    serve_parser.add_argument(
        "--prices", default=None, help="path to the TOML price sheet"
    )
    serve_parser.add_argument("--db", default=None, help="path to the sqlite request log")
    serve_parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    serve_parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    serve_parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="acknowledge the missing authentication and bind beyond loopback",
    )
    serve_parser.add_argument(
        "--router-mode",
        choices=ROUTER_MODES,
        default="shadow",
        help="shadow records the routing decision; off disables routing (default: shadow)",
    )
    serve_parser.add_argument(
        "--effort-mode",
        choices=EFFORT_MODES,
        default="off",
        help="off computes nothing; meter records the effort the policy would use; "
        "apply also sends it, but only for a model --registry vouches for "
        "(default: off)",
    )
    serve_parser.add_argument(
        "--request-ceiling-usd",
        default=None,
        help="USD reserved for one request before it is forwarded",
    )
    serve_parser.add_argument(
        "--registry",
        default=None,
        help="TOML capability catalogue; apply mode sends an effort only for a "
        "model it lists (default: $TAMIAS_REGISTRY or ~/.tamias/models.toml)",
    )
    serve_parser.add_argument(
        "--keys-db",
        default=None,
        help="enable company-key authentication from this SQLite key store; "
        "clients must send a vk- key and may reach only the models it lists "
        "(default: off, which leaves --auth-token or no auth)",
    )
    serve_parser.add_argument(
        "--effort-style",
        choices=("openrouter", "openai"),
        default="openrouter",
        help="request-field style used when the effort policy is active (default: openrouter)",
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
    serve_parser.add_argument(
        "--auth-token",
        default=None,
        help=f"bearer token clients must send as Authorization: Bearer <token> "
        f"(default: ${AUTH_TOKEN_ENV}; a non-loopback --host needs this or "
        f"--keys-db)",
    )
    serve_parser.add_argument(
        "--tls-cert",
        default=None,
        help="PEM certificate chain; with --tls-key, serve HTTPS instead of HTTP",
    )
    serve_parser.add_argument(
        "--tls-key",
        default=None,
        help="PEM private key matching --tls-cert",
    )
    serve_parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=None,
        help=f"reject larger request bodies with 413 (default: {DEFAULT_MAX_BODY_BYTES} bytes, "
        f"overridable via ${MAX_BODY_BYTES_ENV}; 0 disables the cap)",
    )
    serve_parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help=f"limit concurrent in-flight requests (default: {DEFAULT_MAX_CONCURRENCY}, "
        f"overridable via ${MAX_CONCURRENCY_ENV}; 0 disables the limit)",
    )

    run_parser = sub.add_parser("run", help="run one command through a short-lived proxy")
    run_parser.add_argument("--mode", choices=ROUTER_MODES, default=None)
    run_parser.add_argument("--effort-mode", choices=EFFORT_MODES, default=None)
    run_parser.add_argument("--registry", default=None)
    run_parser.add_argument("--keys-db", default=None)
    run_parser.add_argument("--request-ceiling-usd", default=None)
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
        f"(default: ${UPSTREAM_ENV_VAR} if set, else the config/provider preset, "
        f"else {DEFAULT_UPSTREAM})",
    )
    run_parser.add_argument(
        "--provider",
        choices=preset_names(),
        default=None,
        help="named upstream preset, used when --upstream/upstream is not set "
        f"(one of: {', '.join(preset_names())})",
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
    dashboard_parser.add_argument("--port", type=int, default=8010, help="port to listen on")
    dashboard_parser.add_argument("--compare-db", help="optional baseline database to compare")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    dashboard_parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="acknowledge the missing authentication and bind beyond loopback",
    )
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

    prune_parser = sub.add_parser("prune", help="delete old rows from the request log")
    prune_parser.add_argument("--db", required=True, help="path to the proxy sqlite log")
    keep = prune_parser.add_mutually_exclusive_group(required=True)
    keep.add_argument(
        "--keep-days", type=int, help="delete rows older than this many days"
    )
    keep.add_argument(
        "--keep-rows", type=int, help="keep only the most recent this many rows"
    )
    prune_parser.add_argument(
        "--dry-run", action="store_true", help="print what would be deleted without deleting"
    )

    admin_parser = sub.add_parser("admin", help="manage company API keys")
    admin_sub = admin_parser.add_subparsers(dest="admin_command", required=True)
    key_parser = admin_sub.add_parser("key", help="issue, list or revoke company keys")
    key_sub = key_parser.add_subparsers(dest="key_command", required=True)
    issue_parser = key_sub.add_parser(
        "issue", help="mint a key and print its token once"
    )
    issue_parser.add_argument(
        "--keys-db", default=_default_keys_db(), help="path to the SQLite key store"
    )
    issue_parser.add_argument("--label", default="", help="who or what this key is for")
    issue_parser.add_argument(
        "--project", default=None, help="project the key's spend belongs to"
    )
    issue_parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="a model this key may use; repeat for several (default: any model)",
    )
    issue_parser.add_argument("--budget-usd", default=None)
    issue_parser.add_argument("--period-days", type=int, default=None)
    issue_parser.add_argument("--rpm", type=int, default=None)
    issue_parser.add_argument("--tpm", type=int, default=None)
    issue_parser.add_argument(
        "--model-cap",
        action="append",
        default=[],
        metavar="MODEL=USD",
        help="a per-model USD sub-cap; repeat for several",
    )
    list_parser = key_sub.add_parser("list", help="list keys, never their secrets")
    list_parser.add_argument("--keys-db", default=_default_keys_db())
    revoke_parser = key_sub.add_parser("revoke", help="revoke a key by its key_id")
    revoke_parser.add_argument("key_id")
    revoke_parser.add_argument("--keys-db", default=_default_keys_db())

    config_parser = sub.add_parser("config", help="show the effective configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="print the config file path that would be read")
    config_sub.add_parser("show", help="print the values loaded from the config file")
    config_sub.add_parser("providers", help="list the built-in upstream presets")
    return parser


def package_version() -> str:
    """The installed distribution version, falling back to ``__version__``.

    Source runs (and the test suite) may import ``tamias`` without the
    distribution being installed; in that case the metadata lookup raises and
    the single source of truth is ``tamias.__version__``.
    """
    try:
        return importlib.metadata.version("tamias")
    except importlib.metadata.PackageNotFoundError:
        from tamias import __version__ as source_version

        return source_version


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    args = build_parser().parse_args(raw_argv)
    args._argv = raw_argv
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
            + (["--compare-db", args.compare_db] if args.compare_db else [])
            + (["--allow-non-loopback"] if args.allow_non_loopback else [])
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
    if args.command == "prune":
        return prune(args)
    if args.command == "config":
        return config_command(args)
    if args.command == "admin":
        try:
            return admin_command(args)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            print(f"tamias admin: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
