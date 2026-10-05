from __future__ import annotations

import re
from pathlib import Path

import pytest

from tamias.cli import main
from tamias.store import Store
from tamias.types import CostBreakdown, Decision, Usage

SHEET_DATE = "2026-01-05"


def _log_simulated(
    db: str,
    cost: float,
    price_sheet: str = "prices.openrouter-sim.toml",
    simulated: bool = True,
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested="gpt-4o",
        model_used="gpt-4o",
        usage=Usage(input_tokens=100, output_tokens=50, cached_input_tokens=0, cache_write_tokens=None),
        cost=CostBreakdown(usd=cost, formula="test", price_sheet_date=SHEET_DATE),
        latency_ms=120,
        status="200",
        decision=Decision(action="STAY", target_model=None, reason="test"),
        price_sheet=price_sheet,
        price_simulated=simulated,
    )
    store.close()


def _log_legacy(
    db: str,
    cost: float,
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested="gpt-4o",
        model_used="gpt-4o",
        usage=Usage(input_tokens=100, output_tokens=50, cached_input_tokens=0, cache_write_tokens=None),
        cost=CostBreakdown(usd=cost, formula="test", price_sheet_date=SHEET_DATE),
        latency_ms=120,
        status="200",
        decision=Decision(action="STAY", target_model=None, reason="test"),
        price_sheet=None,
        price_simulated=None,
    )
    store.close()


def _log_real_priced_with_stored_sheet(
    db: str,
    cost: float,
    price_sheet: str = "prices.openrouter.toml",
    simulated: bool = False,
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested="gpt-4o",
        model_used="gpt-4o",
        usage=Usage(input_tokens=100, output_tokens=50, cached_input_tokens=0, cache_write_tokens=None),
        cost=CostBreakdown(usd=cost, formula="test", price_sheet_date=SHEET_DATE),
        latency_ms=120,
        status="200",
        decision=Decision(action="STAY", target_model=None, reason="test"),
        price_sheet=price_sheet,
        price_simulated=simulated,
    )
    store.close()

def write_prices(tmp_path: Path, sheet_text: str) -> str:
    p = tmp_path / "prices.toml"
    p.write_text(sheet_text)
    return str(p)


def run(capsys, db: str, prices: str):
    code = main(["report", "--db", db, "--prices", prices])
    return code, capsys.readouterr().out


def test_simulated_rows_with_real_sheet_has_no_bare_total_cost(tmp_path, capsys):
    db = str(tmp_path / "log.db")
    _log_simulated(db, 0.01, price_sheet="prices.openrouter-sim.toml", simulated=True)
    real_prices = write_prices(tmp_path, """
simulated = false
date = "2026-01-05"

["gpt-4o"]
input = 2.5e-6
output = 10e-6
cached_input = 1.25e-6
""")
    code, out = run(capsys, db, real_prices)
    assert code == 0
    for line in out.splitlines():
        if line.startswith("total cost: $"):
            raise AssertionError(f"bare total cost line found: {line}")
    assert "total cost" in out


def test_legacy_rows_have_no_bare_total_cost(tmp_path, capsys):
    db = str(tmp_path / "log.db")
    _log_legacy(db, 0.01)
    prices = write_prices(tmp_path, """
simulated = false
date = "2026-01-05"

["gpt-4o"]
input = 2.5e-6
output = 10e-6
cached_input = 1.25e-6
""")
    code, out = run(capsys, db, prices)
    assert code == 0
    for line in out.splitlines():
        if line.startswith("total cost: $"):
            raise AssertionError(f"bare total cost line found: {line}")
    assert "total cost" in out


def test_real_priced_row_with_stored_sheet_has_no_bare_total_cost(tmp_path, capsys):
    db = str(tmp_path / "log.db")
    _log_real_priced_with_stored_sheet(db, 0.01, price_sheet="prices.openrouter.toml", simulated=False)
    prices = write_prices(tmp_path, """
simulated = false
date = "2026-01-05"

["gpt-4o"]
input = 2.5e-6
output = 10e-6
cached_input = 1.25e-6
""")
    code, out = run(capsys, db, prices)
    assert code == 0
    for line in out.splitlines():
        if line.startswith("total cost: $"):
            raise AssertionError(f"bare total cost line found: {line}")
    assert "total cost" in out


def test_mutation_bare_total_cost_would_fail(tmp_path, capsys):
    db = str(tmp_path / "log.db")
    _log_simulated(db, 0.01, price_sheet="prices.openrouter-sim.toml", simulated=True)
    real_prices = write_prices(tmp_path, """
simulated = false
date = "2026-01-05"

["gpt-4o"]
input = 2.5e-6
output = 10e-6
cached_input = 1.25e-6
""")
    code, out = run(capsys, db, real_prices)
    assert code == 0
    bad = [l for l in out.splitlines() if re.match(r'^total cost:\s*\$\d', l)]
    assert not bad, f"Found bad bare total cost: {bad}"
