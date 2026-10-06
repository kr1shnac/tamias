"""Turn OpenRouter's model catalogue into tamias price-sheet entries.

Nothing in this module opens a connection on its own: :func:`fetch_models`
takes the opener as an argument and :func:`parse_models` takes the already
decoded JSON, so every test runs against a fixture and no test can reach the
network.

Rates on the wire are USD **per token**, strings or numbers alike.  tamias
stores USD **per 1,000,000 tokens**, and the conversion is done in
:class:`decimal.Decimal` so that a price published as ``0.00000055`` becomes
exactly ``0.55`` rather than whatever the nearest binary float happens to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from tamias.pricing import ModelPrice

__all__ = ["OPENROUTER_MODELS_URL", "ParseResult", "Skipped", "parse_models"]

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PROVENANCE_SOURCE = "openrouter-models-api"

TOKENS_PER_RATE_UNIT = Decimal(1_000_000)

# The catalogue's per-token price keys and the sheet field each one feeds.
PROMPT_KEY = "prompt"
COMPLETION_KEY = "completion"
CACHE_READ_KEY = "input_cache_read"
CACHE_WRITE_KEY = "input_cache_write"
REQUIRED_KEYS = (PROMPT_KEY, COMPLETION_KEY)
CACHE_KEYS = (CACHE_READ_KEY, CACHE_WRITE_KEY)
FIELD_FOR_KEY = {
    PROMPT_KEY: "input",
    COMPLETION_KEY: "output",
    CACHE_READ_KEY: "cached_input",
    CACHE_WRITE_KEY: "cache_write",
}


@dataclass(frozen=True, slots=True)
class Skipped:
    """One catalogue entry that could not become a sheet entry, and why."""

    model: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Sheet-ready prices plus the entries that were dropped on the way."""

    models: dict[str, ModelPrice] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)


def parse_models(json_obj: Any) -> ParseResult:
    """Parse OpenRouter's ``/api/v1/models`` payload into sheet entries.

    Entries are processed in the order the catalogue lists them: a repeated id
    keeps its first listing, and anything unusable -- a missing id or price, a
    price that is not a number, a negative price -- is skipped with the reason
    recorded in ``skipped`` rather than guessed at.
    """
    if not isinstance(json_obj, dict):
        raise ValueError("models payload must be a JSON object with a `data` list")
    data = json_obj.get("data")
    if not isinstance(data, list):
        raise ValueError("models payload has no `data` list")

    result = ParseResult()
    for entry in data:
        if not isinstance(entry, dict):
            result.skipped.append(Skipped(model=str(entry), reason="entry is not a JSON object"))
            continue
        if "id" not in entry:
            result.skipped.append(
                Skipped(model="<missing id>", reason="entry has no string `id`")
            )
            continue
        model_id = entry["id"]
        if not isinstance(model_id, str) or not model_id:
            result.skipped.append(
                Skipped(model=str(model_id), reason="entry has no non-empty string `id`")
            )
            continue
        if model_id in result.models:
            result.skipped.append(
                Skipped(model=model_id, reason="duplicate id: the first entry was kept")
            )
            continue
        pricing = entry.get("pricing")
        if not isinstance(pricing, dict):
            result.skipped.append(Skipped(model=model_id, reason="missing `pricing` object"))
            continue
        price, reason = _price_from(pricing)
        if price is None:
            result.skipped.append(Skipped(model=model_id, reason=str(reason)))
            continue
        result.models[model_id] = price
    return result


def _price_from(pricing: dict[str, Any]) -> tuple[ModelPrice | None, str | None]:
    """Build one entry, or explain why the entry cannot be trusted."""
    per_million: dict[str, Decimal] = {}
    for key in REQUIRED_KEYS:
        if key not in pricing:
            return None, f"missing `pricing.{key}`"
        value, reason = _per_million(pricing[key], key)
        if value is None:
            return None, reason
        per_million[key] = value
    for key in CACHE_KEYS:
        if key not in pricing:
            continue  # no published cache price: the sheet field stays omitted
        value, reason = _per_million(pricing[key], key)
        if value is None:
            return None, reason
        per_million[key] = value

    if all(value == 0 for value in per_million.values()):
        # Every published rate is zero: the model is free, and its cache rates
        # are real zeros rather than prices nobody happened to publish.
        cached_input, cache_write = 0.0, 0.0
    else:
        cached_input = (
            float(per_million[CACHE_READ_KEY]) if CACHE_READ_KEY in per_million else None
        )
        cache_write = (
            float(per_million[CACHE_WRITE_KEY]) if CACHE_WRITE_KEY in per_million else None
        )
    return (
        ModelPrice(
            input=float(per_million[PROMPT_KEY]),
            output=float(per_million[COMPLETION_KEY]),
            cached_input=cached_input,
            cache_write=cache_write,
        ),
        None,
    )


def _per_million(raw: Any, key: str) -> tuple[Decimal | None, str | None]:
    """One published per-token price, converted to USD per million tokens."""
    label = f"`pricing.{key}`"
    if isinstance(raw, bool) or raw is None:
        return None, f"{label} is not a number: {raw!r}"
    if isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            return None, f"{label} is not a number: {raw!r}"
    elif isinstance(raw, int | float):
        value = Decimal(str(raw))
    else:
        return None, f"{label} is not a number: {raw!r}"
    if not value.is_finite():
        return None, f"{label} is not a finite number: {raw!r}"
    if value < 0:
        return None, f"{label} is negative: {raw!r}"
    return value * TOKENS_PER_RATE_UNIT, None
