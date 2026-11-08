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

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

#: The one documented lookup used by ``--online``.  It reads an existing
#: generation; it never creates a completion.
LOOKUP_URL = "https://openrouter.ai/api/v1/generation"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_TOLERANCE = 1e-6

#: OpenRouter's generation ids are the only ids its lookup endpoint knows.
#: Other wires use their own ids (``chatcmpl-``, ``msg_``, ...), and sending
#: those to OpenRouter would count a predictable 404 as a failed verification.
OPENROUTER_GENERATION_PREFIX = "gen-"

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
EMPTY = "EMPTY"

INVOICE_JOINS = ("request_id", "generation_id")
INVOICE_SCOPES = ("row", "day")


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
    invoice_billed: float | None = None


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


def is_openrouter_generation_id(value: Any) -> bool:
    """Whether ``value`` is an id the OpenRouter generation endpoint accepts."""
    return isinstance(value, str) and value.startswith(OPENROUTER_GENERATION_PREFIX)


def _computed(row: dict[str, Any]) -> float | None:
    """The list-price computation, from the explicitly named column only.

    A pre-split log stored an ambiguous value in ``cost_usd``.  It might be a
    computation or a provider cost, so it is treated as UNKNOWN rather than
    compared against a provider number it may already equal.
    """
    return _number(row.get("computed_list_cost_usd"))


def compare_rows(
    rows: Sequence[dict[str, Any]],
    tolerance: float = DEFAULT_TOLERANCE,
    *,
    invoice_only: bool = False,
) -> list[ComparedRow]:
    """Compare billed against computed for each row without inventing values.

    ``invoice_cost_usd`` wins over the lookup and the stored
    ``provider_cost_usd`` when present: an invoice is the provider's final
    record.  With ``invoice_only`` the invoice is the *only* billed side, so a
    row the invoice does not cover stays UNKNOWN instead of falling back to a
    response-reported number.  That keeps an invoice proof from mixing sources.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be zero or greater")
    compared: list[ComparedRow] = []
    for row in rows:
        computed = _computed(row)
        logged = _number(row.get("provider_cost_usd"))
        lookup = _number(row.get("lookup_cost_usd"))
        invoice = _number(row.get("invoice_cost_usd"))
        if invoice_only:
            billed = invoice
            source = "invoice" if invoice is not None else None
        elif invoice is not None:
            billed = invoice
            source = "invoice"
        elif lookup is not None:
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
                invoice_billed=invoice,
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
    """Read ``total_cost`` for each distinct OpenRouter generation id in the rows.

    Only the documented generation endpoint is called, and only for ids that
    start with ``gen-``.  Other providers' response ids are not OpenRouter
    generations; they are skipped rather than sent to an endpoint that cannot
    know them.  A transport failure, a non-2xx response, or a response without
    a numeric ``data.total_cost`` leaves that generation out of the result and
    increments the error count.  The caller decides whether an unknown cost is
    acceptable; this function never substitutes zero.
    """
    generation_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        generation_id = _text(row.get("generation_id"))
        if (
            generation_id
            and is_openrouter_generation_id(generation_id)
            and generation_id not in seen
        ):
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


@dataclass(frozen=True, slots=True)
class Invoice:
    """A provider invoice read as ``key -> USD`` plus its line count."""

    costs: Mapping[str, float]
    lines: int
    duplicates: int


def _invoice_amount(value: Any) -> float | None:
    """A non-negative USD amount from a CSV cell, or None.

    Export files commonly carry ``$`` and thousands separators.  Anything else
    -- a blank cell, a currency code, a negative credit, NaN -- is refused so a
    malformed invoice cannot quietly become a smaller bill.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace("$", "").replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def load_invoice(
    path: str | Path,
    *,
    key_column: str = "request_id",
    cost_column: str = "cost_usd",
) -> Invoice:
    """Read a line-item CSV from any provider's billing export.

    The file must carry one identity column and one USD amount column.  The
    identity is matched by the caller against Tamias's row id or the provider's
    generation id; this function does not guess which one it is.  Duplicate keys
    are summed because one request can be split across several SKUs (input,
    output, cache).  A missing header, an empty key, or an unreadable amount is
    an error, never a skipped line.
    """
    source = Path(path)
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [name.strip() for name in (reader.fieldnames or []) if name]
        reader.fieldnames = fields
        missing = [name for name in (key_column, cost_column) if name not in fields]
        if missing:
            raise ValueError(
                f"{source}: missing column(s) {', '.join(missing)}; "
                f"found {', '.join(fields) or '<none>'}"
            )

        costs: dict[str, float] = {}
        lines = 0
        duplicates = 0
        for number, row in enumerate(reader, start=2):
            raw_key = row.get(key_column)
            key = raw_key.strip() if isinstance(raw_key, str) else ""
            if not key:
                raise ValueError(f"{source}:{number}: empty {key_column}")
            cost = _invoice_amount(row.get(cost_column))
            if cost is None:
                raise ValueError(
                    f"{source}:{number}: {cost_column} is not a "
                    "non-negative USD amount"
                )
            lines += 1
            if key in costs:
                duplicates += 1
                costs[key] += cost
            else:
                costs[key] = cost

    if lines == 0:
        raise ValueError(f"{source}: no invoice line items")
    return Invoice(costs=costs, lines=lines, duplicates=duplicates)


