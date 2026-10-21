"""Price sheet loading and cost arithmetic."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tamias.types import CostBreakdown, Usage

__all__ = [
    "ModelPrice",
    "PriceSheet",
    "load_price_sheet",
    "can_price",
    "compute_cost",
    "assume_cache_warm",
]

TOKENS_PER_RATE_UNIT = 1_000_000
UNKNOWN = "?"
RATE_FIELDS = ("input", "output", "cached_input", "cache_write", "cache_write_1h")
# The only keys a `[provenance]` block may carry: who published the rates, at
# what URL, and when the sheet was fetched.  Anything else there would be a
# claim the report cannot audit, so it is refused rather than ignored.
PROVENANCE_FIELDS = ("source", "url", "fetched_at")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD price per 1,000,000 tokens. A None rate means UNKNOWN, not free.

    ``tiers`` holds the names of any tiered-pricing fields the sheet carried --
    long-context tiers and the like.  Their rates are not understood by the
    arithmetic, so an entry carrying one prices as UNKNOWN rather than at its
    base rate.
    """

    input: float | None = None
    output: float | None = None
    cached_input: float | None = None
    cache_write: float | None = None
    cache_write_1h: float | None = None
    tiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriceSheet:
    """Every model's prices, plus the date the sheet was published.

    ``simulated`` is the sheet author's own claim that these rates are made up.
    It changes no arithmetic: it is carried so that anything derived from the
    sheet can label its numbers as simulated rather than present them as money.
    """

    date: str
    models: dict[str, ModelPrice]
    simulated: bool = False
    source: str | None = None
    provenance: dict[str, str] | None = None

    def get(self, model: str) -> ModelPrice | None:
        return self.models.get(model)


def load_price_sheet(path: str | Path) -> PriceSheet:
    source = Path(path)
    document: dict[str, Any] = tomllib.loads(source.read_text(encoding="utf-8"))

    date = document.pop("date", None)
    if not isinstance(date, str) or not date:
        raise ValueError(f"{source}: missing top-level string `date`")

    simulated = document.pop("simulated", False)
    if not isinstance(simulated, bool):
        raise ValueError(
            f"{source}: top-level `simulated` must be true or false, got {simulated!r}"
        )

    provenance = _parse_provenance(source, document.pop("provenance", None))

    models = {model: _parse_model(source, model, table) for model, table in document.items()}
    return PriceSheet(
        date=date, models=models, simulated=simulated, source=str(source), provenance=provenance
    )


def _parse_provenance(source: Path, raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None  # a sheet written before provenance existed: no claim at all
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: `provenance` must be a table of strings, got {raw!r}")

    unexpected = sorted(set(raw) - set(PROVENANCE_FIELDS))
    if unexpected:
        names = ", ".join(str(key) for key in unexpected)
        raise ValueError(f"{source}: `provenance` has unknown key(s): {names}")
    for key, value in raw.items():
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"{source}: `provenance.{key}` must be a non-empty string, got {value!r}"
            )
    return {key: str(raw[key]) for key in PROVENANCE_FIELDS if key in raw}


def _is_tier_field(name: Any) -> bool:
    """Whether a model-table key declares a pricing tier rather than a rate.

    Tiered long-context pricing is not supported, so the only honest reading of
    ``tier_200k_input`` is "this model needs rates we do not model".  Every
    other unrecognised key stays an error: a typo must not become a silent
    UNKNOWN, it must fail loudly at load time.
    """
    return isinstance(name, str) and name.lower().startswith("tier")


def _parse_model(source: Path, model: str, table: Any) -> ModelPrice:
    if not isinstance(table, dict):
        raise ValueError(f"{source}: [{model}] must be a table of rates")

    tiers = tuple(sorted(key for key in table if _is_tier_field(key)))
    unexpected = sorted(set(table) - set(RATE_FIELDS) - set(tiers))
    if unexpected:
        raise ValueError(f"{source}: [{model}] has unknown rate(s): {', '.join(unexpected)}")

    rates: dict[str, float | None] = {}
    for field in RATE_FIELDS:
        raw = table.get(field)
        if raw is None:
            rates[field] = None
        elif isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ValueError(f"{source}: [{model}].{field} must be a number, got {raw!r}")
        else:
            value = float(raw)
            # A rate is a non-negative finite number.  Negative, NaN and infinite
            # rates are refused at load time rather than fed into arithmetic,
            # where a NaN would poison every sum and a -1 would invent a credit.
            # Zero stays a real rate throughout: it is distinct from a missing
            # rate everywhere, and that distinction is what the free-model rule
            # and the missing-cache-rate rule both rest on.
            if value < 0.0 or math.isnan(value) or math.isinf(value):
                raise ValueError(
                    f"{source}: [{model}].{field} must be a finite non-negative "
                    f"number, got {raw!r}"
                )
            rates[field] = value
    if rates.get("cache_write_1h") is None:
        rates["cache_write_1h"] = rates.get("cache_write")
    return ModelPrice(**rates, tiers=tiers)


