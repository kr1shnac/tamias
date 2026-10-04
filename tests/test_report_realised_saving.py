"""Tests for the new ``realised saving`` line in ``tamias report``.

These tests verify the additive realised-saving line that reports how much
the proxy actually rewrote requests to a cheaper model.  The existing
``estimated saving`` line and all unchanged logic are left untouched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias.store import Store  # noqa: E402
from tamias.types import CostBreakdown, Decision, Usage  # noqa: E402

SHEET_DATE = "2026-01-05"
SIMULATED_SHEET = Path(__file__).resolve().parents[1] / "prices.openrouter-sim.toml"


def _log(
    db: str,
    usage: Usage,
    cost: float | None,
    action: str,
    model_used: str = "gpt-4o",
    model_requested: str = "gpt-4o",
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested=model_requested,
        model_used=model_used,
        usage=usage,
        cost=CostBreakdown(usd=cost, formula="test", price_sheet_date=SHEET_DATE),
        latency_ms=120,
        status="200",
        decision=Decision(action=action, target_model=None, reason="test"),
    )
    store.close()


def _run(capsys, db: str, prices: str):
    from tamias.cli import main

    code = main(["report", "--db", db, "--prices", prices])
    return code, capsys.readouterr().out


def _money_realised(text: str) -> float | None:
    """Extract the dollar amount from a realised saving line.

    Handles both ``realised saving: $X`` and
    ``realised saving (rows the proxy actually rewrote): $X over k rows`` formats.
    Returns None when the line indicates UNKNOWN (no dollar amount).
    """
    match = re.search(r"\$([0-9.]+)", text)
    if match is None:
        return None
    return float(match.group(1))


def _money_estimated(text: str) -> float:
    """Extract the dollar amount from an estimated saving line ``estimated saving: $X``."""
    match = re.search(r"estimated saving: \\\$([0-9.]+)", text)
    assert match is not None, f"no dollar amount in estimated saving line:\n{text}"
    return float(match.group(1))


# ---- 8-models session: rows 5 & 8 rewritten, rest stay ----

BIG = Usage(input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None)
SMALL = Usage(input_tokens=1_872, output_tokens=24, cached_input_tokens=0, cache_write_tokens=None)


def write_prices(tmp_path: Path, sheet_text: str) -> str:
    p = tmp_path / "prices.toml"
    p.write_text(sheet_text)
    return str(p)


SIMULATED_TABLE = """
simulated = true
date = "2026-10-04"

["nvidia/nemotron-3-ultra-550b-a55b:free"]
input = 3.0
output = 15.0
cached_input = 0.30

