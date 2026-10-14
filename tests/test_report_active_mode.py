"""``tamias report`` under active routing.

Two claims:

*  In active mode the report separates the switches the proxy *applied* from
   the ones it recorded but declined to apply, so a routed run can show how
   much of its own policy actually took effect;
*  In ``off`` mode the report says so, rather than printing a shadow line for
   decisions that were never made.

Everything runs in-process against a temporary SQLite log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.cli import main
from tamias.store import Store
from tamias.types import CostBreakdown, Decision, Usage

SHEET = """
date = "2026-10-15"

["strong/model"]
input = 3.0
output = 15.0

["cheap/model"]
input = 0.25
output = 1.25
"""

STRONG = "strong/model"
CHEAP = "cheap/model"


def _log(
    db: str,
    *,
    requested: str,
    used: str,
    action: str,
    router_mode: str | None,
    usage: Usage | None = None,
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-10-15T00:00:00Z",
        session_id="s1",
        model_requested=requested,
        model_used=used,
        usage=usage or Usage(2000, 100, 0, None),
        cost=CostBreakdown(usd=None, formula="test", price_sheet_date="2026-10-15"),
        latency_ms=120,
        status="200",
        decision=Decision(action=action, target_model=None, reason="test"),
        router_mode=router_mode,
    )
    store.close()


@pytest.fixture
def prices(tmp_path: Path) -> str:
    path = tmp_path / "prices.toml"
    path.write_text(SHEET)
    return str(path)


def _report(capsys, db: str, prices: str) -> tuple[int, str]:
    code = main(["report", "--db", db, "--prices", prices])
    return code, capsys.readouterr().out


def test_active_reports_applied_switches_and_the_ones_it_declined(
    tmp_path: Path, capsys, prices: str
) -> None:
    """One applied switch and one suppressed switch: both are visible, and the
    suppressed one is named for what it is rather than counted as a saving."""
    db = str(tmp_path / "log.db")
    _log(db, requested=STRONG, used=CHEAP, action="SWITCH", router_mode="active")
    _log(db, requested=STRONG, used=STRONG, action="SWITCH", router_mode="active")

    code, out = _report(capsys, db, prices)

    assert code == 0
    assert "requests: 2" in out
    routed_lines = [line for line in out.splitlines() if line.startswith("requests routed to")]
    assert len(routed_lines) == 1, f"expected one routed line:\n{out}"
    line = routed_lines[0]
    assert "requests routed to the cheap model: 1" in line
    assert "1 more decided SWITCH and were not applied" in line
    assert "the caller's key may not use the target" in line

    # Only the applied rewrite contributes to the realised saving.
    realised = [line for line in out.splitlines() if line.startswith("realised saving")]
    assert len(realised) == 1, f"expected one realised saving line:\n{out}"
    assert "over 1 rows" in realised[0]


def test_active_reports_every_switch_applied_when_none_are_declined(
    tmp_path: Path, capsys, prices: str
) -> None:
    db = str(tmp_path / "log.db")
    _log(db, requested=STRONG, used=CHEAP, action="SWITCH", router_mode="active")

    code, out = _report(capsys, db, prices)

    assert code == 0
    routed_lines = [line for line in out.splitlines() if line.startswith("requests routed to")]
    assert routed_lines == ["requests routed to the cheap model: 1"], f"got:\n{out}"


def test_off_mode_says_no_decision_was_recorded(
    tmp_path: Path, capsys, prices: str
) -> None:
    db = str(tmp_path / "log.db")
    _log(db, requested=STRONG, used=STRONG, action="STAY", router_mode="off")
    _log(db, requested=STRONG, used=STRONG, action="STAY", router_mode="off")

    code, out = _report(capsys, db, prices)

    assert code == 0
    assert "router was off: no decision was recorded" in out
    assert "would have switched" not in out


def test_rows_without_a_router_mode_keep_the_shadow_wording(
    tmp_path: Path, capsys, prices: str
) -> None:
    """A log written before ``router_mode`` existed still reads as shadow."""
    db = str(tmp_path / "log.db")
    _log(db, requested=STRONG, used=STRONG, action="SWITCH", router_mode=None)

    code, out = _report(capsys, db, prices)

    assert code == 0
    assert "requests shadow mode would have switched: 1" in out