def apply_invoice(
    rows: Sequence[dict[str, Any]],
    costs: Mapping[str, float],
    *,
    key: str = "request_id",
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Attach invoice amounts to rows by row id or provider generation id.

    Returns the enriched rows, the number of matched keys, and the invoice keys
    that matched no selected row.  An unmatched key is not silently dropped: the
    caller decides whether it is an out-of-window line or a real coverage gap.
    """
    if key not in INVOICE_JOINS:
        raise ValueError(f"invoice join must be one of: {', '.join(INVOICE_JOINS)}")

    matched: set[str] = set()
    enriched: list[dict[str, Any]] = []
    for row in rows:
        if key == "request_id":
            raw = row.get("id")
            row_key = (
                str(raw)
                if isinstance(raw, int) and not isinstance(raw, bool)
                else None
            )
        else:
            row_key = _text(row.get("generation_id"))
        if row_key is not None and row_key in costs:
            enriched.append({**row, "invoice_cost_usd": costs[row_key]})
            matched.add(row_key)
        else:
            enriched.append(row)

    unmatched = sorted(key_ for key_ in costs if key_ not in matched)
    return enriched, len(matched), unmatched


def aggregate_invoice_days(
    rows: Sequence[dict[str, Any]], costs: Mapping[str, float]
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Collapse rows and invoice lines to one comparison per UTC calendar day.

    Providers that publish only a daily cost total cannot be joined per request.
    For those, the strongest honest claim is the day total: sum Tamias's
    computed cost for the day and compare it with the invoice's line for that
    day.  A day with an unpriced row keeps its total unknown, so the comparison
    is INCOMPLETE rather than a partial sum.

    Returns ``(rows, unmatched_days, matched_days, undated_rows)``.
    """
    for key in costs:
        try:
            date.fromisoformat(key)
        except ValueError as exc:
            raise ValueError(
                f"invoice day {key!r} is not a YYYY-MM-DD date"
            ) from exc

    totals: dict[str, float | None] = {}
    undated = 0
    for row in rows:
        stamp = _row_date(row)
        if stamp is None:
            undated += 1
            continue
        day = stamp.isoformat()
        value = _computed(row)
        if day not in totals:
            totals[day] = value
        else:
            prior = totals[day]
            totals[day] = None if prior is None or value is None else prior + value

    aggregated: list[dict[str, Any]] = []
    matched = 0
    for day in sorted(set(totals) | set(costs)):
        invoice_cost = costs.get(day)
        if invoice_cost is not None:
            matched += 1
        aggregated.append(
            {
                "id": None,
                "ts": f"{day}T00:00:00Z",
                "computed_list_cost_usd": totals.get(day),
                "invoice_cost_usd": invoice_cost,
            }
        )

    unmatched = sorted(set(costs) - set(totals))
    return aggregated, unmatched, matched, undated
