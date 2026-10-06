"""Frozen records shared by every part of tamias.

A None in any field of these records always means UNKNOWN.  It never means
zero: an upstream API that does not report a token count has not told us the
count was zero, and inventing a zero would silently understate cost.
"""

from dataclasses import dataclass
from typing import Literal

__all__ = ["Usage", "CostBreakdown", "Decision", "SessionState", "ToolClass"]

ToolClass = Literal["read", "edit", "shell", "unknown"]
"""Which family a tool name belongs to.

``unknown`` is also the answer for neutral names (todo, plan, task, think) and
for anything ambiguous or unrecognised: UNKNOWN never routes.
"""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts reported by the upstream API for one request.

    Any field may be None, which means the upstream did not report it.
    """

    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    cache_write_1h_tokens: int | None = None
    provider_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Cost of one request plus enough detail to audit it later.

    usd is None when the cost is UNKNOWN: either the model was missing from
    the price sheet or a needed token count or rate was missing.  formula
    always states the arithmetic in readable form, even when it cannot be
    evaluated.
    """

    usd: float | None
    formula: str
    price_sheet_date: str


@dataclass(frozen=True, slots=True)
class Decision:
    """A routing decision.

    persisted_only marks a decision that was recorded but not acted on, i.e. a
    shadow-mode decision: target_model then names the model that WOULD have
    been used, and action says nothing about what actually happened.
    """

    action: Literal["STAY", "SWITCH"]
    target_model: str | None
    reason: str
    persisted_only: bool = False


@dataclass(frozen=True, slots=True)
class SessionState:
    """Where one session currently sits in its routing sequence.

    Holds no conversation content: only the session identity, how many
    requests have been seen, and the model currently in use.
    """

    session_id: str
    request_index: int
    current_model: str