def _show(value: float | int | None) -> str:
    return UNKNOWN if value is None else repr(value)


def can_price(model: str, sheet: PriceSheet) -> bool:
    """Whether :func:`compute_cost` can ever produce a USD for ``model``.

    A budget cannot be enforced by spending against a cost that is always
    UNKNOWN, so a key with a budget is refused a model this returns False for.
    The test mirrors the two ways ``compute_cost`` refuses: the model is absent
    from the sheet, its entry uses tiered pricing the arithmetic does not
    implement, or its base input/output rates are unquoted.  A quoted zero rate
    is priceable ($0.00, not unknown), and a missing cache rate does not matter
    here -- it can only make a specific usage unknown, while the reservation
    ceiling is bounded by the model's quoted rates.
    """
    price = sheet.get(model)
    if price is None or price.tiers:
        return False
    return price.input is not None and price.output is not None


def assume_cache_warm(usage: Usage) -> Usage:
    """The same request as if the prompt cache had already been warm.

    A warm cache serves the whole prompt from ``cached_input`` and writes
    nothing.  ``input_tokens`` is the total prompt, so the transform sets
    ``cached_input_tokens`` to it and zeroes the write counts; the difference
    between this and the request's own counts is the context a switch forced
    the new model to re-ingest.

    Pricing a *stay* model this way is the most favourable reading of "what
    staying would have cost", which makes the saving of switching the most
    conservative one: the difference can be zero or negative, so the instrument
    can report a failure instead of only ever confirming success (F-1).  A
    request that logged no ``input_tokens`` has no prompt to mark cached, so it
    is returned unchanged and the arithmetic reports UNKNOWN rather than
    guessing.
    """
    if usage.input_tokens is None:
        return usage
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.input_tokens,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        provider_cost_usd=usage.provider_cost_usd,
    )


