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
import importlib.resources
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from tamias import discovery as discovery_mod
from tamias import manifest as manifest_mod
from tamias import misroute as misroute_mod
from tamias import reconcile as reconcile_mod
from tamias import verdict as verdict_mod
from tamias.catalog import (
    MODELS_DEV_URL,
    build_zen_registry,
    fetch_catalog,
    render_registry,
)
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
from tamias.limits import ledger_for
from tamias.pricing import (
    PriceSheet,
    assume_cache_warm,
    can_price,
    compute_cost,
    load_effective_sheet,
    overlay_path,
)
from tamias.pricing_fetch import (
    OPENROUTER_MODELS_URL,
    ZEN_MODELS_URL,
    fetch_model_ids,
    fetch_models,
    render_sheet,
    render_zen_sheet,
    zen_free_models,
)
from tamias.providers import (
    ProviderError,
    catalogue_from_registry,
    default_providers_path,
    load_providers,
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

ESTIMATE_LABEL = "estimate; cache-aware, can be negative; not measured"
REALISED_LABEL = (
    "baseline is list-price, actual billed where recorded; "
    "cache-aware, can be negative"
)
ROUTER_MODES = ("shadow", "off", "active")
SIMULATED_LABEL = "SIMULATED PRICES, NOT REAL SAVINGS"
RUN_START_TIMEOUT_SECONDS = 15
RUN_STOP_TIMEOUT_SECONDS = 5
DOCTOR_ONLINE_TIMEOUT_SECONDS = 2.0
PRICE_SHEET_STALE_DAYS = 30
# Base URL of an OpenAI-compatible upstream, given without /v1/chat/completions,
# so `run` can default to a real provider when the flag is omitted.
DEFAULT_UPSTREAM = "https://openrouter.ai/api"
DEFAULT_PRICES_NAME = "prices.toml"
#: The price sheet shipped inside the package, used only when neither
#: ./prices.toml nor ~/.tamias/prices.toml exists.  Without it a fresh install
#: could not price a request at all, and the repo's demo sheets price only
#: free models -- so a first run either refused to start or printed $0.00, and
#: neither is distinguishable from a broken proxy (F-20).
BUNDLED_PRICES_PARTS = ("data", "prices.default.toml")
UPSTREAM_ENV_VAR = "TAMIAS_UPSTREAM"

# Serve hardening (G1/G2/G3). All are opt-in knobs: the token protects a
# non-loopback bind, the body cap and concurrency limit bound a runaway agent,
# and doctor's size check nudges toward `tamias prune --keep-days`.
AUTH_TOKEN_ENV = "TAMIAS_AUTH_TOKEN"
AUTH_TOKEN_FILE_ENV = "TAMIAS_AUTH_TOKEN_FILE"
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
DECISION_TARGET_COLUMNS = ("decision_target_model",)
MODEL_USED_COLUMNS = ("model_used", "model")


def _warn_if_exposed(host: str, *, authenticated: bool = False) -> None:
    """Warn when a service is bound beyond loopback.

    ``authenticated`` reflects whether a token or company keys are configured,
    so the warning never claims the service is unauthenticated when it is not.
    """
    if host not in ("127.0.0.1", "localhost", "::1") and not host.startswith("127."):
        posture = (
            "it is authenticated but reachable beyond loopback"
            if authenticated
            else "this service has no authentication"
        )
        print(
            f"tamias: WARNING — binding to {host!r}; {posture}, keep it on "
            f"loopback unless you are on a trusted network",
            file=sys.stderr,
        )


def _money(value: float) -> str:
    """One amount, formatted the way the OpenCode pop-up formats it.

    The report and ``/tamias`` are read side by side, so they must render the
    same number identically.  The rules are shared by both: an exact zero, a
    floor that says "less than" rather than inventing digits below it, six
    decimals under a dollar, four above -- ``$4.9000`` and ``$4.90`` are
    different claims when the figure is an estimate.
    """
    if value == 0:
        return "$0.00"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude < 0.0001:
        return f"{sign}<$0.0001"
    if magnitude < 1:
        return f"{sign}${magnitude:.6f}"
    return f"{sign}${magnitude:.4f}"


def _money_inr(value: float, rate: float) -> str:
    """The same amount in rupees, at a caller-supplied fixed rate.

    This is a *conversion display*, not a bill: nothing here knows the rate the
    bank or provider used, so the rupee figure is an illustration of the dollar
    amount and the dollar amount stays authoritative.  Whole rupees are shown
    because a converted number has no business implying paise of precision.
    """
    rupees = value * rate
    sign = "-" if rupees < 0 else ""
    return f"{sign}\u20b9{abs(rupees):,.0f}"


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


def _billed_cost(row: dict[str, Any]) -> float | None:
    """The provider-reported cost for this row, or None when it did not report one.

    This is the number the provider said it billed, kept separate from the
    list-price computation.  A zero is a real zero; a missing value is never
    turned into one.
    """
    raw = row.get("provider_cost_usd")
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


def _sheet_identity(source: str | Path) -> str:
    """The name a price sheet is known by, independent of how it was spelled.

    A sheet's identity is its **name**, not the path that happened to be typed
    when it was used.  ``./prices.toml``, ``prices.toml`` and an absolute path
    are the same sheet, and treating them as different ones blanks a saving
    that is in fact computable.
    """
    return Path(str(source)).name


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


def _logged_target(row: dict[str, Any]) -> str | None:
    """The model the router's own decision named as the switch target.

    The proxy persists ``decision_target_model`` on every SWITCH row, so this is
    the deployment's *configured* cheap target -- the model the router would
    actually have picked -- rather than a guess.  It is absent on STAY rows and
    on logs written before the column existed.
    """
    raw = _pick(row, DECISION_TARGET_COLUMNS)
    return raw if isinstance(raw, str) and raw else None


def _applied_switch(row: dict[str, Any]) -> bool:
    """Whether this row actually ran on a different model than the caller asked for.

    An active-mode row is proof enough on its own: the proxy only rewrites the
    model in that mode.  For older logs the recorded target is the only evidence
    left, so a row whose used model equals its logged target is treated as
    applied too.
    """
    used = _pick(row, MODEL_USED_COLUMNS)
    requested = _pick(row, ("model_requested", "model"))
    if not isinstance(used, str) or not used or used == "-":
        return False
    if not isinstance(requested, str) or not requested or requested == "-":
        return False
    if used == requested:
        return False
    return _pick(row, ("router_mode",)) == "active" or _logged_target(row) == used


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
    """What this request would have cost less, had the switch been acted on.

    The stay arm is priced with a **warm** cache (:func:`assume_cache_warm`);
    the switch arm pays the row's real counts, including the context it has to
    re-ingest.  Charging that context is what makes the estimate falsifiable:
    the result can be zero or negative, so the report can show a switch that
    lost money instead of only ever showing winnings (F-1).

    The two arms depend on whether the switch was applied:

    * **applied** -- the caller asked for model A and the row ran on B, so A is
      the stay arm and B is the switch arm.  This is the saving the proxy
      actually produced, and it is the same arithmetic as ``realised saving``.
    * **shadow or declined** -- the row ran on A, so A is the stay arm and the
      router's target is the switch arm.  The target is the model the decision
      named (:func:`_logged_target`), falling back to the sheet's cheapest only
      for rows that predate that column.

    Before this distinction, an active-mode row whose used model *was* the
    target compared the target with itself and reported zero, so a successful
    routing run could not show the money it saved.
    """
    used = _pick(row, MODEL_USED_COLUMNS)
    if not isinstance(used, str) or not used:
        return None

    applied = _applied_switch(row)
    if applied:
        stay = _pick(row, ("model_requested", "model"))
        switch = used
    else:
        if cheap is None:
            return None
        stay = used
        switch = cheap

    if not isinstance(stay, str) or not stay or not isinstance(switch, str) or not switch:
        return None
    usage = _usage(row)
    staying = compute_cost(stay, assume_cache_warm(usage), sheet).usd
    switching = compute_cost(switch, usage, sheet).usd
    if staying is None or switching is None:
        return None
    if stay == switch:
        # Already on the model a switch would choose: there is no switch to
        # price, and comparing a warm price against a cold one for the same
        # model would invent a loss out of a no-op.
        return 0.0
    return staying - switching


def _print_target_line(
    cheap: str | None, logged: set[str], fallback_rows: int
) -> None:
    """Say which model the savings estimate priced its switch arm against.

    The switch target differs by row: rows the router decided on carry the
    configured target in ``decision_target_model``, while older rows (or logs
    with no decision) fall back to the cheapest model in the passed price sheet,
    which need not be the deployment's cheap model.  The line names the logged
    target when there is one -- the model the router would actually have picked
    -- and only discloses the sheet-cheapest fallback when rows depended on it.
    """
    if logged:
        names = ", ".join(sorted(logged))
        line = f"switch target (from router decisions): {names}"
        if fallback_rows:
            line += (
                f"; {fallback_rows} switched row(s) had no logged target and "
                f"were priced against the sheet-cheapest {cheap or 'unknown'}"
            )
        print(line)
    elif fallback_rows:
        print(
            f"switch target: unknown for {fallback_rows} switched row(s) with no "
            f"router decision; savings priced against the sheet-cheapest "
            f"{cheap or 'unknown'}"
        )
    else:
        print("switch target: not recorded yet (no routed request in this log)")


def report(
    db_path: str,
    prices_path: str,
    inr_rate: float | None = None,
    *,
    routing_verdict: bool = False,
) -> None:
    """Print the cost and shadow-routing summary for one database.

    Every line that quotes an amount carries a banner when the price sheet
    declares itself simulated, so a made-up rate cannot be read as a real bill.

    When ``inr_rate`` is given (rupees per US dollar) every dollar figure is
    followed by a rupee conversion in brackets.  The rate is the caller's, not
    the bank's, so the rupee amount is a display of the dollar amount and the
    dollar amount remains the one that reconciles against a provider invoice.
    """
    sheet = load_effective_sheet(prices_path)
    rows = _read_rows(db_path)
    cheap = _cheap_model(sheet)

    def money(value: float) -> str:
        text = _money(value)
        if inr_rate is not None:
            text += f" (\u2248 {_money_inr(value, inr_rate)})"
        return text

    known_total = 0.0
    unknown_costs = 0
    switched = 0
    saved = 0.0
    unpriced = 0
    with_counts = 0
    targets_logged: set[str] = set()
    targets_fallback = 0

    free_count = sum(
        1
        for row in rows
        if isinstance(row.get("model_requested"), str) and row["model_requested"].endswith(":free")
    )

    # The token ledger is the price-independent measurement.  Token counts are
    # exact even when no rate exists, so they are always shown: when a dollar
    # figure is UNKNOWN the token counts are the honest parameter that remains.
    token_input = 0
    token_output = 0
    token_cached = 0
    token_rows = 0
    zero_input = 0
    zero_output = 0
    unknown_input = 0
    unknown_output = 0
    diverted_input = 0
    diverted_output = 0
    diverted_rows = 0

    realised_saved = 0.0
    realised_count = 0
    realised_billed = 0
    realised_unpriced = 0
    # Switches the proxy actually applied: a SWITCH decision whose row then
    # ran on a different model than the one requested.  ``switched`` counts
    # the decisions, so the difference is the switches active mode declined
    # to apply -- a restricted key, or a target the deployment forbade.
    routed = 0

    for row in rows:
        if _has_counts(row):
            with_counts += 1
        cost = _stored_cost(row)
        if cost is None:
            unknown_costs += 1
        else:
            known_total += cost
        row_input = _count(row, "input_tokens")
        row_output = _count(row, "output_tokens")
        if row_input is not None or row_output is not None:
            token_rows += 1
            token_input += row_input or 0
            token_output += row_output or 0
            token_cached += _count(row, "cached_input_tokens") or 0
            if cost is None:
                unknown_input += row_input or 0
                unknown_output += row_output or 0
            elif cost == 0:
                zero_input += row_input or 0
                zero_output += row_output or 0
            used = row.get("model_used")
            requested = row.get("model_requested")
            if (
                isinstance(used, str)
                and isinstance(requested, str)
                and used
                and requested
                and used != requested
            ):
                diverted_rows += 1
                diverted_input += row_input or 0
                diverted_output += row_output or 0
        if not _would_switch(row):
            pass
        else:
            switched += 1
            applied = _applied_switch(row)
            target = _logged_target(row)
            if applied:
                # The model that actually ran is the switch arm, whether or not
                # the decision named it.  It belongs in the target line because
                # it is what the saving was priced against.
                used_model = _pick(row, MODEL_USED_COLUMNS)
                named = target or (used_model if isinstance(used_model, str) else None)
                if named:
                    targets_logged.add(named)
            elif target is None:
                targets_fallback += 1
                target = cheap
            else:
                targets_logged.add(target)
            delta = _saving(row, sheet, target)
            if delta is None:
                unpriced += 1
            else:
                saved += delta

        model_used = _pick(row, MODEL_USED_COLUMNS)
        model_requested = _pick(row, ("model_requested", "model"))
        if model_used is not None and model_requested is not None and model_used != model_requested:
            if _would_switch(row):
                routed += 1
            usage = _usage(row)
            cost_requested = compute_cost(
                model_requested, assume_cache_warm(usage), sheet
            ).usd
            billed_used = _billed_cost(row)
            if billed_used is not None:
                # The provider's own number is the actual cost of the model
                # that ran.  Prefer it over list-price arithmetic; the
                # requested side remains a list-price counterfactual.
                cost_used = billed_used
                realised_billed += 1
            else:
                cost_used = compute_cost(model_used, usage, sheet).usd
            if cost_requested is not None and cost_used is not None:
                # Cache-aware and unclamped: a switch that re-ingested more than
                # it saved shows as a negative "realised saving" (F-1).
                realised_saved += cost_requested - cost_used
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
    passthrough = sum(
        1 for row in rows if isinstance(row, dict) and row.get("route") == "passthrough"
    )
    if passthrough:
        print(
            f"passthrough requests (count-only, never priced): {passthrough}"
        )
    if token_rows:
        print(
            f"tokens: {token_input:,} input / {token_output:,} output / "
            f"{token_cached:,} cached "
            f"({token_rows} of {len(rows)} requests report counts)"
        )
        if zero_input or zero_output:
            print(
                f"tokens on $0-priced models: {zero_input:,} input / "
                f"{zero_output:,} output"
            )
        if unknown_input or unknown_output:
            print(
                f"tokens with UNKNOWN cost: {unknown_input:,} input / "
                f"{unknown_output:,} output (token counts are exact; "
                "no dollar figure was computed)"
            )
        if diverted_rows:
            print(
                f"tokens routed to a different model: {diverted_input:,} input / "
                f"{diverted_output:,} output ({diverted_rows} of {len(rows)} requests)"
            )
    # Determine provenance qualifier for total cost
    stored_rows_simulated = any(flag is True for flag in stored_flags)
    is_legacy = unknown_provenance > 0 or (
        bool(priced_rows) and all(flag is None for flag in stored_flags)
    )
    if unknown_costs:
        if stored_rows_simulated:
            print(
                f"total cost: UNKNOWN "
                f"(known part: {money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; SIMULATED prices)"
            )
        elif is_legacy:
            print(
                f"total cost: UNKNOWN "
                f"(known part: {money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; price provenance unknown)"
            )
        else:
            stored_sheet_str = ", ".join(stored_sheets) if stored_sheets else str(prices_path)
            print(
                f"total cost: UNKNOWN "
                f"(known part: {money(known_total)}; "
                f"{unknown_costs} of {len(rows)} requests have unknown cost) "
                f"(computed, not billed; list prices from {stored_sheet_str})"
            )
    else:
        if stored_rows_simulated:
            print(f"total cost (computed, not billed; SIMULATED prices): {money(known_total)}")
        elif is_legacy:
            print(
                "total cost (computed, not billed; price provenance unknown): "
                f"{money(known_total)}"
            )
        else:
            stored_sheet_str = ", ".join(stored_sheets) if stored_sheets else str(prices_path)
            print(
                "total cost (computed, not billed; list prices from "
                f"{stored_sheet_str}): {money(known_total)}"
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
        # The decisions were meant to be applied, but only the rows whose
        # model actually changed prove it; the rest were held back.
        line = f"requests routed to the cheap model: {routed}"
        suppressed = switched - routed
        if suppressed > 0:
            line += (
                f" ({suppressed} more decided SWITCH and were not applied: "
                "the caller's key may not use the target)"
            )
        print(line)
    elif router_modes == {"off"}:
        print("router was off: no decision was recorded")
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

    fail_open = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("failure_stage") or row.get("failure_type"):
            fail_open += 1
    if fail_open:
        print(
            f"receipt incomplete: {fail_open} of {len(rows)} requests failed "
            "upstream or streamed badly (fail-open events logged)"
        )

    if billed_values:
        print(
            f"spent (billed by OpenRouter): {money(sum(billed_values))} over "
            f"{len(billed_values)} of {len(rows)} requests"
        )
    else:
        print("billed cost: UNKNOWN")
    if reconciled:
        computed_total = sum(computed for computed, _billed in reconciled)
        billed_total = sum(billed for _computed, billed in reconciled)
        stored_label = ", ".join(stored_sheets) if stored_sheets else "provenance unknown"
        print(
            f"computed cost: {money(computed_total)} (computed from list prices "
            f"({stored_label})) over {len(reconciled)} rows{suffix}"
        )
        print(
            f"difference (billed - computed): {money(billed_total - computed_total)} over "
            f"{len(reconciled)} rows{suffix}"
        )

    if unknown_provenance:
        print(
            f"price provenance: provenance unknown "
            f"({unknown_provenance} of {len(priced_rows)} priced rows)"
        )
    elif stored_sheets:
        print(f"price provenance: {', '.join(stored_sheets)}")
    # Compare against the sheet that actually priced the rows: with an overlay
    # that is ``base + overlay``, not the generated file named on the command
    # line, or every overlay run would warn about itself.
    requested_name = _sheet_identity(sheet.source or prices_path)
    stored_names = {_sheet_identity(name) for name in stored_sheets}
    if stored_sheets and requested_name not in stored_names:
        print(
            f"WARNING: requested price sheet {prices_path} differs from stored price sheet "
            f"{', '.join(stored_sheets)}"
        )
    # The name can match while the sheet behind it does not.  Every row
    # records the date of the sheet that priced it, and that date is already
    # stored -- so it is compared here rather than only the name.  A sheet
    # whose date differs from the recorded one is a different price basis, and
    # the saving it would produce is not this log's saving.
    recorded_dates = sorted(
        {
            str(row.get("price_sheet_date"))
            for row in priced_rows
            if row.get("price_sheet_date")
        }
    )
    sheet_date = getattr(sheet, "date", None)
    if recorded_dates and sheet_date and str(sheet_date) not in recorded_dates:
        print(
            f"WARNING: requested price sheet {prices_path} is dated {sheet_date}, but this "
            f"log was priced with {', '.join(recorded_dates)}; its figures are a "
            "different price basis"
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
        and requested_name not in stored_names
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
        line = f"estimated saving: {money(saved)} ({ESTIMATE_LABEL}"
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
                f"{money(realised_saved)} over {realised_count} rows "
                f"({REALISED_LABEL}"
            )
            if realised_billed:
                line1 += (
                    f"; {realised_billed} of {realised_count} actual costs "
                    "came from the provider bill"
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
                f"rows ({REALISED_LABEL}){suffix}"
            )
    else:
        line1 = ""

    print(line1)
    _print_target_line(cheap, targets_logged, targets_fallback)
    print(
        "sheet passed on the command line (used only for savings estimates): "
        f"{prices_path} ({sheet.date})"
    )
    if inr_rate is not None:
        print(
            f"rupee figures are a display conversion at a fixed "
            f"\u20b9{inr_rate:g}/$ you gave, not a billed amount"
        )
    if routing_verdict:
        # What happened on the turn after each downgrade. Read-only: the reasons
        # are already in the log, so this needs no column and no request text.
        for line in verdict_mod.downgrade_outcomes(rows).lines():
            print(line)


def _model_pool(value: str) -> tuple[str, ...]:
    """Split a comma-separated model list into an ordered, de-duplicated pool.

    A single value is the ordinary one-target configuration.  The first entry is
    the primary, so existing scripts that pass one model keep the same target.
    """
    seen: set[str] = set()
    pool: list[str] = []
    for item in value.split(","):
        model = item.strip()
        if model and model not in seen:
            seen.add(model)
            pool.append(model)
    return tuple(pool)


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
    manifest: dict[str, Any] | None = None,
    manifest_key: object = None,
    trusted_hosts: object = None,
    enforce_host: bool = False,
    providers: object = None,
    limits: object = None,
    misroute: object = None,
) -> Any:
    """Build the proxy ASGI app for ``tamias serve``, without starting a server.

    Kept separate from :func:`serve` so the wiring (price sheet, sqlite log,
    router config) can be exercised without binding a port.  ``transport`` is the
    httpx transport the upstream is reached through; ``serve`` leaves it as
    ``None`` for a real socket, and a test passes the suite's in-process one.
    """
    from tamias import proxy

    sheet = load_effective_sheet(prices)
    store = Store(db)
    key_store = KeyStore(keys_db) if keys_db else None
    pool = _model_pool(cheap_model)
    config = router_config or RouterConfig(
        easy_tools=EASY_TOOLS,
        cheap_model=pool[0] if pool else "",
        cheap_pool=pool if len(pool) > 1 else (),
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
        manifest=manifest,
        manifest_key=manifest_key,
        trusted_hosts=trusted_hosts,
        enforce_host=enforce_host,
        providers=providers,
        limits=limits,
        misroute=misroute,
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


def resolve_auth_token(args: argparse.Namespace) -> str | None:
    """The shared bearer token, from ``--auth-token``, env, or a token file.

    Precedence is ``--auth-token`` > ``$TAMIAS_AUTH_TOKEN`` >
    ``$TAMIAS_AUTH_TOKEN_FILE``.  The file form exists because a token on argv
    is visible in ``ps``, shell history and CI logs; pointing at a ``chmod 600``
    file keeps it out of all three.  The file's trailing newline is stripped;
    an empty or unreadable file is treated as no token so the caller still gets
    the honest "no authentication" refusal rather than a silent empty secret.
    """
    token = args.auth_token or os.environ.get(AUTH_TOKEN_ENV)
    if token:
        return token
    path = os.environ.get(AUTH_TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"tamias: could not read ${AUTH_TOKEN_FILE_ENV} ({path}): {exc}",
            file=sys.stderr,
        )
        return None
    token = content.strip()
    return token or None


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
    """``tamias admin key ...`` and ``tamias admin manifest ...``.

    The key token is printed here and nowhere else: only its SHA-256 hash is
    stored, so no later command can recover it, and ``list`` never shows it.
    Manifest subcommands are dispatched to :func:`manifest_command`.
    """
    if getattr(args, "admin_command", None) == "manifest":
        return manifest_command(args)
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


def _load_policy_toml(path: str) -> dict[str, Any]:
    """Read a company policy TOML into the dict a manifest carries."""
    import tomllib

    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read policy {path}: {exc}") from exc


def manifest_command(args: argparse.Namespace) -> int:
    """``tamias admin manifest keygen|sign|verify``.

    ``keygen`` writes the signing keypair; ``sign`` turns a policy TOML into a
    signed manifest; ``verify`` checks one without the private key, which is
    exactly what the gateway does before it will serve the policy.
    """
    command = args.manifest_command
    if command == "keygen":
        private, public_raw = manifest_mod.generate_keypair()
        private_path = Path(args.private_out)
        public_path = Path(args.public_out)
        private_path.write_text(manifest_mod.private_key_pem(private), encoding="utf-8")
        private_path.chmod(0o600)
        public_path.write_text(
            manifest_mod.public_key_pem(public_raw), encoding="utf-8"
        )
        print(f"private: {private_path}")
        print(f"public:  {public_path}")
        print(
            "Keep the private key off the gateway; it needs only the public key.",
            file=sys.stderr,
        )
        return 0
    if command == "sign":
        policy = _load_policy_toml(args.policy)
        private = manifest_mod.load_private_key(args.key)
        signed = manifest_mod.sign_manifest(
            policy,
            args.project,
            private,
            expires=args.expires,
            issued_at=args.issued_at,
        )
        text = manifest_mod.dump_manifest(signed, args.out)
        if args.out is None:
            print(text, end="")
        else:
            print(f"wrote {args.out}")
        print(f"project: {signed['project'] or '-'}")
        print(f"expires: {signed['expires']}")
        print(f"use_case: {manifest_mod.policy_use_case(policy) or '-'}")
        return 0
    if command == "verify":
        document = manifest_mod.load_manifest(args.manifest)
        trusted = manifest_mod.load_public_key(args.key) if args.key else None
        verified = manifest_mod.verify_manifest(
            document, expected_project=args.project, trusted_public_key=trusted
        )
        print(
            f"ok: project={verified['project'] or '-'} "
            f"expires={verified['expires']} "
            f"use_case={manifest_mod.policy_use_case(verified['policy']) or '-'}"
        )
        return 0
    return 2


def serve(args: argparse.Namespace) -> int:
    """Run the proxy under uvicorn until interrupted."""
    import uvicorn

    from tamias.security import is_loopback, refuse_non_loopback

    cfg = _load_effective_config(args)
    # Providers first: a catalogue makes a single --upstream optional, because
    # each model then names the provider that serves it.
    explicit_providers = pick(getattr(args, "providers", None), cfg.providers, None)
    try:
        catalogue = load_providers(explicit_providers, missing_ok=not explicit_providers)
    except ProviderError as exc:
        print(f"tamias serve: {exc}", file=sys.stderr)
        return 2
    upstream = _resolve_upstream(args, cfg)
    if not upstream and not len(catalogue):
        print(
            "tamias serve: an upstream is required; pass --upstream, "
            f"set ${UPSTREAM_ENV_VAR}, set upstream/provider in the config file, or "
            "write ~/.tamias/providers.toml to route across providers",
            file=sys.stderr,
        )
        return 2
    args.upstream = upstream or "http://127.0.0.1:0/unconfigured"
    args.router_mode = _resolve_router_mode(args, cfg)
    args.effort_mode = _resolve_effort_mode(args, cfg)
    if not _flag_given(args, "--host") and cfg.host is not None:
        args.host = cfg.host
    if not _flag_given(args, "--port") and cfg.port is not None:
        args.port = cfg.port

    # Refuse a wider bind before any server-side work starts.  Authentication
    # is determined first so the refusal message is accurate: naming "no
    # authentication" when a token/keys *were* given would be a false message.
    auth_token = resolve_auth_token(args)
    refused = refuse_non_loopback(
        "serve",
        args.host,
        args.allow_non_loopback,
        authenticated=auth_token is not None or bool(args.keys_db),
    )
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
    notice = _bundled_prices_notice(prices_path)
    if notice is not None:
        print(notice, file=sys.stderr)
    args.prices = str(prices_path)
    args.db = str(pick(args.db, cfg.db, "requests.db"))
    if not _flag_given(args, "--effort-style") and cfg.effort_style is not None:
        args.effort_style = cfg.effort_style
    if not _flag_given(args, "--cheap-model") and cfg.cheap_model is not None:
        args.cheap_model = cfg.cheap_model
    if not _flag_given(args, "--strong-model") and cfg.strong_model is not None:
        args.strong_model = cfg.strong_model

    # Discovery and the limit ledger.  A deployment with no provider file keeps
    # today's single-upstream behaviour exactly.
    registry = _load_registry(args)
    if len(catalogue) and getattr(args, "discover", True):
        report = discovery_mod.discover(catalogue)
        registry = discovery_mod.merge_into(registry, report)
        print(f"tamias serve: discovered {report.summary()}", file=sys.stderr)
    if len(catalogue):
        catalogue = catalogue_from_registry(catalogue, registry)
        missing = catalogue.unconfigured()
        if missing:
            print(
                "tamias serve: no credential for "
                + ", ".join(missing)
                + "; those providers are skipped by routing until it is set",
                file=sys.stderr,
            )
    limits_path = getattr(args, "limits", None)
    if limits_path is None and args.db != ":memory:":
        limits_path = str(Path(args.db).expanduser().parent / "limits.db")
    limit_ledger = ledger_for(limits_path) if len(catalogue) else None

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
    # Managed mode: a signed policy manifest the gateway verifies before it
    # will serve anything, so serving a policy is bound to holding a valid
    # manifest.  A manifest with no authentication would hand the policy to an
    # anonymous caller, so that combination is refused rather than warned.
    manifest_doc: dict[str, Any] | None = None
    manifest_key: bytes | None = None
    if args.manifest:
        if auth_token is None and not args.keys_db:
            print(
                "tamias serve: --manifest needs authentication: pass --auth-token "
                "or --keys-db so the policy is served only to an authenticated "
                "caller.",
                file=sys.stderr,
            )
            return 2
        if not args.manifest_key:
            print(
                "tamias serve: --manifest needs --manifest-key "
                "(a pinned public key) to prevent self-signed manifests",
                file=sys.stderr,
            )
            return 2
        try:
            manifest_doc = manifest_mod.load_manifest(args.manifest)
            manifest_key = manifest_mod.load_public_key(args.manifest_key)
            manifest_mod.verify_manifest(
                manifest_doc, trusted_public_key=manifest_key
            )
        except manifest_mod.ManifestError as exc:
            print(f"tamias serve: refusing to start: {exc}", file=sys.stderr)
            return 2
    elif args.manifest_key:
        print("tamias serve: --manifest-key needs --manifest", file=sys.stderr)
        return 2
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
        registry=registry,
        keys_db=args.keys_db,
        auth_token=auth_token,
        max_body_bytes=max_body_bytes,
        max_concurrency=max_concurrency,
        request_ceiling_usd=args.request_ceiling_usd,
        manifest=manifest_doc,
        manifest_key=manifest_key,
        trusted_hosts=args.trusted_host,
        enforce_host=bool(args.trusted_host) or not is_loopback(args.host),
        providers=catalogue,
        limits=limit_ledger,
        misroute=misroute_mod.MisroutePolicy(
            enabled=bool(args.misroute_penalty),
            escalate_turns=args.misroute_turns,
        ),
    )
    if len(catalogue):
        target = " + ".join(catalogue.names())
    else:
        target = args.upstream
    print(
        f"tamias serve: {args.router_mode} mode, "
        f"{scheme}://{args.host}:{args.port}{proxy_path()} -> {target}",
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
        if args.auth_token:
            # The token is already visible here; the warning exists so the
            # operator learns before the shell history does.
            print(
                "tamias serve: WARNING — the token was passed on the command line, "
                f"which lands in shell history, ps and CI logs. Prefer "
                f"${AUTH_TOKEN_ENV} or a chmod-600 file via ${AUTH_TOKEN_FILE_ENV}.",
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
    if manifest_doc is not None:
        print(
            f"tamias serve: signed policy manifest is ON for project "
            f"{manifest_doc.get('project') or '(any)'}; GET /policy serves it to "
            f"authenticated callers",
            file=sys.stderr,
        )
    _warn_if_exposed(args.host, authenticated=auth_token is not None or bool(args.keys_db))
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


def agent_config(
    agent: str,
    port: int,
    project: str | None,
    models: Sequence[str] = (),
) -> str:
    """Return a paste-only local proxy snippet; this never writes configuration.

    For OpenCode, ``models`` becomes the provider's ``models`` map.  OpenCode
    needs at least one model to offer in its picker; a provider block without
    one is accepted by the config parser but lists nothing.
    """
    prefix = f"/p/{project}" if project else ""
    root = f"http://127.0.0.1:{port}{prefix}"
    if agent == "claude-code":
        return f"export ANTHROPIC_BASE_URL={root}"
    if agent == "codex":
        return f'[model_providers.tamias]\nbase_url = "{root}/v1"\nwire_api = "responses"'
    provider: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "tamias",
        # OpenCode hides a custom provider that has no credential, even though
        # tamias replaces whatever the client sends with the provider's own key.
        # A placeholder keeps the provider visible; it is not a secret.
        "options": {"baseURL": f"{root}/v1", "apiKey": "tamias-local"},
    }
    if models:
        provider["models"] = {model: {"name": model} for model in models}
    return json.dumps(
        {"provider": {"tamias": provider}},
        separators=(",", ":"),
    )


#: Rates a hand-written price overlay may carry.  The same names the generated
#: sheet uses, so one parser reads both.
PRICE_FIELDS = ("input", "output", "cached_input", "cache_write", "cache_write_1h")


def _render_price_overlay(models: dict[str, dict[str, float]]) -> str:
    """Render the overlay as TOML: one model table per id, fixed field order.

    Model ids are written as quoted TOML keys, because most of them carry a
    slash and several carry a colon.
    """
    lines = [
        "# Hand-written price overlay, read after the generated price sheet.",
        "# A model listed here wins over the same id in the generated sheet.",
        "# Use it for a provider the catalogue does not price, a free tier, or a",
        "# local model.  Re-running `tamias prices fetch` never touches this file.",
        "",
    ]
    for model in sorted(models):
        rates = models[model]
        escaped = model.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'["{escaped}"]')
        for field in PRICE_FIELDS:
            if field in rates:
                lines.append(f"{field} = {rates[field]!r}")
        lines.append("")
    return "\n".join(lines)


def prices_set(args: argparse.Namespace) -> int:
    """Write one model's rates into the overlay beside the generated sheet."""
    rates = {
        field: float(getattr(args, field))
        for field in PRICE_FIELDS
        if getattr(args, field) is not None
    }
    if not rates:
        print(
            "tamias prices set: pass at least one rate, e.g. --input 0 --output 0",
            file=sys.stderr,
        )
        return 2
    negative = sorted(field for field, value in rates.items() if value < 0)
    if negative:
        print(
            f"tamias prices set: a rate cannot be negative ({', '.join(negative)})",
            file=sys.stderr,
        )
        return 2

    target = overlay_path(Path(args.sheet).expanduser())
    existing: dict[str, dict[str, float]] = {}
    if target.exists():
        try:
            import tomllib

            document = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"tamias prices set: {exc}", file=sys.stderr)
            return 1
        for model, table in document.items():
            if not isinstance(table, dict):
                continue
            existing[model] = {
                field: float(table[field])
                for field in PRICE_FIELDS
                if isinstance(table.get(field), (int, float))
            }
    existing[args.model] = rates
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_price_overlay(existing), encoding="utf-8")
    except OSError as exc:
        print(f"tamias prices set: {exc}", file=sys.stderr)
        return 1
    written = ", ".join(f"{field}={value:g}" for field, value in rates.items())
    print(f"wrote {target}")
    print(f"{args.model}: {written} USD per 1M tokens")
    return 0


