"""Price sheet loading and cost arithmetic."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tamias.types import CostBreakdown, Usage

__all__ = ["ModelPrice", "PriceSheet", "load_price_sheet", "compute_cost"]

TOKENS_PER_RATE_UNIT = 1_000_000
UNKNOWN = "?"
RATE_FIELDS = ("input", "output", "cached_input", "cache_write", "cache_write_1h")
# The only keys a `[provenance]` block may carry: who published the rates, at
# what URL, and when the sheet was fetched.  Anything else there would be a
# claim the report cannot audit, so it is refused rather than ignored.
PROVENANCE_FIELDS = ("source", "url", "fetched_at")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD price per 1,000,000 tokens. A None rate means UNKNOWN, not free."""

    input: float | None = None
    output: float | None = None
    cached_input: float | None = None
    cache_write: float | None = None
    cache_write_1h: float | None = None


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


def _parse_model(source: Path, model: str, table: Any) -> ModelPrice:
    if not isinstance(table, dict):
        raise ValueError(f"{source}: [{model}] must be a table of rates")

    unexpected = sorted(set(table) - set(RATE_FIELDS))
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
            rates[field] = float(raw)
    if rates.get("cache_write_1h") is None:
        rates["cache_write_1h"] = rates.get("cache_write")
    return ModelPrice(**rates)


def _show(value: float | int | None) -> str:
    return UNKNOWN if value is None else repr(value)


def compute_cost(model: str, usage: Usage, sheet: PriceSheet) -> CostBreakdown:
    price = sheet.get(model)
    if price is None:
        return CostBreakdown(
            usd=None,
            formula=(
                f"usd = unknown: model {model!r} is not in price sheet {sheet.date}, "
                "so no rate is known"
            ),
            price_sheet_date=sheet.date,
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
        )

    # Rule 0 is model missing (done). Rule 1: if every price is 0
    all_prices = [input_p, output_p, cached_p, cw_p, cw1h_p]
    if all(p == 0.0 for p in all_prices if p is not None):
        return CostBreakdown(
            usd=0.0,
            formula="free model: all prices are 0",
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
            usd=None, formula=f"{expr}; unknown: cached_input_tokens", price_sheet_date=sheet.date
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
            usd=None, formula=f"{expr}; inconsistent usage", price_sheet_date=sheet.date
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
        final=True,
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
    final: bool = False,
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