def compute_cost(model: str, usage: Usage, sheet: PriceSheet) -> CostBreakdown:
    # Rule 0 begins here.  A provider-billed amount (usage.provider_cost_usd) is
    # a measurement, never a substitute for this arithmetic: it is carried out
    # separately in its own stored column, and the sheet calculation below is
    # computed independently whenever the sheet knows enough.  Treating the
    # billed number as the formula's result is exactly the conflation the
    # billing/computed separation exists to prevent.

    price = sheet.get(model)
    if price is None:
        return CostBreakdown(
            usd=None,
            formula=(
                f"usd = unknown: model {model!r} is not in price sheet {sheet.date}, "
                "so no rate is known"
            ),
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # A tiered entry (long-context tiers and the like) is priced by rules this
    # arithmetic does not implement.  Quoting its base rate would understate
    # some requests and overstate others, so the cost is UNKNOWN instead.
    if price.tiers:
        fields = ", ".join(price.tiers)
        return CostBreakdown(
            usd=None,
            formula=(
                f"usd = unknown: model {model!r} in price sheet {sheet.date} declares "
                f"tiered pricing field(s): {fields}; tiered pricing is unsupported"
            ),
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # Get effective prices
    input_p = price.input
    output_p = price.output
    cached_p = price.cached_input
    cw_p = price.cache_write
    cw1h_p = price.cache_write_1h if price.cache_write_1h is not None else price.cache_write

    # Input and output are present on every completion.  An unquoted base rate
    # is unknown, not a zero-dollar rate; short-circuit before the free-model
    # shortcut or arithmetic's ``or 0`` defaults can disguise that absence.
    missing_rates = []
    if input_p is None:
        missing_rates.append("input_rate")
    if output_p is None:
        missing_rates.append("output_rate")
    if missing_rates:
        expr = _build_expr(usage, input_p, output_p, cached_p, cw_p, cw1h_p)
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; unknown: {', '.join(missing_rates)}",
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # Rule 1b before the free-model shortcut: a cache rate the sheet never
    # quoted cannot price tokens that actually reached that bucket.  Falling
    # back to the input rate (or to zero) would invent a price, so the field is
    # named and the cost is left UNKNOWN.  A count of zero or an unreported
    # count touches no such tokens, so it costs nothing here and is settled by
    # the count rules below.  This check must run before Rule 1 so that a model
    # whose quoted base rates are all zero still prices as UNKNOWN, not $0.00,
    # when its unquoted cache buckets were actually used.
    missing_cache_rates = []
    if cached_p is None and usage.cached_input_tokens is not None:
        if usage.cached_input_tokens > 0:
            missing_cache_rates.append("cached_input_rate")
    if cw_p is None and usage.cache_write_tokens is not None:
        if usage.cache_write_tokens > 0:
            missing_cache_rates.append("cache_write_rate")
    if cw1h_p is None and usage.cache_write_1h_tokens is not None:
        if usage.cache_write_1h_tokens > 0:
            missing_cache_rates.append("cache_write_1h_rate")
    if missing_cache_rates:
        expr = _build_expr(usage, input_p, output_p, cached_p, cw_p, cw1h_p)
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; unknown: {', '.join(missing_cache_rates)}",
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # Rule 1: if every quoted price is 0, and Rule 1b found no unquoted bucket
    # in use, the model is genuinely free at the quoted buckets: $0.00 is the
    # exact number, not an absence.  Zero rates and missing rates stay distinct
    # by construction: a None rate never appears in `all_prices`, so a model
    # that never quoted its cache rate can only reach this rule when no tokens
    # touched that bucket (Rule 1b already priced the alternative).
    all_prices = [input_p, output_p, cached_p, cw_p, cw1h_p]
    if all(p == 0.0 for p in all_prices if p is not None):
        return CostBreakdown(
            usd=0.0,
            formula="free model: all quoted prices are 0 and no unquoted rate was used",
            price_sheet_date=sheet.date,
        )

    def is_nonzero(p: float | None) -> bool:
        return p is not None and p != 0.0

    # Rule 3
    cached_tokens = usage.cached_input_tokens
    if (
        cached_tokens is None
        and is_nonzero(cached_p)
        and is_nonzero(input_p)
        and cached_p != input_p
    ):
        expr = _build_expr(
            usage, input_p, output_p, cached_p, cw_p, cw1h_p, cached_f=0, uncached_f=None
        )
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; unknown: cached_input_tokens",
            price_sheet_date=sheet.date,
            source="unknown",
        )
    elif (
        cached_tokens is None
        and is_nonzero(cached_p)
        and is_nonzero(input_p)
        and cached_p == input_p
    ):
        cached_tokens_eff = 0
    elif cached_tokens is None:
        # price zero or not needed; treat as 0
        cached_tokens_eff = 0
    else:
        cached_tokens_eff = cached_tokens

    # Rule 4
    inconsistent = False
    if usage.input_tokens is not None:
        cw_sub = usage.cache_write_tokens if usage.cache_write_tokens is not None else 0
        c_sub = cached_tokens_eff if cached_tokens_eff is not None else 0
        uncached_cand = usage.input_tokens - c_sub - cw_sub
        if uncached_cand < 0 or c_sub + cw_sub > usage.input_tokens:
            inconsistent = True
    if (
        (usage.cache_write_tokens is not None and usage.cache_write_tokens < 0)
        or (usage.cache_write_1h_tokens is not None and usage.cache_write_1h_tokens < 0)
        or (cached_tokens is not None and cached_tokens < 0)
        or (usage.input_tokens is not None and usage.input_tokens < 0)
        or (usage.output_tokens is not None and usage.output_tokens < 0)
    ):
        inconsistent = True

    if inconsistent:
        expr = _build_expr(
            usage,
            input_p,
            output_p,
            cached_p,
            cw_p,
            cw1h_p,
            cached_f=cached_tokens_eff,
            uncached_f=None,
        )
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; inconsistent usage",
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # Rule 2: determine missing needed fields
    missing = []
    if is_nonzero(input_p):
        if usage.input_tokens is None:
            missing.append("input_tokens")
        # need cached_input_tokens if cached price nonzero and differs from input price?
        if is_nonzero(cached_p) and cached_p != input_p:
            if cached_tokens is None:
                missing.append("cached_input_tokens")
    if is_nonzero(cached_p):
        if cached_tokens is None:
            if not (is_nonzero(input_p) and cached_p == input_p):
                missing.append("cached_input_tokens")
    if is_nonzero(output_p):
        if usage.output_tokens is None:
            missing.append("output_tokens")
    if is_nonzero(cw_p) or is_nonzero(cw1h_p):
        if usage.cache_write_tokens is None:
            missing.append("cache_write_tokens")
    # cache_write_1h_tokens is an optional subset of cache_write_tokens, so it is
    # never "missing": an absent count means zero 1h writes, not an unknown cost.
    if usage.input_tokens is None and input_p is not None:
        # Even if input price is 0, we still need to know how many tokens to
        # subtract when cache/write buckets are priced or when the formula needs
        # to balance the input total. Without it, arithmetic would crash or
        # invent numbers.
        if cached_tokens is None or is_nonzero(cached_p) or is_nonzero(cw_p) or is_nonzero(cw1h_p):
            if "input_tokens" not in missing:
                missing.append("input_tokens")

    if missing:
        expr = _build_expr(
            usage,
            input_p,
            output_p,
            cached_p,
            cw_p,
            cw1h_p,
            cached_f=cached_tokens_eff,
            uncached_f=None,
        )
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; unknown: {', '.join(missing)}",
            price_sheet_date=sheet.date,
            source="unknown",
        )

    # Compute
    cached_calc = cached_tokens if cached_tokens is not None else cached_tokens_eff
    if (
        cached_tokens is None
        and is_nonzero(cached_p)
        and is_nonzero(input_p)
        and cached_p == input_p
    ):
        cached_calc = 0
    elif cached_tokens is None:
        cached_calc = 0  # price zero or not nonzero case handled

    cw_total = usage.cache_write_tokens if usage.cache_write_tokens is not None else 0
    cw1h = usage.cache_write_1h_tokens if usage.cache_write_1h_tokens is not None else 0
    writes_5m = cw_total - cw1h
    writes_1h_res = cw1h

    # recompute uncached
    if usage.input_tokens is None:
        expr = _build_expr(
            usage,
            input_p,
            output_p,
            cached_p,
            cw_p,
            cw1h_p,
            cached_f=cached_calc,
            uncached_f=None,
        )
        return CostBreakdown(
            usd=None,
            formula=f"{expr}; unknown: input_tokens",
            price_sheet_date=sheet.date,
            source="unknown",
        )
    uncached_calc = usage.input_tokens - cached_calc - cw_total  # type: ignore

    cost = (
        uncached_calc * (input_p or 0)
        + cached_calc * (cached_p or 0)
        + writes_5m * (cw_p or 0)
        + writes_1h_res * (cw1h_p or 0)
        + (usage.output_tokens or 0) * (output_p or 0)
    ) / TOKENS_PER_RATE_UNIT

    expr = _build_expr(
        usage,
        input_p,
        output_p,
        cached_p,
        cw_p,
        cw1h_p,
        cached_f=cached_calc,
        uncached_f=uncached_calc,
        w5m=writes_5m,
        w1h=writes_1h_res,
    )
    return CostBreakdown(usd=cost, formula=f"{expr} = {_show(cost)}", price_sheet_date=sheet.date)