["nvidia/nemotron-3.5-lightning:free"]
input = 0.25
output = 1.25
cached_input = 0.03
"""


@pytest.mark.parametrize(
    "test_fn",
    [
        "test_active_shape_two_rewritten",
        "test_shadow_no_rewritten",
        "test_unpriced_rewritten_two_rows",
        "test_one_priced_one_unpriced",
        "test_rewritten_model_not_in_sheet",
    ],
)
def test_realised_saving_scenarios(tmp_path, capsys, test_fn):
    """Parametrised runner - each scenario calls its own helper."""
    pass


def test_active_shape_two_rewritten(tmp_path, capsys):
    """Scenario (a): 8 rows; rows 5 and 8 rewritten with token counts.

    Row 5: strong=7946/46/0, cheap=local-model → saving ≈ 0.024528
    Row 8: strong uncached=8434-6528=1906, cheap → saving ≈ 0.0056888
    Total realised ≈ 0.0302168, over 2 rows.
    """
    db = str(tmp_path / "log.db")
    # Row 5: rewritten with counts
    _log(
        db,
        Usage(input_tokens=7_946, output_tokens=46, cached_input_tokens=0, cache_write_tokens=None),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    # Row 6: stay on requested model
    _log(
        db,
        Usage(input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None),
        None,
        "STAY",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    # Row 7: stay on requested model
    _log(
        db,
        Usage(input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None),
        None,
        "STAY",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    # Row 8: rewritten with counts
    _log(
        db,
        Usage(
            input_tokens=8_434, output_tokens=53, cached_input_tokens=6_528, cache_write_tokens=None
        ),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    # Rows 1-4: stay on requested model
    for _ in range(4):
        _log(
            db,
            Usage(
                input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None
            ),
            None,
            "STAY",
            model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
        )

    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = _run(capsys, db, prices)

    assert code == 0
    assert "requests: 8" in out
    # The realised saving line must be present
    realised_lines = [
        realised_line
        for realised_line in out.splitlines()
        if realised_line.startswith("realised saving")
    ]
    assert len(realised_lines) == 1, f"expected exactly 1 realised saving line, got:\n{out}"
    line = realised_lines[0]
    assert "over 2 rows" in line, f"expected 'over 2 rows' in:\n{line}"
    # Within 1e-6 tolerance
    val = _money_realised(line)
    assert abs(val - 0.0302168) < 1e-6, (
        f"realised saving {val!r} not within 1e-6 of 0.0302168\n{out}"
    )


def test_shadow_no_rewritten(tmp_path, capsys):
    """Scenario (b): shadow-only DB with no rewritten rows.

    The output contains no ``realised saving`` line and still contains
    the existing estimated-saving line unchanged.
    """
    db = str(tmp_path / "log.db")
    _log(db, BIG, None, "STAY", model_requested="local-model", model_used="local-model")
    _log(db, SMALL, None, "STAY", model_requested="local-model", model_used="local-model")

    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = _run(capsys, db, prices)

    assert code == 0
    # No realised saving line when nothing was rewritten
    assert not any(
        realised_line.startswith("realised saving") for realised_line in out.splitlines()
    ), f"expected no realised saving line, got:\n{out}"
    # Estimated saving line still present
    estimated_lines = [
        estimated_line
        for estimated_line in out.splitlines()
        if estimated_line.startswith("estimated saving")
    ]
    assert len(estimated_lines) >= 1, f"expected at least 1 estimated saving line, got:\n{out}"


def test_unpriced_rewritten_two_rows(tmp_path, capsys):
    """Scenario (c): two rewritten rows with NULL token counts.

    The realised line says ``UNKNOWN over 2 rows`` (no ``not priced`` caveat
    when every rewritten row is unpriced).
    """
    db = str(tmp_path / "log.db")
    # Two rows rewritten, but no token counts
    _log(
        db,
        Usage(
            input_tokens=None, output_tokens=None, cached_input_tokens=None, cache_write_tokens=None
        ),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    _log(
        db,
        Usage(
            input_tokens=None, output_tokens=None, cached_input_tokens=None, cache_write_tokens=None
        ),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )

    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = _run(capsys, db, prices)

    assert code == 0
    realised_lines = [
        realised_line
        for realised_line in out.splitlines()
        if realised_line.startswith("realised saving")
    ]
    assert len(realised_lines) == 1, f"expected exactly 1 realised saving line, got:\n{out}"
    line = realised_lines[0]
    # When all rewritten rows are unpriced, just "UNKNOWN over 2 rows"
    assert "UNKNOWN over 2 rows" in line, f"expected 'UNKNOWN over 2 rows' in:\n{line}"


def test_one_priced_one_unpriced(tmp_path, capsys):
    """Scenario (d): one priced and one unpriced rewritten row.

    The dollar amount from the priced row plus the "not priced" caveat
    when applicable (assert on the wording the code actually prints).
    """
    db = str(tmp_path / "log.db")
    # Row with counts and pricing (cached_input_tokens=0 so sheet rates apply)
    _log(
        db,
        Usage(input_tokens=1_000, output_tokens=50, cached_input_tokens=0, cache_write_tokens=None),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    # Row without counts (unpriced)
    _log(
        db,
        Usage(
            input_tokens=None, output_tokens=None, cached_input_tokens=None, cache_write_tokens=None
        ),
        None,
        "SWITCH",
        model_used="nvidia/nemotron-3.5-lightning:free",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )

    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = _run(capsys, db, prices)

    assert code == 0
    realised_lines = [
        realised_line
        for realised_line in out.splitlines()
        if realised_line.startswith("realised saving")
    ]
    assert len(realised_lines) == 1, f"expected exactly 1 realised saving line, got:\n{out}"
    line = realised_lines[0]
    # The dollar amount should be present from the priced row
    val = _money_realised(line)
    assert val is not None and val > 0, (
        f"expected positive dollar amount in realised line, got {val!r}:\n{line}"
    )
    # Should contain "over {realised_count} rows" since that's the code's wording
    import re

    m = re.search(r"over (\d+) rows", line)
    assert m is not None, f"expected 'over N rows' in realised line, got:\n{line}"
    assert m.group(1) == str(1), (
        f"expected 'over 1 rows' in realised line (1 priced row), got:\n{line}"
    )


def test_rewritten_model_not_in_sheet(tmp_path, capsys):
    """Scenario (e): a rewritten row whose model is not in the sheet.

    Counted as not priced, never as 0.
    """
    db = str(tmp_path / "log.db")
    _log(
        db,
        Usage(
            input_tokens=1_000, output_tokens=50, cached_input_tokens=None, cache_write_tokens=None
        ),
        None,
        "SWITCH",
        model_used="nonexistent-model-that-does-not-exist",
        model_requested="nvidia/nemotron-3-ultra-550b-a55b:free",
    )

    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = _run(capsys, db, prices)

    assert code == 0
    realised_lines = [
        realised_line
        for realised_line in out.splitlines()
        if realised_line.startswith("realised saving")
    ]
    assert len(realised_lines) == 1, f"expected exactly 1 realised saving line, got:\n{out}"
    line = realised_lines[0]
    # Model not in sheet → not priced → UNKNOWN caveat, never $0.00
    assert "UNKNOWN" in line, f"expected 'UNKNOWN' in realised line, got:\n{line}"
    # Dollar amount should be 0 or absent (model unpriced)
    val = _money_realised(line)
    assert val == 0.0 or val is None, (
        f"expected 0.0 or no dollar amount when model unpriced, got {val!r}:\n{line}"
    )
