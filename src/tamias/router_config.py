"""Router configuration profiles and TOML loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from tamias.router import EASY_TOOLS, ERROR_MARKERS, RouterConfig

GENERIC_ERROR_MARKERS = ERROR_MARKERS + ("Error:",)


def _string_set(
    document: dict[str, Any], key: str, default: frozenset[str] | tuple[str, ...]
) -> frozenset[str]:
    value = document.get(key, default)
    if not isinstance(value, list | tuple | frozenset) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"router config: {key} must be a list of non-empty strings")
    return frozenset(value)


def load_router_config(path: str | Path | None = None, profile: str = "generic") -> RouterConfig:
    """Load a profile plus optional TOML overrides.

    ``legacy`` retains the historical exact marker set.  ``generic`` additionally
    treats the common tool convention ``Error: ...`` as a failed tool response.
    """
    if profile not in {"generic", "legacy"}:
        raise ValueError(f"router config: unknown profile {profile!r}")
    document: dict[str, Any] = {}
    if path is not None:
        parsed = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("router config: expected a TOML table")
        document = parsed.get("router", parsed)
        if not isinstance(document, dict):
            raise ValueError("router config: [router] must be a table")
    allowed = {"easy_tools", "error_markers", "cheap_model", "strong_model", "min_gap"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ValueError(f"router config: unknown key(s): {', '.join(unknown)}")
    default_markers = GENERIC_ERROR_MARKERS if profile == "generic" else ERROR_MARKERS
    min_gap = document.get("min_gap", 3)
    if isinstance(min_gap, bool) or not isinstance(min_gap, int) or min_gap < 0:
        raise ValueError("router config: min_gap must be a non-negative integer")
    models = {key: document.get(key, "") for key in ("cheap_model", "strong_model")}
    if not all(isinstance(value, str) for value in models.values()):
        raise ValueError("router config: model names must be strings")
    return RouterConfig(
        easy_tools=_string_set(document, "easy_tools", EASY_TOOLS),
        error_markers=_string_set(document, "error_markers", default_markers),
        cheap_model=models["cheap_model"],
        strong_model=models["strong_model"],
        min_gap=min_gap,
    )