def _build_expr(
    usage: Usage,
    input_p: float | None,
    output_p: float | None,
    cached_p: float | None,
    cw_p: float | None,
    cw1h_p: float | None,
    cached_f: int | None = None,
    uncached_f: int | None = None,
    w5m: int | None = None,
    w1h: int | None = None,
) -> str:
    if uncached_f is None and usage.input_tokens is not None:
        c = cached_f if cached_f is not None else usage.cached_input_tokens or 0
        cw = usage.cache_write_tokens if usage.cache_write_tokens is not None else 0
        uncached_f = usage.input_tokens - c - cw
    if w5m is None:
        w5m = (usage.cache_write_tokens or 0) - (usage.cache_write_1h_tokens or 0)
    if w1h is None:
        w1h = usage.cache_write_1h_tokens or 0
    cached_disp = cached_f if cached_f is not None else usage.cached_input_tokens
    terms = []
    terms.append(f"uncached_input {_show(uncached_f)} * {_show(input_p)}")
    terms.append(f"cached_input {_show(cached_disp)} * {_show(cached_p)}")
    terms.append(f"5m_writes {_show(w5m)} * {_show(cw_p)}")
    terms.append(f"1h_writes {_show(w1h)} * {_show(cw1h_p)}")
    terms.append(f"output {_show(usage.output_tokens)} * {_show(output_p)}")
    expression = " + ".join(terms)
    return f"usd = ({expression}) / {TOKENS_PER_RATE_UNIT}"