def _strip_jsonc(text: str) -> tuple[str, bool]:
    """Remove ``//`` and ``/* */`` comments and trailing commas outside strings.

    OpenCode's config is JSONC.  Rewriting it needs plain JSON, so comments have
    to go; the caller backs the file up and says so.  String literals are
    tracked so a ``//`` inside a URL or a key is never mistaken for a comment.
    """
    out: list[str] = []
    index = 0
    had_comments = False
    in_string = False
    escaped = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            had_comments = True
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            had_comments = True
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    stripped = "".join(out)
    # A trailing comma before a closing brace or bracket is legal JSONC.
    without_trailing = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return without_trailing, had_comments


def _opencode_config_path() -> Path:
    """The global OpenCode config, preferring the file that already exists."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    directory = root / "opencode"
    for name in ("opencode.jsonc", "opencode.json"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return directory / "opencode.json"


def _opencode_cli_path() -> Path:
    """OpenCode's TUI settings file, which is where plugins are listed."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "opencode" / "cli.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return document


def _backup(path: Path, stamp: str) -> Path:
    """Copy a config aside before rewriting it.

    The copy can contain provider credentials that the config already held.
    ``copy2`` preserves the original mode, so a 0644 config would leave a
    world-readable second copy of a secret; force owner-only instead.
    """
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    try:
        backup.chmod(0o600)
    except OSError:
        pass
    return backup


