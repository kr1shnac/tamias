"""Row-by-row comparison of computed cost and provider-billed cost.

This module is deliberately pure: it takes rows already read from the request
log, compares the two numbers that matter, and never invents a billed amount.
The CLI owns database access, key handling and formatting.

The comparison is intentionally asymmetric.  ``computed_list_cost_usd`` is the
number Tamias derived from a price sheet; ``provider_cost_usd`` is the number a
provider reported, or ``lookup_cost_usd`` when the provider's generation lookup
was queried.  A row with only one of the two is *incomplete*, never a match and
never ``$0.00``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

#: The one documented lookup used by ``--online``.  It reads an existing
#: generation; it never creates a completion.
LOOKUP_URL = "https://openrouter.ai/api/v1/generation"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_TOLERANCE = 1e-6

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
EMPTY = "EMPTY"


@dataclass(frozen=True, slots=True)
class ComparedRow:
    """One logged request and the two costs being compared."""

    request: int | None
    ts: str | None
    session_id: str | None
    model_requested: str | None
    model_used: str | None
    generation_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    computed: float | None
    billed: float | None
    difference: float | None
    matches: bool | None
    billed_source: str | None
    logged_billed: float | None
    lookup_billed: float | None


@dataclass(frozen=True, slots=True)
class Summary:
    """The outcome of comparing a set of rows."""

    status: str
    rows: int
    compared: int
    matched: int
    mismatched: int
    without_billed: int
    without_computed: int
    computed_usd: float
    billed_usd: float
    difference_usd: float

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready summary; every amount is USD and never a formatted string."""
        return {
            "status": self.status,
            "rows": self.rows,
            "compared": self.compared,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "without_billed": self.without_billed,
            "without_computed": self.without_computed,
            "computed_usd": self.computed_usd,
            "billed_usd": self.billed_usd,
            "difference_usd": self.difference_usd,
        }


def _number(value: Any) -> float | None:
    """Return a finite float, or None for anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _computed(row: dict[str, Any]) -> float | None:
    """The list-price computation, from the explicitly named column only.

    A pre-split log stored an ambiguous value in ``cost_usd``.  It might be a
    computation or a provider cost, so it is treated as UNKNOWN rather than
    compared against a provider number it may already equal.
    """
    return _number(row.get("computed_list_cost_usd"))


def compare_rows(
    rows: Sequence[dict[str, Any]], tolerance: float = DEFAULT_TOLERANCE
) -> list[ComparedRow]:
    """Compare billed against computed for each row without inventing values.

    ``lookup_cost_usd`` wins over the stored ``provider_cost_usd`` when present:
    the lookup is the provider's own record, while the stored value came from
    the response Tamias handled.  Both are kept on the result so an artifact can
    show the two sources separately.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be zero or greater")
    compared: list[ComparedRow] = []
    for row in rows:
        computed = _computed(row)
        logged = _number(row.get("provider_cost_usd"))
        lookup = _number(row.get("lookup_cost_usd"))
        if lookup is not None:
            billed = lookup
            source = "lookup"
        elif logged is not None:
            billed = logged
            source = "logged"
        else:
            billed = None
            source = None
        difference = billed - computed if billed is not None and computed is not None else None
        compared.append(
            ComparedRow(
                request=_integer(row.get("id")),
                ts=_text(row.get("ts")),
                session_id=_text(row.get("session_id")),
                model_requested=_text(row.get("model_requested")),
                model_used=_text(row.get("model_used")),
                generation_id=_text(row.get("generation_id")),
                prompt_tokens=_integer(row.get("input_tokens")),
                completion_tokens=_integer(row.get("output_tokens")),
                computed=computed,
                billed=billed,
                difference=difference,
                matches=abs(difference) <= tolerance if difference is not None else None,
                billed_source=source,
                logged_billed=logged,
                lookup_billed=lookup,
            )
        )
    return compared


def summarize(rows: Sequence[ComparedRow]) -> Summary:
    """Classify a comparison.

    A mismatch always wins: even one row billed differently makes the whole
    reconciliation FAIL, no matter how many other rows are incomplete.  If
    nothing mismatched but any row lacks a number (or there was nothing to
    compare), the result is INCOMPLETE -- never a PASS on unproven rows.
    """
    if not rows:
        return Summary(EMPTY, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0)

    compared = [row for row in rows if row.matches is not None]
    matched = sum(1 for row in rows if row.matches is True)
    mismatched = sum(1 for row in rows if row.matches is False)
    without_billed = sum(1 for row in rows if row.billed is None)
    without_computed = sum(1 for row in rows if row.computed is None)

    if mismatched:
        status = FAIL
    elif not compared or without_billed or without_computed:
        status = INCOMPLETE
    else:
        status = PASS

    computed_usd = sum(row.computed for row in compared if row.computed is not None)
    billed_usd = sum(row.billed for row in compared if row.billed is not None)
    difference_usd = sum(row.difference for row in compared if row.difference is not None)
    return Summary(
        status=status,
        rows=len(rows),
        compared=len(compared),
        matched=matched,
        mismatched=mismatched,
        without_billed=without_billed,
        without_computed=without_computed,
        computed_usd=computed_usd,
        billed_usd=billed_usd,
        difference_usd=difference_usd,
    )


def _row_date(row: dict[str, Any]) -> date | None:
    raw = row.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def select_rows(
    rows: Iterable[dict[str, Any]],
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Select rows whose timestamp falls inside an inclusive calendar window.

    ``since`` and ``until`` are ``YYYY-MM-DD`` dates, matching the flag
    documentation.  A row whose timestamp cannot be parsed is excluded from a
    bounded window rather than guessed into it; with no window every row is
    returned.
    """
    lower = date.fromisoformat(since) if since else None
    upper = date.fromisoformat(until) if until else None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError(f"--since {since} is after --until {until}")
    if lower is None and upper is None:
        return list(rows)

    selected: list[dict[str, Any]] = []
    for row in rows:
        stamp = _row_date(row)
        if stamp is None:
            continue
        if lower is not None and stamp < lower:
            continue
        if upper is not None and stamp > upper:
            continue
        selected.append(row)
    return selected


def lookup_generation_costs(
    rows: Iterable[dict[str, Any]], *, client: httpx.Client
) -> tuple[dict[str, float], int]:
    """Read ``total_cost`` for each distinct generation id in the rows.

    Only the documented generation endpoint is called.  A missing id, a
    transport failure, a non-2xx response, or a response without a numeric
    ``data.total_cost`` leaves that generation out of the result and increments
    the error count.  The caller decides whether an unknown cost is acceptable;
    this function never substitutes zero.
    """
    generation_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        generation_id = _text(row.get("generation_id"))
        if generation_id and generation_id not in seen:
            seen.add(generation_id)
            generation_ids.append(generation_id)

    costs: dict[str, float] = {}
    errors = 0
    for generation_id in generation_ids:
        try:
            response = client.get(LOOKUP_URL, params={"id": generation_id})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            errors += 1
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        cost = _number(data.get("total_cost")) if isinstance(data, dict) else None
        if cost is None:
            errors += 1
            continue
        costs[generation_id] = cost
    return costs, errors
