"""Golden-number tests for the ``estimated saving`` switch target (F-2).

The estimate prices its switch arm against the model the router's own decision
named (``decision_target_model``), *not* the cheapest model in the passed price
sheet.  These tests pin the exact dollar amount for a log whose logged target
differs from the sheet's cheapest model, so a regression that silently re-prices
the estimate against the wrong model fails here instead of shipping a flattering
number.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias.store import Store  # noqa: E402
from tamias.types import CostBreakdown, Decision, Usage  # noqa: E402

SHEET_DATE = "2026-01-05"

# The strong model that answered, the sheet's cheapest model (ranked by the mean
# of input+output rates), and the router's actual logged cheap target.  The
# router target is deliberately NOT the sheet-cheapest, so pricing against the
# wrong one changes the answer.
TABLE = """
date = "2026-01-05"

["gpt-4o"]
input = 5.0
output = 15.0
cached_input = 0.5

["mini-cheap"]
input = 0.1
output = 0.2
cached_input = 0.01

["mid-cheap"]
input = 1.0
output = 3.0
cached_input = 0.1
"""


def _log(db: str, target: str | None) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested="gpt-4o",
        model_used="gpt-4o",
        usage=Usage(
            input_tokens=1_000,
            output_tokens=100,
            cached_input_tokens=0,
            cache_write_tokens=None,
        ),
        cost=CostBreakdown(usd=None, formula="test", price_sheet_date=SHEET_DATE),
        latency_ms=120,
        status="200",
        decision=Decision(
            action="SWITCH",
            target_model=target,
            reason="test",
            persisted_only=True,
        ),
    )
    store.close()


def _report(capsys, tmp_path: Path, target: str | None):
    db = str(tmp_path / "log.db")
    prices = tmp_path / "prices.toml"
    prices.write_text(TABLE)
    _log(db, target)

    from tamias.cli import main

    code = main(["report", "--db", db, "--prices", str(prices)])
    return code, capsys.readouterr().out


def _estimated(out: str) -> float:
    match = re.search(r"estimated saving: \$([0-9.]+)", out)
    assert match is not None, f"no estimated saving amount in:\n{out}"
    return float(match.group(1))


def test_saving_prices_against_logged_target_not_sheet_cheapest(tmp_path, capsys):
    """Logged target mid-cheap: saving = 0.002000 - 0.001300 = 0.000700.

    Stay gpt-4o warm:  (1000*0.5 + 100*15) / 1e6 = 0.002
    Switch mid-cheap:  (1000*1.0 + 100*3.0) / 1e6 = 0.0013
    Price against mini-cheap would instead give 0.00188 -- an overstatement.
    """
    code, out = _report(capsys, tmp_path, "mid-cheap")

    assert code == 0
    assert abs(_estimated(out) - 0.0007) < 1e-9, out
    # The line names the logged target, never the sheet-cheapest.
    assert "switch target (from router decisions): mid-cheap" in out, out
    assert "mini-cheap" not in out, out


def test_saving_falls_back_to_sheet_cheapest_without_a_logged_target(tmp_path, capsys):
    """Legacy rows with no logged target keep the old sheet-cheapest basis.

    Stay gpt-4o warm:  0.002
    Switch mini-cheap: (1000*0.1 + 100*0.2) / 1e6 = 0.00012
    Saving = 0.00188.
    """
    code, out = _report(capsys, tmp_path, None)

    assert code == 0
    assert abs(_estimated(out) - 0.00188) < 1e-9, out
    # With no router decision to read, the line discloses the sheet-cheapest
    # fallback rather than pretending the router chose it.
    assert "cheap model assumed: mini-cheap" in out, out
    assert "switch target (from router decisions)" not in out, out