def _plugin_directory(explicit: str | None) -> Path | None:
    """Where the TUI plugin lives, if this install can find it."""
    candidate = explicit or os.environ.get("TAMIAS_PLUGIN_DIR")
    if candidate:
        path = Path(candidate).expanduser()
        return path if path.is_dir() else None
    # A wheel install carries the pop-up inside the package.  This is checked
    # before the checkout search because an editable install has no
    # ``tamias/plugin`` and must keep using the sibling directory.
    packaged = Path(__file__).resolve().parent / "plugin"
    if (packaged / "tui.tsx").is_file():
        return packaged
    # Running from a source checkout: the plugin sits next to pyproject.toml.
    for parent in Path(__file__).resolve().parents:
        sibling = parent / "tamias-opencode-plugin"
        if (sibling / "tui.tsx").is_file():
            return sibling
    return None


#: The runtime peers the host must be able to resolve *next to the plugin
#: directory*.  The wheel ships TypeScript only; OpenCode resolves a plugin's
#: imports by walking up from the entrypoint, so ``site-packages/tamias/plugin``
#: with no ``node_modules`` beside it fails with "Cannot find package
#: 'solid-js'" and OpenCode reports the plugin as failed.  Verified against
#: OpenCode 2.2.5 on a bare clean-venv install.
#:
#: There is no way around installing them: ``@opentui/*`` is not in OpenCode's
#: own ``node_modules`` (only ``@opencode-ai/plugin`` is), and shipping ~9 MB of
#: native and tree-sitter dependencies inside the wheel to save one command is
#: a bad trade.
PLUGIN_RUNTIME_PEER = "solid-js"

#: Installed plugin lives under ``~/.tamias/plugin`` unless the operator moves
#: the data dir.  It is ours to rewrite; a checkout beside the package is not.
#: ``TAMIAS_HOME`` is the same switch the plugin reads for its own paths.
PLUGIN_ENV_HOME = "TAMIAS_HOME"

#: Files copied from the bundled package into the install dir.  Everything the
#: host needs at runtime: no harness, no lockfile, no node_modules.
PLUGIN_INSTALL_FILES = ("tui.tsx", "package.json", "tsconfig.json", "src")

#: A plugin helper may need the network; a developer's first install is slow,
#: a hung one must not be.
PLUGIN_INSTALL_TIMEOUT = 600.0


def _bun_available() -> bool:
    return shutil.which("bun") is not None


def _plugin_install_dir() -> Path:
    """Where ``setup`` stages the bundled plugin, under the data dir."""
    home = os.environ.get(PLUGIN_ENV_HOME)
    base = Path(home).expanduser() if home else Path.home() / ".tamias"
    return base / "plugin"


