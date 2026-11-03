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

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from tamias.pricing import RATE_FIELDS, ModelPrice

__all__ = [
    "OPENROUTER_MODELS_URL",
    "PROVENANCE_SOURCE",
    "ZEN_KNOWN_FREE",
    "ZEN_MODELS_URL",
    "ZEN_PROVENANCE_SOURCE",
    "ParseResult",
    "Skipped",
    "fetch_model_ids",
    "fetch_models",
    "parse_models",
    "render_sheet",
    "render_zen_sheet",
    "zen_free_models",
]

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PROVENANCE_SOURCE = "openrouter-models-api"

ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
ZEN_PROVENANCE_SOURCE = "opencode-zen-models-api"

ZEN_KNOWN_FREE = frozenset({"big-pickle"})
"""Zen models documented as free whose id carries no ``-free`` suffix.

The suffix is the published marker; this set carries the documented exception
rather than guessing from a model name.
"""

TOKENS_PER_RATE_UNIT = Decimal(1_000_000)

# The catalogue's per-token price keys and the sheet field each one feeds.
PROMPT_KEY = "prompt"
COMPLETION_KEY = "completion"
CACHE_READ_KEY = "input_cache_read"
CACHE_WRITE_KEY = "input_cache_write"
REQUIRED_KEYS = (PROMPT_KEY, COMPLETION_KEY)
CACHE_KEYS = (CACHE_READ_KEY, CACHE_WRITE_KEY)


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


def render_sheet(models: dict[str, ModelPrice], fetched_at: str, source_url: str) -> str:
    """A whole price sheet for ``models``, in the schema the loader reads.

    The sheet declares ``simulated = false`` because every rate in it was
    published rather than invented, and a ``provenance`` table naming the API
    it came from and when it was fetched.  Models render in id order so the
    same catalogue always produces the same bytes, and a rate the catalogue
    did not publish is left out of the sheet rather than written as zero.
    """
    if not isinstance(fetched_at, str) or not fetched_at:
        raise ValueError("fetched_at must be a non-empty timestamp string")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url must be a non-empty URL string")

    date = fetched_at.split("T", 1)[0] if "T" in fetched_at else fetched_at
    lines = [
        "# Price sheet generated by `tamias prices fetch` from OpenRouter's models API.",
        "# Every rate is USD per 1,000,000 tokens.  A rate left out means the price is",
        "# UNKNOWN, never zero.  Do not edit by hand: re-run the fetch instead.",
        "",
        f"date = {_string(date)}",
        "simulated = false",
        "",
        "[provenance]",
        f"source = {_string(PROVENANCE_SOURCE)}",
        f"url = {_string(source_url)}",
        f"fetched_at = {_string(fetched_at)}",
    ]
    for model_id in sorted(models):
        price = models[model_id]
        lines.append("")
        lines.append(f"[{_string(model_id)}]")
        for name in RATE_FIELDS:
            value = getattr(price, name)
            if value is not None:
                lines.append(f"{name} = {float(value)!r}")
        # Tier fields are written back out rather than dropped: losing one
        # would turn an entry that prices as UNKNOWN into an entry that prices
        # confidently at its base rate.  The value is a marker, not a price.
        for tier in price.tiers:
            lines.append(f"{_string(tier)} = {_string('unsupported')}")
    return "\n".join(lines) + "\n"


def _string(value: str) -> str:
    """Quote a string for TOML: JSON's escapes are a subset of TOML's."""
    return json.dumps(value, ensure_ascii=True)


def fetch_models(opener: Any, url: str = OPENROUTER_MODELS_URL) -> ParseResult:
    """Fetch the catalogue through ``opener`` and parse what comes back.

    The opener is an argument rather than a global so that no caller -- and in
    particular no test -- has to touch the network to exercise this path.
    """
    response = opener.open(url)
    try:
        raw = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return parse_models(json.loads(raw))


def fetch_model_ids(opener: Any, url: str = ZEN_MODELS_URL) -> list[str]:
    """Fetch an OpenAI-style ``/v1/models`` list and return the ids in order.

    Zen publishes model ids only -- no rates -- so only ids are read.  The
    opener is an argument for the same reason as in :func:`fetch_models`: no
    test should have to touch the network.
    """
    response = opener.open(url)
    try:
        raw = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    payload = json.loads(raw)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("model list has no 'data' array")
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return ids


def zen_free_models(model_ids: Iterable[str]) -> list[str]:
    """The ids Zen documents as free, in sorted order.

    A ``-free``/``:free`` suffix is the published marker; ``ZEN_KNOWN_FREE``
    carries the documented exceptions whose id does not say it.  Nothing is
    inferred from a model's name or size.
    """
    return sorted(
        {
            model_id
            for model_id in model_ids
            if model_id.endswith(("-free", ":free")) or model_id in ZEN_KNOWN_FREE
        }
    )


def render_zen_sheet(
    free_models: Iterable[str], fetched_at: str, source_url: str
) -> str:
    """A price sheet for the free Zen models, with paid models deliberately absent.

    Every free model gets explicit zero rates, so it prices as ``$0.00`` rather
    than UNKNOWN.  Paid models are **not** written: Zen does not publish their
    rates on the models endpoint, and an absent rate means UNKNOWN, never a
    guessed dollar figure.
    """
    if not isinstance(fetched_at, str) or not fetched_at:
        raise ValueError("fetched_at must be a non-empty timestamp string")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url must be a non-empty URL string")

    date = fetched_at.split("T", 1)[0] if "T" in fetched_at else fetched_at
    lines = [
        "# Price sheet generated by `tamias prices zen` from OpenCode Zen's models API.",
        "# Free models are priced at $0.  Paid models are deliberately absent: Zen",
        "# does not publish their rates here, and an absent rate means UNKNOWN, never $0.",
        "",
        f"date = {_string(date)}",
        "simulated = false",
        "",
        "[provenance]",
        f"source = {_string(ZEN_PROVENANCE_SOURCE)}",
        f"url = {_string(source_url)}",
        f"fetched_at = {_string(fetched_at)}",
    ]
    for model_id in sorted(set(free_models)):
        lines.extend(
            (
                "",
                f"[{_string(model_id)}]",
                "input = 0.0",
                "output = 0.0",
                "cached_input = 0.0",
                "cache_write = 0.0",
            )
        )
    return "\n".join(lines) + "\n"
