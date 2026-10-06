"""Reasoning-effort switching for chat completions request bodies."""

import copy
from typing import Any


def apply_effort(
    body: dict[str, Any], effort: str | None, style: str = "openrouter"
) -> dict[str, Any]:
    """Return a new body dict with the effort field set.

    Parameters:
        body: the original request body dict (never mutated).
        effort: one of "low", "medium", "high", or None.
        style: "openrouter" sets body["reasoning"]["effort"];
               "openai" sets body["reasoning_effort"].

    Returns a new dict; the input body is never mutated.
    """
    if effort is None:
        return body
    new = copy.deepcopy(body)
    if style == "openrouter":
        r = new.get("reasoning")
        r = dict(r) if isinstance(r, dict) else {}
        r["effort"] = effort
        new["reasoning"] = r
    elif style == "openai":
        new["reasoning_effort"] = effort
    else:
        raise ValueError(f"unknown effort style: {style}")
    return new