def _install_bundled_plugin() -> tuple[Path | None, str | None]:
    """Copy the bundled pop-up beside the data dir and install its peers.

    Returns the ready directory, or ``None`` and why.  A failure is reported,
    never papered over: registering a directory whose imports cannot resolve is
    how a machine ends up with a permanent ``1 plugin failed`` toast.
    """
    packaged = Path(__file__).resolve().parent / "plugin"
    if not _bun_available():
        return None, (
            "the bundled OpenCode plugin needs Bun to install its runtime "
            "dependencies (solid-js, @opentui/core, @opentui/solid), which the "
            "host resolves next to the plugin directory. Install Bun "
            "(https://bun.sh) and re-run this command, then re-run "
            "`tamias opencode setup`"
        )
    target = _plugin_install_dir()
    try:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        for name in PLUGIN_INSTALL_FILES:
            source = packaged / name
            if not source.exists():
                continue
            destination = target / name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
    except OSError as exc:
        return None, f"could not stage the plugin under {target}: {exc}"
    try:
        completed = subprocess.run(  # noqa: S603 - operator opted in
            ["bun", "install", "--cwd", str(target)],
            capture_output=True,
            text=True,
            timeout=PLUGIN_INSTALL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not install the plugin dependencies with Bun: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return None, "bun install failed: " + (detail[-1] if detail else "no output")
    if not (target / "node_modules" / PLUGIN_RUNTIME_PEER).is_dir():
        return None, f"bun install did not leave {PLUGIN_RUNTIME_PEER} beside the plugin"
    return target, None


def _prepare_plugin(explicit: str | None) -> tuple[Path | None, str | None]:
    """Resolve the plugin directory, installing peers when it needs them.

    Three sources, in order: an explicit ``--plugin``, the bundled wheel copy,
    and the source checkout.  A directory that already has its peers beside it
    is used as-is -- that is the developer checkout, and rewriting it is not
    tamias's to do.  Only the bundled copy gets staged and installed, because
    that one ships no ``node_modules`` of its own.
    """
    path = _plugin_directory(explicit)
    if path is None:
        return None, "the TUI plugin was not found"
    if (path / "node_modules" / PLUGIN_RUNTIME_PEER).is_dir():
        return path, None
    packaged = Path(__file__).resolve().parent / "plugin"
    if explicit is None and path == packaged:
        return _install_bundled_plugin()
    # An explicit path is the operator's choice: honour it, but say why the
    # host will fail to load it.  A checkout is theirs to fix, not ours.
    return path, (
        f"{path} has no installed dependencies, so OpenCode will report the "
        "plugin as failed (Cannot find package 'solid-js'). Run `bun install` "
        "in that directory"
    )


def opencode_setup(args: argparse.Namespace) -> int:
    """Write the provider block and plugin entry OpenCode needs, with backups.

    ``install`` deliberately only prints the block, because rewriting a working
    config is not a safe default.  ``setup`` is the opt-in version: it merges
    the ``tamias`` provider into the existing file, keeping every other key,
    and copies the original to a timestamped ``.bak`` first.
    """
    config_path = (
        Path(args.config).expanduser() if args.config else _opencode_config_path()
    )
    try:
        models = _opencode_model_ids(args)
    except (OSError, ValueError) as exc:
        print(f"tamias opencode setup: {exc}", file=sys.stderr)
        return 1
    if not models:
        print(
            "tamias opencode setup: no models to offer; pass --model, or a "
            "--registry/--prices that lists models",
            file=sys.stderr,
        )
        return 2

    try:
        raw = config_path.read_text(encoding="utf-8") if config_path.exists() else "{}"
        stripped, had_comments = _strip_jsonc(raw)
        document = json.loads(stripped) if stripped.strip() else {}
        if not isinstance(document, dict):
            raise ValueError(f"{config_path}: expected a JSON object")
    except (OSError, ValueError) as exc:
        print(f"tamias opencode setup: {exc}", file=sys.stderr)
        return 1

    block = json.loads(agent_config("opencode", args.port, None, models))
    document.setdefault("provider", {}).update(block["provider"])
    plugin, plugin_note = _prepare_plugin(args.plugin)
    if plugin_note:
        print(f"tamias opencode setup: {plugin_note}", file=sys.stderr)
    cli_path = _opencode_cli_path()
    cli_document: dict[str, Any] = {}
    if plugin is not None:
        try:
            cli_document = _read_json_file(cli_path)
        except (OSError, ValueError) as exc:
            print(f"tamias opencode setup: {exc}", file=sys.stderr)
            return 1

    if args.dry_run:
        print(f"would write {config_path}:")
        print(json.dumps(document, indent=2))
        if plugin is not None:
            plugins = list(cli_document.get("plugins") or [])
            if str(plugin) not in plugins:
                plugins.append(str(plugin))
            cli_document["plugins"] = plugins
            print(f"would write {cli_path}:")
            print(json.dumps(cli_document, indent=2))
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        backups: list[Path] = []
        if config_path.exists():
            backups.append(_backup(config_path, stamp))
        config_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        if plugin is not None:
            plugins = [str(entry) for entry in (cli_document.get("plugins") or [])]
            if str(plugin) not in plugins:
                plugins.append(str(plugin))
            cli_document["plugins"] = plugins
            cli_path.parent.mkdir(parents=True, exist_ok=True)
            if cli_path.exists():
                backups.append(_backup(cli_path, stamp))
            cli_path.write_text(
                json.dumps(cli_document, indent=2) + "\n", encoding="utf-8"
            )
    except OSError as exc:
        print(f"tamias opencode setup: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {config_path} (provider tamias, {len(models)} model(s))")
    for backup in backups:
        print(
            f"backup: {backup} (mode 600; it may contain provider credentials "
            "-- delete it once you are satisfied)",
            file=sys.stderr,
        )
    if had_comments:
        print(
            "note: OpenCode's JSONC comments are not preserved; the original "
            "text is in the backup",
            file=sys.stderr,
        )
    if plugin is None:
        print(
            "plugin not registered: /tamias will not open the pop-up on this "
            "machine (see the note above)",
            file=sys.stderr,
        )
    else:
        print(f"registered plugin {plugin} in {cli_path}")
        if plugin_note is None and plugin == _plugin_install_dir():
            print(
                f"installed the plugin's runtime dependencies under {plugin} "
                "with Bun",
                file=sys.stderr,
            )
        legacy = _opencode_commands_dir(None) / "tamias.md"
        if legacy.exists():
            print(
                f"warning: {legacy} also registers /tamias and may shadow the "
                "plugin pop-up; remove it or rename it to tamias-report.md",
                file=sys.stderr,
            )
    print("")
    print("start the proxy with the same --db and --prices:")
    serve = _opencode_serve_command(args)
    print("  " + serve)
    return 0


def _is_tamias_plugin_dir(path: Path) -> bool:
    """Whether ``path`` is a directory this project staged as its plugin."""
    try:
        package = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(package, dict) and package.get("name") == "tamias-opencode-plugin"


def _is_tamias_plugin_entry(entry: object, explicit: Path | None) -> bool:
    """Whether one ``cli.json`` plugin entry names a tamias plugin.

    Exact matches are the staged install directory and an explicit ``--plugin``
    path.  A checkout registered by path is recognised by its package name, so
    an operator who used ``--plugin`` can uninstall without repeating it.
    """
    if not isinstance(entry, str) or not entry:
        return False
    path = Path(entry).expanduser()
    if explicit is not None and path.resolve() == explicit.resolve():
        return True
    if path.resolve() == _plugin_install_dir().resolve():
        return True
    return _is_tamias_plugin_dir(path)


def opencode_uninstall(args: argparse.Namespace) -> int:
    """Remove the provider, plugin registrations and slash commands setup added.

    Only tamias's own entries are touched: ``provider.tamias`` in the OpenCode
    config, plugin entries that name this project's plugin, and the
    ``tamias.md`` / ``tamias-report.md`` command files.  Every other provider,
    plugin and command is left alone, and each rewritten config is backed up
    first.  The staged plugin directory under the data dir is removed too,
    unless ``--keep-plugin`` is given.
    """
    config_path = (
        Path(args.config).expanduser() if args.config else _opencode_config_path()
    )
    explicit = Path(args.plugin).expanduser() if args.plugin else None
    dry_run = bool(args.dry_run)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    changed_config = False
    had_comments = False
    config_document: dict[str, Any] | None = None
    if config_path.exists():
        try:
            raw = config_path.read_text(encoding="utf-8")
            stripped, had_comments = _strip_jsonc(raw)
            parsed = json.loads(stripped) if stripped.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError(f"{config_path}: expected a JSON object")
            config_document = parsed
        except (OSError, ValueError) as exc:
            print(f"tamias opencode uninstall: {exc}", file=sys.stderr)
            return 1
        provider = config_document.get("provider")
        if isinstance(provider, dict) and "tamias" in provider:
            del provider["tamias"]
            if not provider:
                config_document.pop("provider", None)
            changed_config = True

    cli_path = _opencode_cli_path()
    changed_cli = False
    removed_plugins = 0
    cli_document: dict[str, Any] | None = None
    if cli_path.exists():
        try:
            cli_document = _read_json_file(cli_path)
        except (OSError, ValueError) as exc:
            print(f"tamias opencode uninstall: {exc}", file=sys.stderr)
            return 1
        plugins = cli_document.get("plugins")
        if isinstance(plugins, list):
            kept = [
                entry
                for entry in plugins
                if not _is_tamias_plugin_entry(entry, explicit)
            ]
            removed_plugins = len(plugins) - len(kept)
            if removed_plugins:
                changed_cli = True
                if kept:
                    cli_document["plugins"] = kept
                else:
                    cli_document.pop("plugins", None)

    commands = []
    command_dirs = [_opencode_commands_dir(None)]
    project = getattr(args, "project", None)
    if project:
        command_dirs.append(_opencode_commands_dir(project))
    for directory in command_dirs:
        for name in ("tamias.md", "tamias-report.md"):
            path = directory / name
            if path.is_file():
                commands.append(path)

    staged = _plugin_install_dir()
    remove_staged = (
        not args.keep_plugin
        and staged.is_dir()
        and _is_tamias_plugin_dir(staged)
    )

    if not (changed_config or changed_cli or commands or remove_staged):
        print("tamias opencode uninstall: nothing from tamias is registered; nothing to remove")
        return 0

    verb = "would remove" if dry_run else "removed"
    backups: list[Path] = []
    try:
        if changed_config and config_document is not None:
            print(f"{verb} provider tamias from {config_path}")
            if not dry_run:
                backups.append(_backup(config_path, stamp))
                config_path.write_text(
                    json.dumps(config_document, indent=2) + "\n", encoding="utf-8"
                )
        if changed_cli and cli_document is not None:
            print(
                f"{verb} {removed_plugins} tamias plugin registration(s) from {cli_path}"
            )
            if not dry_run:
                backups.append(_backup(cli_path, stamp))
                cli_path.write_text(
                    json.dumps(cli_document, indent=2) + "\n", encoding="utf-8"
                )
        for path in commands:
            print(f"{verb} {path}")
            if not dry_run:
                path.unlink()
        if remove_staged:
            print(f"{verb} staged plugin {staged}")
            if not dry_run:
                shutil.rmtree(staged)
    except OSError as exc:
        print(f"tamias opencode uninstall: {exc}", file=sys.stderr)
        return 1

    if not dry_run:
        for backup in backups:
            print(
                f"backup: {backup} (mode 600; it may contain provider "
                "credentials -- delete it once you are satisfied)",
                file=sys.stderr,
            )
        if had_comments:
            print(
                "note: OpenCode's JSONC comments were not preserved; the "
                "original text is in the backup",
                file=sys.stderr,
            )
    print("restart OpenCode to unload the provider and plugin")
    return 0


def _shell_join(argv: Sequence[str], *, windows: bool = False) -> str:
    """Quote an argv for the shell the user will paste it into.

    POSIX shells get :func:`shlex.join`; Windows gets the quoting
    :mod:`subprocess` uses for ``CreateProcess``, which ``cmd.exe`` and
    PowerShell both accept for ordinary paths and flags.  Using POSIX quoting
    on Windows turns ``C:\\Program Files\\...`` into a command that cannot run.

    The platform is an argument rather than a global read so the same code is
    testable on every runner, and so a caller cannot accidentally quote for the
    wrong shell.
    """
    if windows:
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _opencode_serve_command(args: argparse.Namespace) -> str:
    """The serve command that mirrors the config just written.

    A provider catalogue is the v1 shape: one endpoint in front of every key the
    user has.  Only when there is no catalogue does the single-upstream
    ``--provider`` preset make sense, because that preset points at one service.
    """
    db = str(Path(args.db).expanduser())
    prices = str(Path(args.prices).expanduser())
    cheap = ",".join(_model_pool(getattr(args, "cheap_model", "") or ""))
    providers = getattr(args, "providers", None)
    if providers is None:
        default = Path.home() / ".tamias" / "providers.toml"
        providers = str(default) if default.exists() else None
    argv = ["tamias", "serve"]
    if providers:
        argv.extend(["--providers", str(Path(providers).expanduser())])
    else:
        argv.extend(["--provider", "opencode"])
    argv.extend(
        [
            "--db",
            db,
            "--prices",
            prices,
            "--port",
            str(args.port),
            "--router-mode",
            "active",
            "--min-gap",
            "0",
            "--cheap-model",
            cheap or "<free-model>",
        ]
    )
    registry = getattr(args, "registry", None)
    if registry:
        argv.extend(["--registry", str(Path(registry).expanduser())])
    return _shell_join(argv, windows=os.name == "nt")


OPENCODE_COMMAND_FILENAME = "tamias.md"


def _opencode_commands_dir(project: str | None) -> Path:
    """Where OpenCode loads custom slash commands from.

    Global commands live in ``$XDG_CONFIG_HOME/opencode/commands`` (default
    ``~/.config/opencode/commands``); project commands in
    ``<project>/.opencode/commands``.
    """
    if project:
        return Path(project).expanduser() / ".opencode" / "commands"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "opencode" / "commands"


def _opencode_model_ids(args: argparse.Namespace) -> list[str]:
    """The models to offer in OpenCode's picker, from what the user passed.

    A registry is the strongest source because it records capabilities. Without
    one, only free models from the price sheet are offered, so a large paid
    sheet does not dump hundreds of unrelated ids into the config. ``--model``
    adds explicit ids, and ``--cheap-model`` is always included because the
    printed serve command names it.
    """
    models: list[str] = []
    registry = getattr(args, "registry", None)
    if registry:
        models.extend(load_registry(Path(registry).expanduser()).ids())
    else:
        sheet = load_effective_sheet(args.prices)
        models.extend(
            model
            for model, price in sheet.models.items()
            if price.input == 0 and price.output == 0
        )
    models.extend(getattr(args, "model", None) or [])
    cheap = getattr(args, "cheap_model", None)
    if cheap:
        # The same flag carries a single model or a comma-separated pool; the
        # picker must list each member, never the comma-joined string as an id.
        models.extend(part.strip() for part in cheap.split(",") if part.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for model in models:
        if model not in seen:
            seen.add(model)
            unique.append(model)
    return unique


def _tamias_executable() -> str:
    """The tamias binary to embed in a generated OpenCode command.

    Prefer the executable that is running this command.  A user in a virtualenv
    may have an older ``tamias`` earlier on ``PATH``, and the slash command must
    call the version that wrote it, not whatever ``which`` finds first.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    candidate = Path(argv0).expanduser()
    if candidate.name in ("tamias", "tamias.exe") and candidate.is_file():
        return str(candidate.resolve())
    return shutil.which("tamias") or "tamias"


def opencode_install(args: argparse.Namespace) -> int:
    """Write a report slash command OpenCode loads at startup.

    Without the TUI plugin the command is ``/tamias`` and injects
    ``tamias report`` output into the prompt, so the model can summarise real
    logged numbers instead of guessing them.  With the plugin installed it is
    ``/tamias-report``, because the plugin already owns ``/tamias`` and opens
    the live pop-up.  It never touches ``opencode.json``: the provider block is
    printed for the user to merge, because rewriting a working config is not a
    safe default.
    """
    db = str(Path(args.db).expanduser())
    prices = str(Path(args.prices).expanduser())
    # The TUI plugin owns `/tamias` when it is installed.  A markdown command
    # with the same name is a second registration of the same slash command,
    # and which one OpenCode runs is not a contract.  Keep the report command
    # under `/tamias-report` in that case; a standalone install without the
    # plugin still gets the familiar `/tamias`.
    plugin, plugin_note = _prepare_plugin(getattr(args, "plugin", None))
    if plugin_note:
        print(f"tamias opencode install: {plugin_note}", file=sys.stderr)
    command_name = "tamias-report" if plugin is not None else "tamias"
    target = _opencode_commands_dir(args.project) / f"{command_name}.md"
    if target.exists() and not args.force:
        print(
            f"tamias opencode install: {target} already exists; pass --force to replace it",
            file=sys.stderr,
        )
        return 2
    try:
        models = _opencode_model_ids(args)
    except (OSError, ValueError) as exc:
        print(f"tamias opencode install: {exc}", file=sys.stderr)
        return 1
    if not models:
        print(
            "tamias opencode install: no models to offer; pass --model, or a "
            "--registry/--prices that lists free models",
            file=sys.stderr,
        )
        return 2
    report_argv = [_tamias_executable(), "report", "--db", db, "--prices", prices]
    inr_rate = getattr(args, "inr_rate", None)
    if inr_rate is not None:
        report_argv.extend(["--inr-rate", f"{inr_rate:g}"])
    report_command = _shell_join(report_argv, windows=os.name == "nt")
    content = (
        "---\n"
        "description: Show tamias savings, tokens and routing status\n"
        "---\n\n"
        f"!`{report_command}`\n\n"
        "Summarise the report above in at most six lines: requests, total cost "
        "(or why it is UNKNOWN), the token counts, how many requests were routed "
        "or would have been routed to the cheap model, and the estimated and "
        "realised saving. Never invent a dollar figure when the report says "
        "UNKNOWN -- quote the token counts instead. End with one sentence on what "
        "tamias is doing in this session.\n"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"tamias opencode install: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {target}")
    print("")
    print("provider block for opencode.json (merge it, do not replace the file):")
    print(agent_config("opencode", args.port, None, models))
    print("")
    print("start the proxy with the same --db and --prices:")
    print("  " + _opencode_serve_command(args))
    print("")
    print("or let `tamias opencode setup` merge the provider block for you")
    print(f"then restart OpenCode and type /{command_name}")
    return 0


SERVICE_UNIT_NAME = "tamias.service"
SERVICE_MARKER = "# Managed by tamias: tamias service install"


def _systemd_user_dir() -> Path:
    """The systemd user unit directory, honouring ``XDG_CONFIG_HOME``."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "systemd" / "user"


def _service_file(value: str, label: str) -> str:
    """An existing input file as an absolute path, or a usage error."""
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    return str(path.resolve())


def _service_db(value: str) -> str:
    """A database path whose directory exists, resolved to absolute."""
    path = Path(value).expanduser()
    if not path.parent.is_dir():
        raise ValueError(f"database directory does not exist: {path.parent}")
    return str(path.resolve())


def _systemd_exec_arg(value: str) -> str:
    """Quote one ``ExecStart`` argument for systemd.

    systemd has its own quoting rules, not the shell's, so ``shlex.quote`` is
    not safe here.  ``%`` is a specifier introducer even inside quotes and must
    be doubled.  A value containing both quote styles (or a backslash) is
    rejected rather than guessed at.
    """
    if not value:
        return '""'
    value = value.replace("%", "%%")
    safe = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "/._-:,=+@"
    )
    if all(ch in safe for ch in value):
        return value
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("a service argument contains a control character")
    if "'" not in value and "\\" not in value:
        return f"'{value}'"
    if '"' not in value and "\\" not in value and "$" not in value:
        return f'"{value}"'
    raise ValueError(
        "cannot safely quote a service argument containing both quote styles, "
        "a backslash, or a dollar sign"
    )


def render_service_unit(
    executable: str,
    *,
    db: str,
    prices: str,
    registry: str | None,
    cheap_model: str,
    port: int,
    min_gap: int,
    provider: str,
    router_mode: str,
    providers: str | None = None,
) -> str:
    """A systemd user unit that runs ``tamias serve`` with these settings.

    ``providers`` names a catalogue and takes precedence over the single
    ``provider`` preset, so the service can route across every configured
    provider instead of pinning one upstream.
    """
    argv = [executable, "serve"]
    if providers:
        argv.extend(["--providers", providers])
    else:
        argv.extend(["--provider", provider])
    argv.extend(
        [
            "--db",
            db,
            "--prices",
            prices,
            "--port",
            str(port),
            "--router-mode",
            router_mode,
            "--min-gap",
            str(min_gap),
        ]
    )
    if registry:
        argv.extend(["--registry", registry])
    if cheap_model:
        argv.extend(["--cheap-model", cheap_model])
    exec_start = " ".join(_systemd_exec_arg(part) for part in argv)
    return (
        f"{SERVICE_MARKER}\n"
        "[Unit]\n"
        "Description=tamias model router for OpenCode\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "TimeoutStopSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _service_env_warning(catalogue_path: str | None) -> str | None:
    """One note about the provider keys the unit will need, or ``None``.

    A ``systemd --user`` unit does not inherit a shell's environment, so a
    provider that reads ``api_key_env`` is *unconfigured* under the unit even
    though the same command works in a terminal -- the variable never arrives.
    ``api_key_command`` is fine: the unit runs it at start-up, exactly as
    ``serve`` does.  Naming the variables at install time turns that silent
    gap into a one-line instruction instead of an empty pool at 3am.

    The document is read rather than resolved: ``load_providers`` only reports
    providers whose key it *found*, and it is precisely the env-var ones it
    finds here and would not find in the unit.
    """
    path = Path(catalogue_path) if catalogue_path else default_providers_path()
    if not path or not path.is_file():
        return None
    try:
        import tomllib

        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = document.get("providers") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return None
    notes: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("api_key_command"):
            continue  # the unit runs the command itself
        variable = entry.get("api_key_env")
        if not isinstance(variable, str) or not variable.strip():
            continue
        name = entry.get("name", "?")
        notes.append(f"{name} reads {variable.strip()} from the environment")
    if not notes:
        return None
    joined = "; ".join(sorted(notes))
    return (
        f"note: the unit does not inherit your shell's environment, so {joined}. "
        "Either export it in the unit (systemctl --user edit tamias, then add "
        "Environment=NAME=...), or route that provider by api_key_command so "
        "the service resolves the key itself"
    )


def _systemctl_user(*command: str) -> int:
    """Run ``systemctl --user`` and report failure without raising."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", *command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"tamias service: systemctl failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(
            f"tamias service: systemctl {' '.join(command)} failed"
            f"{': ' + detail if detail else ''}",
            file=sys.stderr,
        )
    return result.returncode


def service_install(args: argparse.Namespace) -> int:
    """Write a systemd user unit for ``tamias serve``.

    The unit is written but not started unless ``--start`` is passed, so an
    install never changes a running system by surprise.  Active routing needs
    the same things ``serve`` needs: a target and a registry that vouches for
    it.  Checking that here turns a unit that would crash-loop into a usage
    error before anything is written.
    """
    if sys.platform != "linux":
        print(
            "tamias service install: systemd user units are Linux-only; "
            "see docs/OPENCODE.md for manual background setup",
            file=sys.stderr,
        )
        return 2
    if args.provider not in PROVIDER_PRESETS:
        choices = ", ".join(sorted(PROVIDER_PRESETS))
        print(
            f"tamias service install: unknown provider {args.provider!r}; "
            f"choose one of: {choices}",
            file=sys.stderr,
        )
        return 2
    cheap_models = _model_pool(args.cheap_model)
    if args.router_mode == "active" and not cheap_models:
        print(
            "tamias service install: active routing needs --cheap-model",
            file=sys.stderr,
        )
        return 2
    if args.router_mode == "active" and not args.registry:
        print(
            "tamias service install: active routing needs --registry so every "
            "target is vouched for",
            file=sys.stderr,
        )
        return 2
    if args.router_mode == "active":
        try:
            registry_path = _service_file(args.registry, "registry")
            prices_path = _service_file(args.prices, "price sheet")
            registry_obj = load_registry(Path(registry_path))
            sheet = load_effective_sheet(prices_path)
        except (OSError, ValueError) as exc:
            print(f"tamias service install: {exc}", file=sys.stderr)
            return 2
        for target in cheap_models:
            info = registry_obj.get(target)
            if info is None or not info.tools:
                print(
                    f"tamias service install: target {target!r} is not a "
                    "tool-capable entry in the registry",
                    file=sys.stderr,
                )
                return 2
            if not can_price(target, sheet):
                print(
                    f"tamias service install: target {target!r} is not priced "
                    f"by {args.prices}",
                    file=sys.stderr,
                )
                return 2
    executable = args.executable or _tamias_executable()
    resolved = Path(executable).expanduser()
    if not resolved.is_absolute():
        found = shutil.which(executable)
        if not found:
            print(
                f"tamias service install: cannot find executable {executable!r}; "
                "pass --exec PATH",
                file=sys.stderr,
            )
            return 2
        resolved = Path(found)
    try:
        unit = render_service_unit(
            str(resolved.resolve()),
            db=_service_db(args.db),
            prices=_service_file(args.prices, "price sheet"),
            registry=_service_file(args.registry, "registry") if args.registry else None,
            cheap_model=",".join(cheap_models),
            port=args.port,
            min_gap=args.min_gap,
            provider=args.provider,
            router_mode=args.router_mode,
            providers=(
                _service_file(args.providers, "provider catalogue")
                if getattr(args, "providers", None)
                else None
            ),
        )
    except ValueError as exc:
        print(f"tamias service install: {exc}", file=sys.stderr)
        return 2
    target = _systemd_user_dir() / SERVICE_UNIT_NAME
    note = _service_env_warning(args.providers or None)
    if note:
        print(note, file=sys.stderr)
    if args.dry_run:
        print(f"# {target}")
        print(unit, end="")
        return 0
    if target.exists() and not args.force:
        print(
            f"tamias service install: {target} already exists; pass --force to replace it",
            file=sys.stderr,
        )
        return 2
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(unit, encoding="utf-8")
    except OSError as exc:
        print(f"tamias service install: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {target}")
    if args.start:
        if _systemctl_user("daemon-reload") != 0:
            print("the unit was written but not started", file=sys.stderr)
            return 1
        if _systemctl_user("enable", "--now", SERVICE_UNIT_NAME) != 0:
            print("the unit was written but not started", file=sys.stderr)
            return 1
        print("started; logs: journalctl --user -u tamias -f")
    else:
        print("enable it with:")
        print("  systemctl --user daemon-reload")
        print(f"  systemctl --user enable --now {SERVICE_UNIT_NAME}")
    return 0


def service_uninstall(args: argparse.Namespace) -> int:
    """Stop and remove the unit, but only one tamias wrote."""
    if sys.platform != "linux":
        print(
            "tamias service uninstall: systemd user units are Linux-only",
            file=sys.stderr,
        )
        return 2
    target = _systemd_user_dir() / SERVICE_UNIT_NAME
    if not target.exists():
        print(f"{SERVICE_UNIT_NAME} is not installed at {target}")
        return 0
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"tamias service uninstall: {exc}", file=sys.stderr)
        return 1
    if SERVICE_MARKER not in text and not args.force:
        print(
            f"tamias service uninstall: {target} was not written by tamias; "
            "pass --force to remove it anyway",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        print(f"would remove {target}")
        if not args.keep_running:
            print(f"would run systemctl --user disable --now {SERVICE_UNIT_NAME}")
        return 0
    if not args.keep_running and _systemctl_user(
        "disable", "--now", SERVICE_UNIT_NAME
    ) != 0:
        print(
            "tamias service uninstall: the unit file is still being removed",
            file=sys.stderr,
        )
    try:
        target.unlink()
    except OSError as exc:
        print(f"tamias service uninstall: {exc}", file=sys.stderr)
        return 1
    _systemctl_user("daemon-reload")
    print(f"removed {target}")
    return 0


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
    explicit: bool = True,
    today: date | None = None,
    opener: Callable[..., Any] = urlopen,
    port_checker: Callable[[int], bool] | None = None,
) -> int:
    """Print local readiness checks without contacting an upstream by default.

    Exit code is a contract: **0 = ready, 1 = a blocking problem**. ``FAIL``
    lines (unsupported Python, an unwritable DB directory, a busy port) set the
    exit code; ``WARN`` lines (a stale price sheet, a missing API key, an
    unreachable upstream) do not, so a WARN-only run still exits 0 and is safe
    to chain with ``&&``.
    """
    blocking = False
    python_version = (sys.version_info.major, sys.version_info.minor)
    if python_version >= (3, 11):
        print(f"OK Python: {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print(f"FAIL Python: {sys.version_info.major}.{sys.version_info.minor}; requires 3.11+")
        blocking = True

    try:
        sheet = load_effective_sheet(prices)
        published = date.fromisoformat(sheet.date)
        age = (today or date.today()) - published
        if age.days < 0 or age.days > PRICE_SHEET_STALE_DAYS:
            print(f"WARN Price sheet: {prices} ({age.days} days old)")
        else:
            print(f"OK Price sheet: {prices} ({age.days} days old)")
    except (OSError, ValueError) as exc:
        if not explicit:
            # No sheet was named, and this is a fresh checkout/install: the
            # resolver will price with the snapshot bundled in the package, so
            # the missing local file is not a problem.
            bundled = _bundled_prices_path()
            if bundled is not None:
                print(
                    f"OK Price sheet: no local sheet; will use the bundled snapshot ({bundled})"
                )
            else:
                print(f"WARN Price sheet: {exc}")
        else:
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
    cheap = _model_pool(str(args.cheap_model or config.cheap_model or ""))
    return RouterConfig(
        profile=config.profile,
        easy_tools=config.easy_tools,
        edit_tools=config.edit_tools,
        shell_tools=config.shell_tools,
        error_markers=config.error_markers,
        big_output_chars=config.big_output_chars,
        effort_policy=args.effort_mode != "off",
        cheap_model=cheap[0] if cheap else "",
        cheap_pool=cheap if len(cheap) > 1 else (),
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


def _bundled_prices_path() -> Path | None:
    """The price sheet shipped with the installed package, if it is on disk."""
    candidate = Path(str(importlib.resources.files("tamias").joinpath(*BUNDLED_PRICES_PARTS)))
    return candidate if candidate.is_file() else None


def _bundled_prices_notice(path: Path) -> str | None:
    """A one-line provenance warning when a command falls back to the bundle."""
    bundled = _bundled_prices_path()
    if bundled is None or path != bundled:
        return None
    return (
        f"tamias: no price sheet at ./{DEFAULT_PRICES_NAME} or "
        f"~/.tamias/{DEFAULT_PRICES_NAME}; using the OpenRouter snapshot bundled "
        f"with tamias ({bundled}). Run `tamias prices fetch` for current rates."
    )


def _resolve_prices_path(explicit: str | None) -> Path | None:
    """The sheet a command prices with: the flag, else ./prices.toml, else
    ~/.tamias/prices.toml, else the snapshot bundled inside the package.

    An *explicit* path that does not exist is still an error -- the caller
    named a file, and silently pricing with a different one would be worse than
    stopping.
    """
    if explicit is not None:
        path = Path(explicit)
        return path if path.is_file() else None
    home_sheet = Path.home() / ".tamias" / DEFAULT_PRICES_NAME
    for candidate in (Path(DEFAULT_PRICES_NAME), home_sheet):
        if candidate.is_file():
            return candidate
    return _bundled_prices_path()


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
    notice = _bundled_prices_notice(prices_path)
    if notice is not None:
        print(notice, file=sys.stderr)
    upstream = _resolve_upstream(args, cfg) or DEFAULT_UPSTREAM
    db_override = pick(args.db, cfg.db, None)
    cheap_model = args.cheap_model if getattr(args, "cheap_model", None) != "" else (
        cfg.cheap_model or ""
    )
    strong_model = args.strong_model if getattr(args, "strong_model", None) != "" else (
        cfg.strong_model or ""
    )
    if getattr(args, "mode", None) is not None:
        print(
            "tamias run: --mode is deprecated and will be removed after the next "
            "release; use --router-mode",
            file=sys.stderr,
        )
    if getattr(args, "router_mode", None) is not None and getattr(args, "mode", None) is not None:
        if args.router_mode != args.mode:
            print(
                "tamias run: --router-mode and the deprecated --mode disagree; "
                "use --router-mode only",
                file=sys.stderr,
            )
            return 2
    explicit_mode = getattr(args, "router_mode", None) or getattr(args, "mode", None)
    run_router_mode = explicit_mode if explicit_mode is not None else cfg.router_mode

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
        cheap_model,
        "--strong-model",
        strong_model,
    ]
    if run_router_mode:
        serve_command.extend(("--router-mode", run_router_mode))
    if getattr(args, "min_gap", None) is not None:
        serve_command.extend(("--min-gap", str(args.min_gap)))
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
        "--providers",
        default=None,
        help=(
            "provider catalogue TOML (default: ~/.tamias/providers.toml when it "
            "exists). Each entry names a base URL and an api_key_env/api_key_command."
        ),
    )
    serve_parser.add_argument(
        "--no-discover",
        dest="discover",
        action="store_false",
        help="do not read each provider's /v1/models at start-up; use the registry as written",
    )
    serve_parser.add_argument(
        "--limits",
        default=None,
        help="sqlite file remembering provider cooldowns (default: limits.db beside the log)",
    )
    serve_parser.add_argument(
        "--prices", default=None, help="path to the TOML price sheet"
    )
    serve_parser.add_argument("--db", default=None, help="path to the sqlite request log")
    serve_parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    serve_parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    serve_parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help=(
            "acknowledge a wider bind and bind anyway; a non-loopback bind still "
            "requires (--auth-token / $TAMIAS_AUTH_TOKEN / --keys-db)"
        ),
    )
    serve_parser.add_argument(
        "--trusted-host",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Host header to allow beyond loopback (repeatable); passing it turns on "
            "the Host allowlist when bound to a wider host"
        ),
    )
    serve_parser.add_argument(
        "--router-mode",
        choices=ROUTER_MODES,
        default="shadow",
        help="off disables routing; shadow records the decision without changing the "
        "request; active also rewrites the model to --cheap-model, but only a "
        "target --registry vouches for and --prices can price (default: shadow)",
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
        help="model a SWITCH moves mechanical work to, or a comma-separated "
        "pool (the first is primary). Each session keeps one pool member while "
        "different sessions spread across the pool. Without it a shadow "
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
        "--misroute-penalty",
        action="store_true",
        help="when a session has been downgraded and the *next* turn fails a tool, "
        "hold that session on the model the caller named for --misroute-turns "
        "requests (default: off). Session-scoped only: one bad turn never "
        "suppresses a model for every conversation, because tamias cannot see the "
        "counterfactual. Every escalation is logged as a STAY whose reason names it",
    )
    serve_parser.add_argument(
        "--misroute-turns",
        type=int,
        default=5,
        metavar="N",
        help="how many requests a misroute escalation holds the strong model for "
        "(default: 5)",
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
        f"--keys-db). Prefer the environment or ${AUTH_TOKEN_FILE_ENV} over argv: "
        f"a token on the command line is visible in ps and shell history",
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
    serve_parser.add_argument(
        "--manifest",
        default=None,
        help="signed policy manifest (JSON) this gateway verifies at startup and "
        "serves at GET /policy; needs --auth-token or --keys-db (default: off)",
    )
    serve_parser.add_argument(
        "--manifest-key",
        default=None,
        help="pin the manifest to this Ed25519 public key (PEM or base64); a "
        "manifest signed by any other key is refused",
    )

    run_parser = sub.add_parser("run", help="run one command through a short-lived proxy")
    run_parser.add_argument(
        "--router-mode",
        choices=ROUTER_MODES,
        default=None,
        help="off disables routing; shadow records the decision without changing the "
        "request; active also rewrites the model to --cheap-model, but only a "
        "target --registry vouches for and --prices can price "
        "(default: the router-config value, else shadow)",
    )
    run_parser.add_argument(
        "--mode",
        choices=ROUTER_MODES,
        default=None,
        help=argparse.SUPPRESS,
    )
    run_parser.add_argument("--effort-mode", choices=EFFORT_MODES, default=None)
    run_parser.add_argument("--registry", default=None)
    run_parser.add_argument("--keys-db", default=None)
    run_parser.add_argument("--request-ceiling-usd", default=None)
    run_parser.add_argument("--project")
    run_parser.add_argument(
        "--cheap-model",
        default="",
        help="model or comma-separated pool a SWITCH moves work to",
    )
    run_parser.add_argument("--strong-model", default="")
    run_parser.add_argument(
        "--min-gap",
        type=int,
        default=None,
        help="requests that must pass after the last switch before switching again",
    )
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
    report_parser.add_argument(
        "--inr-rate",
        type=float,
        default=None,
        metavar="RUPEES_PER_USD",
        help="also show each dollar amount converted to rupees at this fixed "
        "rate (a display conversion, not a billed amount); e.g. --inr-rate 85",
    )
    report_parser.add_argument(
        "--routing-verdict",
        action="store_true",
        help="also print what each downgrade was followed by on the next turn, "
        "read off the decision reasons already in the log (no new data, no "
        "request text); a swap only carries an observed consequence when the "
        "proxy actually rewrote it",
    )
    zen_parser = prices_sub.add_parser(
        "zen",
        help="fetch OpenCode Zen's model list and price its free models at $0",
    )
    zen_parser.add_argument("--out", required=True, help="new TOML price sheet path")
    zen_parser.add_argument("--url", default=ZEN_MODELS_URL, help="Zen model-list URL")
    zen_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )

    set_parser = prices_sub.add_parser(
        "set",
        help="write a rate for one model into the hand-written overlay",
    )
    set_parser.add_argument("model", help="model id, exactly as the provider sends it")
    for field in PRICE_FIELDS:
        set_parser.add_argument(
            f"--{field.replace('_', '-')}",
            dest=field,
            type=float,
            default=None,
            help=f"USD per 1M tokens for {field.replace('_', ' ')} (0 means free)",
        )
    set_parser.add_argument(
        "--sheet",
        default=str(Path.home() / ".tamias" / "prices.toml"),
        help="generated sheet whose overlay should hold the rate "
        "(default: ~/.tamias/prices.toml)",
    )

    agent_parser = sub.add_parser("agent-config", help="print an agent configuration snippet")
    agent_parser.add_argument("agent", choices=("claude-code", "codex", "opencode"))
    agent_parser.add_argument("--port", type=int, default=8000)
    agent_parser.add_argument("--project")
    agent_parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model id to list in the OpenCode provider block (repeatable)",
    )

    opencode_parser = sub.add_parser(
        "opencode", help="OpenCode integration helpers"
    )
    opencode_sub = opencode_parser.add_subparsers(
        dest="opencode_command", required=True
    )
    opencode_install_parser = opencode_sub.add_parser(
        "install", help="write the /tamias slash command into OpenCode's commands directory"
    )
    opencode_install_parser.add_argument(
        "--db", required=True, help="path to the proxy sqlite log the command should read"
    )
    opencode_install_parser.add_argument(
        "--prices", required=True, help="price sheet the command should price with"
    )
    opencode_install_parser.add_argument(
        "--providers",
        default=None,
        help="provider catalogue named in the printed serve command "
        "(default: ~/.tamias/providers.toml when it exists)",
    )
    opencode_install_parser.add_argument("--port", type=int, default=8000)
    opencode_install_parser.add_argument(
        "--project",
        default=None,
        help="write into <project>/.opencode/commands instead of the global config",
    )
    opencode_install_parser.add_argument(
        "--force", action="store_true", help="replace an existing command file"
    )
    opencode_install_parser.add_argument(
        "--plugin",
        default=None,
        help="path to tamias-opencode-plugin; also read from TAMIAS_PLUGIN_DIR",
    )
    opencode_install_parser.add_argument(
        "--registry",
        default=None,
        help="capability registry to name in the printed serve command",
    )
    opencode_install_parser.add_argument(
        "--cheap-model",
        default=None,
        help="free model, or comma-separated pool, to name in the printed "
        "serve command",
    )
    opencode_install_parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model id to offer in OpenCode's picker (repeatable)",
    )
    opencode_install_parser.add_argument(
        "--inr-rate",
        type=float,
        default=None,
        help="rupees per US dollar to embed as a display conversion in /tamias",
    )

    opencode_setup_parser = opencode_sub.add_parser(
        "setup",
        help="merge the tamias provider (and optional TUI plugin) into OpenCode's config",
    )
    opencode_setup_parser.add_argument(
        "--config",
        default=None,
        help="OpenCode config to edit (default: ~/.config/opencode/opencode.jsonc)",
    )
    opencode_setup_parser.add_argument(
        "--plugin",
        default=None,
        help="path to tamias-opencode-plugin; also read from TAMIAS_PLUGIN_DIR",
    )
    opencode_setup_parser.add_argument(
        "--db",
        default=str(Path.home() / ".tamias" / "requests.db"),
        help="request log named in the printed serve command",
    )
    opencode_setup_parser.add_argument(
        "--prices",
        default=str(Path.home() / ".tamias" / "prices.toml"),
        help="price sheet used for the model list and the serve command",
    )
    opencode_setup_parser.add_argument(
        "--providers",
        default=None,
        help="provider catalogue named in the printed serve command "
        "(default: ~/.tamias/providers.toml when it exists)",
    )
    opencode_setup_parser.add_argument("--port", type=int, default=8000)
    opencode_setup_parser.add_argument(
        "--registry", default=None, help="capability registry to offer in the picker"
    )
    opencode_setup_parser.add_argument(
        "--cheap-model", default=None, help="free model, or comma-separated pool"
    )
    opencode_setup_parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model id to offer in OpenCode's picker (repeatable)",
    )
    opencode_setup_parser.add_argument(
        "--dry-run", action="store_true", help="print the result without writing"
    )

    opencode_uninstall_parser = opencode_sub.add_parser(
        "uninstall",
        help="remove tamias's provider block, plugin registration and slash commands",
    )
    opencode_uninstall_parser.add_argument(
        "--config",
        default=None,
        help="OpenCode config to edit (default: ~/.config/opencode/opencode.jsonc)",
    )
    opencode_uninstall_parser.add_argument(
        "--plugin",
        default=None,
        help="explicit plugin path to unregister; also read from TAMIAS_PLUGIN_DIR",
    )
    opencode_uninstall_parser.add_argument(
        "--project",
        default=None,
        help="also remove the command files under <project>/.opencode/commands",
    )
    opencode_uninstall_parser.add_argument(
        "--keep-plugin",
        action="store_true",
        help="unregister the plugin but leave ~/.tamias/plugin on disk",
    )
    opencode_uninstall_parser.add_argument(
        "--dry-run", action="store_true", help="print what would change without writing"
    )

    service_parser = sub.add_parser(
        "service", help="install tamias as a systemd user service (Linux)"
    )
    service_sub = service_parser.add_subparsers(
        dest="service_command", required=True
    )
    service_install_parser = service_sub.add_parser(
        "install", help="write a systemd user unit for tamias serve"
    )
    service_install_parser.add_argument(
        "--db", required=True, help="request log the service should write"
    )
    service_install_parser.add_argument(
        "--prices", required=True, help="price sheet the service should use"
    )
    service_install_parser.add_argument(
        "--registry", default=None, help="capability registry the service should use"
    )
    service_install_parser.add_argument(
        "--cheap-model",
        default="",
        help="model or comma-separated pool the service routes to",
    )
    service_install_parser.add_argument("--port", type=int, default=8000)
    service_install_parser.add_argument(
        "--min-gap",
        type=int,
        default=0,
        help="requests between switches (default 0, so the first easy turn routes)",
    )
    service_install_parser.add_argument(
        "--provider", default="opencode", help="upstream preset (default: opencode)"
    )
    service_install_parser.add_argument(
        "--providers",
        default=None,
        help="provider catalogue to route across (wins over --provider)",
    )
    service_install_parser.add_argument(
        "--router-mode", choices=ROUTER_MODES, default="active"
    )
    service_install_parser.add_argument(
        "--exec",
        dest="executable",
        default=None,
        help="tamias executable to embed (default: the one running this command)",
    )
    service_install_parser.add_argument(
        "--start",
        action="store_true",
        help="run systemctl --user enable --now after writing the unit",
    )
    service_install_parser.add_argument(
        "--dry-run", action="store_true", help="print the unit without writing it"
    )
    service_install_parser.add_argument(
        "--force", action="store_true", help="replace an existing unit file"
    )
    service_uninstall_parser = service_sub.add_parser(
        "uninstall", help="stop and remove the unit tamias wrote"
    )
    service_uninstall_parser.add_argument(
        "--dry-run", action="store_true", help="show what would be removed"
    )
    service_uninstall_parser.add_argument(
        "--force",
        action="store_true",
        help="remove a unit file even when it lacks tamias's marker",
    )
    service_uninstall_parser.add_argument(
        "--keep-running",
        action="store_true",
        help="remove the unit without stopping the running proxy",
    )

    registry_parser = sub.add_parser(
        "registry", help="build a model capability registry"
    )
    registry_sub = registry_parser.add_subparsers(
        dest="registry_command", required=True
    )
    registry_zen_parser = registry_sub.add_parser(
        "zen",
        help="write a registry for OpenCode Zen's free models from models.dev",
    )
    registry_zen_parser.add_argument(
        "--out", required=True, help="new TOML registry path"
    )
    registry_zen_parser.add_argument(
        "--models-url", default=ZEN_MODELS_URL, help="Zen model-list URL"
    )
    registry_zen_parser.add_argument(
        "--catalog-url", default=MODELS_DEV_URL, help="models.dev catalogue URL"
    )
    registry_zen_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
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

    admin_parser = sub.add_parser(
        "admin", help="manage company keys and signed project manifests"
    )
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

    manifest_parser = admin_sub.add_parser(
        "manifest", help="sign, verify and generate keys for project manifests"
    )
    manifest_sub = manifest_parser.add_subparsers(
        dest="manifest_command", required=True
    )
    keygen_parser = manifest_sub.add_parser(
        "keygen", help="generate an Ed25519 signing keypair"
    )
    keygen_parser.add_argument(
        "--private-out", required=True, help="where to write the private key (PEM)"
    )
    keygen_parser.add_argument(
        "--public-out", required=True, help="where to write the public key (PEM)"
    )
    sign_parser = manifest_sub.add_parser(
        "sign", help="sign a policy TOML into a manifest"
    )
    sign_parser.add_argument("--policy", required=True, help="path to the policy TOML")
    sign_parser.add_argument(
        "--key", required=True, help="path to the Ed25519 private key (PEM)"
    )
    sign_parser.add_argument(
        "--project", required=True, help="project the manifest is bound to"
    )
    sign_parser.add_argument(
        "--expires", required=True, help="ISO-8601 expiry, e.g. 2099-12-31T00:00:00Z"
    )
    sign_parser.add_argument(
        "--issued-at", default=None, help="ISO-8601 issue time (default: now)"
    )
    sign_parser.add_argument(
        "-o", "--out", default=None, help="write the manifest here (default: stdout)"
    )
    verify_parser = manifest_sub.add_parser("verify", help="verify a signed manifest")
    verify_parser.add_argument("manifest", help="path to the manifest JSON")
    verify_parser.add_argument(
        "--key", default=None, help="pin the expected public key (PEM or base64)"
    )
    verify_parser.add_argument(
        "--project", default=None, help="require the manifest to be for this project"
    )

    config_parser = sub.add_parser("config", help="show the effective configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="print the config file path that would be read")
    config_sub.add_parser("show", help="print the values loaded from the config file")
    config_sub.add_parser("providers", help="list the built-in upstream presets")
    sub.add_parser("demo", help="run a zero-credit fixture demo against the mock upstream")
    reconcile_parser = sub.add_parser(
        "reconcile",
        help="reconcile logged rows against the provider's billed cost per request",
    )
    reconcile_parser.add_argument("--db", required=True, help="path to the proxy sqlite log")
    reconcile_parser.add_argument(
        "--api-key-env",
        default="OPENROUTER_API_KEY",
        help="environment variable holding the generation-lookup key "
        "(only read with --online)",
    )
    reconcile_parser.add_argument(
        "--online",
        action="store_true",
        help="also read the provider's generation record and use it as the "
        "billed cost (network; requires --api-key-env)",
    )
    reconcile_parser.add_argument(
        "--invoice",
        default=None,
        help="CSV export of provider invoice line items; the invoice becomes "
        "the only billed side",
    )
    reconcile_parser.add_argument(
        "--invoice-join",
        choices=reconcile_mod.INVOICE_JOINS,
        default="request_id",
        help="match invoice lines to the Tamias row id or the provider "
        "generation id (default: request_id)",
    )
    reconcile_parser.add_argument(
        "--invoice-scope",
        choices=reconcile_mod.INVOICE_SCOPES,
        default="row",
        help="row-level lines, or one line per UTC day when the provider only "
        "publishes daily totals (default: row)",
    )
    reconcile_parser.add_argument(
        "--invoice-key-column",
        default=None,
        help="CSV identity column (default: the --invoice-join name, or 'date' "
        "for --invoice-scope day)",
    )
    reconcile_parser.add_argument(
        "--invoice-cost-column",
        default="cost_usd",
        help="CSV USD amount column (default: cost_usd)",
    )
    reconcile_parser.add_argument(
        "--invoice-allow-unmatched",
        action="store_true",
        help="invoice lines outside the selected window do not block a PASS; "
        "selected rows missing from the invoice always stay INCOMPLETE",
    )
    reconcile_parser.add_argument(
        "--out", default=None, help="write a text-free artifact to this path"
    )
    reconcile_parser.add_argument("--since", default=None, help="YYYY-MM-DD lower bound")
    reconcile_parser.add_argument("--until", default=None, help="YYYY-MM-DD upper bound")
    reconcile_parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="USD tolerance for a match (default: 0.000001)",
    )
    reconcile_parser.add_argument("--json", action="store_true", help="emit JSON")
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




def _models_opener() -> Any:
    """An opener that identifies tamias when fetching a model catalogue.

    OpenCode's Zen endpoint answers 403 to Python's default urllib
    user-agent, so a model fetch must name itself.  The header carries no
    secret: it is a product name and version.
    """
    from urllib.request import build_opener

    opener = build_opener()
    opener.addheaders = [
        ("User-Agent", f"tamias/{package_version()}"),
        ("Accept", "application/json"),
    ]
    return opener


def demo_command(args: argparse.Namespace) -> int:
    """Run one request through the proxy against a bundled mock upstream.

    Zero keys, zero network, zero config. Writes a local SQLite log and then
    runs ``tamias report`` over it.
    """
    import asyncio
    import os
    from pathlib import Path

    import httpx

    from tamias import proxy
    from tamias.demo_upstream import StreamingASGITransport, create_mock_upstream
    from tamias.router import RouterConfig
    from tamias.store import Store

    tmpdir = Path(os.getcwd()) / ".tamias-demo"
    tmpdir.mkdir(parents=True, exist_ok=True)
    db = tmpdir / "demo.db"
    prices = getattr(args, "prices", None)
    if prices:
        prices = Path(prices)
    else:
        prices = _bundled_prices_path()
    if prices is None:
        print("tamias demo: no price sheet found", file=sys.stderr)
        return 1

    upstream = create_mock_upstream()
    store = Store(str(db))
    sheet = load_effective_sheet(str(prices))
    try:
        cfg = RouterConfig(
            cheap_model=(
                getattr(sheet, "default_cheap", None)
                or "cohere/north-mini-code:free"
            ),
            strong_model="deepseek/deepseek-v4-pro",
        )
    except Exception:
        cfg = RouterConfig()
    app = proxy.create_app(
        "http://upstream.invalid",
        store,
        sheet,
        "shadow",
        config=cfg,
        transport=StreamingASGITransport(upstream),
    )
    client = httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test"
    )
    try:
        resp = asyncio.run(
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "deepseek/deepseek-v4-pro",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        print(f"status: {resp.status_code}")
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:
            pass
    print(f"db: {db}")
    try:
        report(str(db), str(prices))
    except Exception as exc:
        print(f"report failed: {exc}", file=sys.stderr)
    return 0


def reconcile_command(args: argparse.Namespace) -> int:
    """Compare each row's computed list cost with the provider's billed cost.

    Offline is the default and uses only the request log: no provider is
    called and no key is read.  ``--online`` additionally reads the documented
    OpenRouter generation record for every selected row that carries a
    generation id, and uses that number as the billed side.  A lookup that
    fails leaves the row unknown rather than guessing zero.

    Exit codes are the contract in ``docs/RECONCILE.md``: 0 PASS, 3 FAIL, 2
    INCOMPLETE or ZERO_ROWS, 1 runtime failure.
    """
    from dataclasses import replace
    from pathlib import Path

    rows = _read_rows(args.db)
    try:
        selected_rows = reconcile_mod.select_rows(
            rows,
            getattr(args, "since", None),
            getattr(args, "until", None),
        )
    except ValueError as exc:
        print(f"tamias reconcile: {exc}", file=sys.stderr)
        return 1
    # A passthrough row is count-only by design: it has no model price and is
    # never sent through the pricing path.  Counting it as an unpriced request
    # would drown the rows that can actually be reconciled.
    selected = [
        row for row in selected_rows if row.get("route") != "passthrough"
    ]
    passthrough_excluded = len(selected_rows) - len(selected)
    if not selected:
        window = ""
        if getattr(args, "since", None) or getattr(args, "until", None):
            window = (
                f" in --since {getattr(args, 'since', None) or 'start'}"
                f" --until {getattr(args, 'until', None) or 'end'}"
            )
        print(
            f"ZERO_ROWS: no metered rows selected{window}; run tamias reconcile "
            "after requests have been logged",
            file=sys.stderr,
        )
        return 2

    tolerance = (
        reconcile_mod.DEFAULT_TOLERANCE
        if getattr(args, "tolerance", None) is None
        else float(args.tolerance)
    )

    invoice_path = getattr(args, "invoice", None)
    invoice = None
    invoice_scope = getattr(args, "invoice_scope", "row")
    invoice_join = getattr(args, "invoice_join", "request_id")
    invoice_matched = 0
    invoice_unmatched: list[str] = []
    invoice_undated = 0
    online = bool(getattr(args, "online", False))
    if invoice_path and online:
        print(
            "tamias reconcile: --online and --invoice are alternative billed "
            "sources; pass one",
            file=sys.stderr,
        )
        return 1
    if invoice_path:
        key_column = getattr(args, "invoice_key_column", None) or (
            "date" if invoice_scope == "day" else invoice_join
        )
        try:
            invoice = reconcile_mod.load_invoice(
                invoice_path,
                key_column=key_column,
                cost_column=getattr(args, "invoice_cost_column", "cost_usd"),
            )
        except (OSError, ValueError) as exc:
            print(f"tamias reconcile: {exc}", file=sys.stderr)
            return 1

    lookups: dict[str, float] = {}
    lookup_errors = 0
    lookup_skipped = 0
    if online:
        key_env = getattr(args, "api_key_env", reconcile_mod.DEFAULT_KEY_ENV)
        token = os.environ.get(key_env)
        if not token:
            print(f"tamias reconcile: {key_env} is not set", file=sys.stderr)
            return 1
        lookup_skipped = sum(
            1
            for row in selected
            if row.get("generation_id")
            and not reconcile_mod.is_openrouter_generation_id(
                row.get("generation_id")
            )
        )
        with reconcile_mod.httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=20.0
        ) as client:
            lookups, lookup_errors = reconcile_mod.lookup_generation_costs(
                selected, client=client
            )
        message = (
            f"looked up {len(lookups)} generation(s); "
            f"{lookup_errors} generation lookup(s) failed"
        )
        if lookup_skipped:
            message += (
                f"; {lookup_skipped} non-OpenRouter generation id(s) skipped"
            )
        print(message, file=sys.stderr)

    enriched: list[dict[str, Any]] = []
    for row in selected:
        generation_id = row.get("generation_id")
        if isinstance(generation_id, str) and generation_id in lookups:
            enriched.append({**row, "lookup_cost_usd": lookups[generation_id]})
        else:
            enriched.append(row)

    if invoice is not None:
        if invoice_scope == "day":
            enriched, invoice_unmatched, invoice_matched, invoice_undated = (
                reconcile_mod.aggregate_invoice_days(selected, invoice.costs)
            )
        else:
            enriched, invoice_matched, invoice_unmatched = (
                reconcile_mod.apply_invoice(
                    enriched, invoice.costs, key=invoice_join
                )
            )

    try:
        compared = reconcile_mod.compare_rows(
            enriched,
            tolerance=tolerance,
            invoice_only=invoice is not None,
        )
    except ValueError as exc:
        print(f"tamias reconcile: {exc}", file=sys.stderr)
        return 1
    summary = reconcile_mod.summarize(compared)
    # In online mode the lookup is the independent verification.  If any
    # selected row's lookup failed, a stored number must not turn the run into
    # a PASS: the verification did not happen for every row.
    if online and lookup_errors and summary.status == reconcile_mod.PASS:
        summary = replace(summary, status=reconcile_mod.INCOMPLETE)
    # An invoice proof is complete only when every selected row is covered and
    # every invoice line belongs to the selected window.  A stored number never
    # fills a gap: that would mix a response-reported cost into a proof.
    if invoice is not None:
        allow_unmatched = bool(getattr(args, "invoice_allow_unmatched", False))
        incomplete = (
            invoice_matched == 0
            or invoice_undated > 0
            or (bool(invoice_unmatched) and not allow_unmatched)
        )
        if incomplete and summary.status == reconcile_mod.PASS:
            summary = replace(summary, status=reconcile_mod.INCOMPLETE)

    out_path = getattr(args, "out", None)
    if out_path:
        artifact = []
        for row in compared:
            artifact.append(
                {
                    "request_id": row.request,
                    "ts": row.ts,
                    "session_id_truncated": (row.session_id or "")[:8],
                    "model_requested": row.model_requested,
                    "model_used": row.model_used,
                    "generation_id": row.generation_id,
                    "tokens": {
                        "prompt_tokens": row.prompt_tokens,
                        "completion_tokens": row.completion_tokens,
                    },
                    "computed_cost": row.computed,
                    "billed_cost": row.billed,
                    "difference": row.difference,
                    "matches": row.matches,
                    "billed_source": row.billed_source,
                    "logged_billed_cost": row.logged_billed,
                    "lookup_cost": row.lookup_billed,
                    "invoice_cost": row.invoice_billed,
                }
            )
        destination = Path(out_path)
        destination.write_text(
            json.dumps(artifact, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {destination}", file=sys.stderr)

    if getattr(args, "json", False):
        document = summary.as_dict()
        document["tolerance_usd"] = tolerance
        document["passthrough_excluded"] = passthrough_excluded
        if online:
            document["lookups"] = {
                "succeeded": len(lookups),
                "failed": lookup_errors,
                "skipped": lookup_skipped,
            }
        if invoice is not None:
            document["invoice"] = {
                "scope": invoice_scope,
                "join": invoice_join if invoice_scope == "row" else "date",
                "lines": invoice.lines,
                "duplicates": invoice.duplicates,
                "matched": invoice_matched,
                "unmatched": len(invoice_unmatched),
                "undated_rows": invoice_undated,
            }
        print(json.dumps(document, sort_keys=True))
    else:
        print(f"rows: {summary.rows} selected of {len(selected)} metered in log")
        if passthrough_excluded:
            print(
                f"passthrough rows excluded: {passthrough_excluded} "
                "(count-only, never priced)"
            )
        if invoice is not None:
            joined_by = invoice_join if invoice_scope == "row" else "date"
            print(
                f"invoice: {invoice.lines} line(s) by {joined_by} "
                f"({invoice_scope} scope); {invoice_matched} matched"
            )
            if invoice.duplicates:
                print(f"invoice duplicate keys summed: {invoice.duplicates}")
            if invoice_unmatched:
                print(
                    f"invoice lines matching no selected row: "
                    f"{len(invoice_unmatched)}"
                )
            if invoice_undated:
                print(f"rows without a usable date: {invoice_undated}")
        print(f"compared: {summary.compared} of {summary.rows}")
        print(f"matched: {summary.matched}")
        print(f"mismatched: {summary.mismatched}")
        print(f"{summary.without_billed} without a billed cost")
        print(f"{summary.without_computed} without a computed cost")
        print(f"computed: {_money(summary.computed_usd)}")
        print(f"billed: {_money(summary.billed_usd)}")
        print(f"difference (billed - computed): {_money(summary.difference_usd)}")
        print(summary.status)

    if summary.status == reconcile_mod.FAIL:
        return 3
    if summary.status in (reconcile_mod.INCOMPLETE, reconcile_mod.EMPTY):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    args = build_parser().parse_args(raw_argv)
    args._argv = raw_argv
    if args.command == "prices" and args.prices_command == "fetch":
        output = Path(args.out)
        if output.exists() and not args.force:
            print(
                f"tamias prices fetch: refusing to overwrite {output}; "
                "pass --force",
                file=sys.stderr,
            )
            return 1
        try:
            fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
            result = fetch_models(_models_opener(), args.url)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_sheet(result.models, fetched_at, args.url), encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"tamias prices fetch: {exc}", file=sys.stderr)
            return 1
        reasons = Counter(entry.reason for entry in result.skipped)
        skipped = ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))
        free = sum(1 for price in result.models.values() if price.input == 0 and price.output == 0)
        print(f"models: {len(result.models)}")
        print(f"free: {free}")
        print(f"skipped by reason: {skipped or 'none'}")
        return 0
    if args.command == "prices" and args.prices_command == "set":
        return prices_set(args)
    if args.command == "prices" and args.prices_command == "zen":
        output = Path(args.out)
        if output.exists() and not args.force:
            print(
                f"tamias prices zen: refusing to overwrite {output}; pass --force",
                file=sys.stderr,
            )
            return 1
        try:
            fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
            model_ids = fetch_model_ids(_models_opener(), args.url)
            free = zen_free_models(model_ids)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                render_zen_sheet(free, fetched_at, args.url), encoding="utf-8"
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"tamias prices zen: {exc}", file=sys.stderr)
            return 1
        print(f"models: {len(model_ids)}")
        print(f"free priced at $0: {len(free)}")
        print(f"paid left UNKNOWN (rates not published here): {len(model_ids) - len(free)}")
        return 0
    if args.command == "opencode" and args.opencode_command == "install":
        return opencode_install(args)
    if args.command == "opencode" and args.opencode_command == "setup":
        return opencode_setup(args)
    if args.command == "opencode" and args.opencode_command == "uninstall":
        return opencode_uninstall(args)
    if args.command == "service" and args.service_command == "install":
        return service_install(args)
    if args.command == "service" and args.service_command == "uninstall":
        return service_uninstall(args)
    if args.command == "registry" and args.registry_command == "zen":
        output = Path(args.out)
        if output.exists() and not args.force:
            print(
                f"tamias registry zen: refusing to overwrite {output}; pass --force",
                file=sys.stderr,
            )
            return 1
        try:
            fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
            opener = _models_opener()
            model_ids = fetch_model_ids(opener, args.models_url)
            free = zen_free_models(model_ids)
            catalog = fetch_catalog(opener, args.catalog_url)
            build = build_zen_registry(catalog, free)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                render_registry(
                    build.models, fetched_at, args.models_url, args.catalog_url
                ),
                encoding="utf-8",
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"tamias registry zen: {exc}", file=sys.stderr)
            return 1
        print(f"free models live on Zen: {len(free)}")
        print(f"tool-capable, written: {len(build.models)}")
        if build.missing_capability:
            print(
                "no published capability (excluded): "
                + ", ".join(build.missing_capability)
            )
        if build.not_tool_capable:
            print(
                "tool_call is not true (excluded): "
                + ", ".join(build.not_tool_capable)
            )
        return 0
    if args.command == "report":
        try:
            report(
                args.db,
                args.prices,
                getattr(args, "inr_rate", None),
                routing_verdict=getattr(args, "routing_verdict", False),
            )
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
    if args.command == "agent-config":
        print(agent_config(args.agent, args.port, args.project, tuple(args.model or ())))
        return 0
    if args.command == "doctor":
        return doctor(
            prices=Path(args.prices),
            db=Path(args.db),
            port=args.port,
            online=args.online,
            upstream=args.upstream,
            explicit=_flag_given(args, "--prices"),
        )
    if args.command == "prune":
        return prune(args)
    if args.command == "config":
        return config_command(args)
    if args.command == "demo":
        return demo_command(args)
    if args.command == "reconcile":
        try:
            return reconcile_command(args)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            print(f"tamias reconcile: {exc}", file=sys.stderr)
            return 1
    if args.command == "admin":
        try:
            return admin_command(args)
        except (OSError, ValueError, sqlite3.DatabaseError, manifest_mod.ManifestError) as exc:
            print(f"tamias admin: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
