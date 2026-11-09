"""Outcome attribution for routing decisions, read back off the request log.

The router decides about one request and never sees the next one, so the log is
the only place a decision's *consequence* can be found.  The ``decision_reason``
it already writes distinguishes a mechanical turn from a failed one, and rows
are ordered, so "the turn after a downgrade failed" is one query away: no new
column, no request text stored, no hook inside the request path.

What this module deliberately does **not** claim:

* A swap has an *observed* consequence only when the proxy actually rewrote the
  model (``active`` mode).  In shadow mode the request ran on the model the
  caller named and nothing followed the recorded downgrade, so counting a
  consequence there would present arithmetic as a measurement.
* ``error_followed`` counts *candidates*, never verdicts.  An error after a swap
  is evidence that the cheap model struggled; it is not proof, because the
  strong model may have failed that turn too and the proxy never sees the
  counterfactual.
* An unrecognised next turn is ``inconclusive``, not a pass.

Everything here is pure: it reads row dictionaries and returns records, so the
report and the tests share one implementation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

__all__ = [
    "ERROR",
    "MECHANICAL",
    "PLANNING",
    "OTHER",
    "Outcome",
    "Verdict",
    "classify_reason",
    "downgrade_outcomes",
    "verdict",
]

#: The next turn failed a tool, which is what the strong model is for.
ERROR = "error"
#: The next turn was mechanical work: another read/shell step, or the cheap
#: model holding on from the downgrade itself.
MECHANICAL = "mechanical"
#: The next turn was the agent planning: the mechanical run ended.
PLANNING = "planning"
#: Anything not recognised by the phrases above.
OTHER = "other"

# Substrings of the reason strings the router writes.  Kept as substrings so a
# new rule phrase cannot silently turn this module's arithmetic into a guess:
# an unknown reason is classified ``OTHER`` and counted as inconclusive, never
# folded into one of the classes that carry a judgement.
_PHRASES: tuple[tuple[str, str], ...] = (
    ("tool error", ERROR),
    ("user turn", PLANNING),
    ("held", MECHANICAL),
    ("easy tool", MECHANICAL),
    ("requested model limited", OTHER),
    ("no cheaper pool target", OTHER),
    ("no model passed the routing gates", OTHER),
    ("pinned by client", PLANNING),
)


def classify_reason(reason: object) -> str:
    """The class of one logged decision reason, or ``OTHER`` when unknown."""
    text = reason if isinstance(reason, str) else ""
    for phrase, klass in _PHRASES:
        if phrase in text:
            return klass
    return OTHER


def _row_model(row: dict, key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _was_applied(row: dict) -> bool:
    """Did the proxy actually rewrite this request, or only record the want?

    Two independent markers, because a log written by an older build may carry
    only one: the mode the row was logged under, and the physical fact that the
    model that ran differs from the one the caller named.  Either is enough;
    a row that carries neither is treated as a projection rather than a fact.
    """
    mode = row.get("router_mode")
    if isinstance(mode, str) and mode and mode != "active":
        return False
    requested = _row_model(row, "model_requested")
    used = _row_model(row, "model_used")
    target = _row_model(row, "decision_target_model")
    if target and used and used == target and used != requested:
        return True
    return used != requested


@dataclass(frozen=True)
class Outcome:
    """One logged downgrade, and what the session did on the turn after it."""

    session_id: str
    requested: str
    target: str
    applied: bool
    followed_by: str
    """ERROR, MECHANICAL, PLANNING, OTHER, or ``""`` when the log has no next row."""

    @property
    def misroute_candidate(self) -> bool:
        return self.followed_by == ERROR

    @property
    def inconclusive(self) -> bool:
        return self.followed_by in ("", OTHER)


@dataclass(frozen=True)
class Verdict:
    """The consequence record for every downgrade in one log."""

    outcomes: tuple[Outcome, ...] = ()

    @property
    def projected(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if not o.applied)

    @property
    def observed(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if o.applied)

    @property
    def observed_total(self) -> int:
        return len(self.observed)

    @property
    def projected_total(self) -> int:
        return len(self.projected)

    @property
    def misroute_candidates(self) -> int:
        return sum(1 for o in self.observed if o.misroute_candidate)

    @property
    def carried_work(self) -> int:
        return sum(1 for o in self.observed if o.followed_by == MECHANICAL)

    @property
    def returned_to_planning(self) -> int:
        return sum(1 for o in self.observed if o.followed_by == PLANNING)

    @property
    def inconclusive(self) -> int:
        return sum(1 for o in self.observed if o.inconclusive)

    def by_pair(self) -> list[tuple[tuple[str, str], Counter[str]]]:
        """Per (requested -> target) pair, the classes that followed a downgrade."""
        pairs: dict[tuple[str, str], Counter[str]] = {}
        for outcome in self.observed:
            key = (outcome.requested, outcome.target)
            pairs.setdefault(key, Counter())[outcome.followed_by or "end"] += 1
        return sorted(pairs.items(), key=lambda item: (-sum(item[1].values()), item[0]))

    def lines(self) -> list[str]:
        """Printable lines, in the voice ``report`` uses for its caveats."""
        if not self.outcomes:
            return []
        lines = [
            f"routing verdict: {self.observed_total} downgrade(s) the proxy applied, "
            f"{self.projected_total} recorded in shadow only"
        ]
        if self.observed_total:
            lines.append(
                "  followed by a failed tool: "
                f"{self.misroute_candidates} of {self.observed_total} "
                "(misroute candidates, not verdicts)"
            )
            lines.append(
                f"  followed by more mechanical work: {self.carried_work}"
                f"; by planning again: {self.returned_to_planning}"
                f"; inconclusive: {self.inconclusive}"
            )
        if self.projected_total:
            lines.append(
                "  shadow rows carry no consequence: the request ran on the model "
                "the caller named, so nothing followed the downgrade"
            )
        if self.observed_total and self.inconclusive == self.observed_total:
            lines.append(
                "  no downgrade can be judged from this log: every applied swap is "
                "the last row of its session"
            )
        return lines


def downgrade_outcomes(rows: list[dict]) -> Verdict:
    """Walk the log in order and pair each downgrade with the next turn in its session.

    Row order is the log's own ``rowid`` order, which is the order the requests
    arrived in; ``_read_rows`` already returns it that way.  A session's rows
    are paired only with rows of the same session, so two conversations running
    concurrently cannot lend each other evidence.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        session = _row_model(row, "session_id") or ""
        if session not in groups:
            groups[session] = []
            order.append(session)
        groups[session].append(row)

    outcomes: list[Outcome] = []
    for session in order:
        sequence = groups[session]
        for index, row in enumerate(sequence):
            if row.get("decision_action") != "SWITCH":
                continue
            following = sequence[index + 1] if index + 1 < len(sequence) else None
            outcomes.append(
                Outcome(
                    session_id=session,
                    requested=_row_model(row, "model_requested"),
                    target=_row_model(row, "decision_target_model")
                    or _row_model(row, "model_used"),
                    applied=_was_applied(row),
                    followed_by=(
                        classify_reason(following.get("decision_reason"))
                        if following is not None
                        else ""
                    ),
                )
            )
    return Verdict(outcomes=tuple(outcomes))


def verdict(rows: list[dict]) -> Verdict:
    """The verdict for one log.  An alias that reads better at the call site."""
    return downgrade_outcomes(rows)
