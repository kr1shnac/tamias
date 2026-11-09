"""Tests for the routing verdict: what each downgrade was followed by.

Everything is built here from plain row dictionaries, because the module reads
rows and nothing else.  The rows carry only the columns the store already
writes -- no request text, no new schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias.verdict import (  # noqa: E402
    ERROR,
    MECHANICAL,
    PLANNING,
    classify_reason,
    downgrade_outcomes,
)


def row(
    action: str,
    reason: str,
    *,
    target: str = "cheap-1",
    session: str = "s1",
    requested: str = "strong-1",
    used: str | None = None,
    mode: str | None = "active",
) -> dict:
    """One request-log row, shaped the way ``Store.log_request`` writes it."""
    return {
        "session_id": session,
        "model_requested": requested,
        "model_used": used if used is not None else (target if action == "SWITCH" else requested),
        "decision_action": action,
        "decision_target_model": target,
        "decision_reason": reason,
        "router_mode": mode,
    }


def test_no_switches_produce_no_verdict() -> None:
    verdict = downgrade_outcomes([row("STAY", "user turn: planning")])
    assert verdict.outcomes == ()
    assert verdict.lines() == []


def test_an_applied_swap_followed_by_an_error_is_a_candidate() -> None:
    rows = [
        row("SWITCH", "easy tool: read"),
        row("STAY", "tool error: needs strong model"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.observed_total == 1
    assert verdict.misroute_candidates == 1
    assert verdict.observed[0].applied is True
    assert verdict.observed[0].followed_by == ERROR


def test_a_shadow_swap_carries_no_consequence() -> None:
    """Shadow never rewrote anything, so nothing can have followed the downgrade."""
    rows = [
        row("SWITCH", "easy tool: read", mode="shadow", used="strong-1"),
        row("STAY", "tool error: needs strong model", used="strong-1", mode="shadow"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.observed_total == 0
    assert verdict.projected_total == 1
    assert verdict.misroute_candidates == 0
    assert any("shadow rows carry no consequence" in line for line in verdict.lines())


def test_a_hold_counts_as_the_work_continuing() -> None:
    rows = [
        row("SWITCH", "easy tool: read"),
        row("SWITCH", "already on cheap-1: held"),
        row("SWITCH", "already on cheap-1: held"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.observed_total == 3
    # Two of the three are followed by another mechanical turn; the last row of
    # the session has nothing after it, so it is inconclusive rather than a pass.
    assert verdict.carried_work == 2
    assert verdict.inconclusive == 1
    assert verdict.misroute_candidates == 0


def test_a_swap_that_ends_the_session_is_inconclusive() -> None:
    rows = [row("SWITCH", "easy tool: read")]
    verdict = downgrade_outcomes(rows)
    assert verdict.observed_total == 1
    assert verdict.inconclusive == 1
    assert any("last row of its session" in line for line in verdict.lines())


def test_planning_after_a_swap_is_not_a_misroute() -> None:
    rows = [
        row("SWITCH", "easy tool: read"),
        row("STAY", "user turn: planning"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.returned_to_planning == 1
    assert verdict.misroute_candidates == 0


def test_rows_are_only_paired_inside_their_own_session() -> None:
    """Two conversations must not lend each other evidence."""
    rows = [
        row("SWITCH", "easy tool: read", session="s1"),
        row("STAY", "tool error: needs strong model", session="s2"),
        row("SWITCH", "easy tool: read", session="s2"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.observed_total == 2
    # s1's swap is the last row of s1, and s2's swap is the last row of s2.
    assert verdict.inconclusive == 2
    assert verdict.misroute_candidates == 0


def test_an_unrecognised_next_turn_is_inconclusive_not_a_pass() -> None:
    rows = [
        row("SWITCH", "easy tool: read"),
        row("STAY", "some future rule phrase nobody has written yet"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.inconclusive == 1
    assert verdict.misroute_candidates == 0


def test_a_log_without_the_router_mode_column_still_works() -> None:
    """Rows written before the column existed must not crash the report."""
    verdict = downgrade_outcomes(
        [
            {"session_id": "s", "model_requested": "strong-1", "model_used": "cheap-1",
             "decision_action": "SWITCH", "decision_reason": "easy tool: read",
             "decision_target_model": "cheap-1"},
            {"session_id": "s", "model_requested": "strong-1", "model_used": "strong-1",
             "decision_action": "STAY", "decision_reason": "tool error",
             "decision_target_model": None},
        ]
    )
    assert verdict.observed_total == 1
    assert verdict.misroute_candidates == 1


def test_classify_reason_maps_the_phrases_the_router_actually_writes() -> None:
    assert classify_reason("tool error: needs strong model") == ERROR
    assert classify_reason("user turn: planning") == PLANNING
    assert classify_reason("already on cheap-1: held") == MECHANICAL
    assert classify_reason("easy tool: read") == MECHANICAL
    assert classify_reason("hysteresis: 2 of 3 requests since last switch") == "other"
    assert classify_reason(None) == "other"


def test_the_pair_breakdown_names_the_two_models() -> None:
    rows = [
        row("SWITCH", "easy tool: read"),
        row("STAY", "tool error: needs strong model"),
    ]
    verdict = downgrade_outcomes(rows)
    assert verdict.by_pair() == [(("strong-1", "cheap-1"), {"error": 1})]
