"""Fetch and render a conservative TOML price sheet from an OpenRouter feed."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.request import urlopen

DEFAULT_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class FetchResult:
    """Rendered models and accounting for values deliberately left out."""

    models: dict[str, dict[str, float]]
    skipped: dict[str, int]

    @property
    def free(self) -> int:
        return sum(
            1 for rates in self.models.values() if all(value == 0 for value in rates.values())
        )


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def fetch_models(
    url: str = DEFAULT_URL, *, opener: Callable[..., Any] = urlopen
) -> FetchResult:
    """Load models from ``url`` without treating absent prices as free.

    The OpenRouter listing expresses rates as USD per token; Tamias sheets use
    USD per million tokens.  Models without a usable id, input, or output rate
    are omitted and counted by reason for an auditable fetch result.
    """
    with opener(url) as response:
        document = json.loads(response.read().decode("utf-8"))
    entries = document.get("data") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("price feed: expected a JSON object with a data list")
    models: dict[str, dict[str, float]] = {}
    skipped: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            skipped["invalid model record"] = skipped.get("invalid model record", 0) + 1
            continue
        name = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(name, str) or not name or not isinstance(pricing, dict):
            skipped["missing id or pricing"] = skipped.get("missing id or pricing", 0) + 1
            continue
        input_rate = _number(pricing.get("prompt"))
        output_rate = _number(pricing.get("completion"))
        if input_rate is None or output_rate is None:
            skipped["missing or invalid input/output rate"] = (
                skipped.get("missing or invalid input/output rate", 0) + 1
            )
            continue
        rates = {"input": input_rate * 1_000_000, "output": output_rate * 1_000_000}
        for source, target in (
            ("input_cache_read", "cached_input"),
            ("input_cache_write", "cache_write"),
        ):
            value = _number(pricing.get(source))
            if value is not None:
                rates[target] = value * 1_000_000
        models[name] = rates
    return FetchResult(models=models, skipped=skipped)


def render_sheet(result: FetchResult, *, today: date | None = None) -> str:
    """Return a deterministic, loadable TOML price sheet."""
    stamp = (today or datetime.now(UTC).date()).isoformat()
    lines = [f'date = "{stamp}"', ""]
    for name, rates in sorted(result.models.items()):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'["{escaped}"]')
        for field in ("input", "output", "cached_input", "cache_write"):
            if field in rates:
                lines.append(f"{field} = {rates[field]!r}")
        lines.append("")
    return "\n".join(lines)
