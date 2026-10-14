"""Reasoning-effort switching for chat completions request bodies."""

import copy
from typing import Any

#: The operator-facing scopes for the effort axis, kept separate from the
#: model-routing mode: effort can be metered or applied even though active model
#: routing (which rewrites the model) is disabled, so the two switches no longer
#: share one dead gate.
#:
#: ``off``   - compute nothing; the request is untouched.
#: ``meter`` - record the effort the policy would have used, without sending it.
#: ``apply`` - also send it, but only for a model the registry vouches for.
EFFORT_MODES: tuple[str, ...] = ("off", "meter", "apply")


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
