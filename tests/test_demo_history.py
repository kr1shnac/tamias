"""demo/make_history.py, checked from the repo's own suite.

The three-week history is a demo artefact, so the things worth pinning here
are the ones that would otherwise slip: that the generator is deterministic
(same seed, same data), that the numbers it writes actually add up (saving is
the strong line minus the mix line, per session and in total), and that the
database it writes is a normal tamias request log -- real Store schema, rows
stamped simulated so ``tamias report`` labels every amount, no request that
does not carry a token count.

Run this file alone with:

    .venv/bin/pytest -q tests/test_demo_history.py
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo"

sys.path.insert(0, str(REPO_ROOT / "src"))  # noqa: E402

from tamias.cli import main as report_main  # noqa: E402

PRICE_SHEET = "prices.simulated.toml"
END = date(2026, 10, 6)
DAYS = 5
SEED = 7


def _load(name: str) -> ModuleType:
    """Import a demo/ script by path so demo/ never lands on sys.path.

    The generated name is also registered in ``sys.modules`` -- unlike the
    reporting scripts, make_history uses dataclasses with ``slots=True``, which
    need the module in ``sys.modules`` for ``__module__`` lookup.
    """
    key = f"_demo_{name}"
    spec = importlib.util.spec_from_file_location(key, DEMO_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


make_history = _load("make_history")


def _generate(tmp_path: Path, out: str = "out") -> dict[str, Any]:
    """Run the generator into tmp_path/<out> and return the summary JSON."""
    target = tmp_path / out
    code = make_history.main(
        [
            "--out",
            str(target),
            "--end",
            END.isoformat(),
            "--days",
            str(DAYS),
            "--seed",
            str(SEED),
            "--prices",
            str(REPO_ROOT / PRICE_SHEET),
        ]
    )
    assert code == 0
    return json.loads((target / "agent-3weeks.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_same_seed_produces_same_data(tmp_path) -> None:
    first = _generate(tmp_path, "a")
    second = _generate(tmp_path, "b")
    assert first["totals"] == second["totals"]
    assert first["sessions"] == second["sessions"]


# --------------------------------------------------------------------------
# The arithmetic holds together
# --------------------------------------------------------------------------


def test_totals_and_sessions_add_up(tmp_path) -> None:
    summary = _generate(tmp_path)
    totals = summary["totals"]

    assert totals["sessions"] == len(summary["sessions"])
    assert totals["requests"] == sum(s["requests"] for s in summary["sessions"])
    assert totals["switches"] == sum(s["switches"] for s in summary["sessions"])
    assert totals["cheap_requests"] == sum(s["cheap_requests"] for s in summary["sessions"])

    # Session values are rounded to six decimals in the JSON, so a total built
    # from unrounded rows differs from the sum of the shown numbers by at most
    # rounding drift.  The check is that they agree to "shown" precision.
    assert totals["all_strong_usd"] == pytest.approx(
        sum(s["all_strong_usd"] for s in summary["sessions"]), abs=1e-4
    )
    assert totals["actual_mix_usd"] == pytest.approx(
        sum(s["actual_mix_usd"] for s in summary["sessions"]), abs=1e-4
    )
    assert totals["saving_usd"] == pytest.approx(
        totals["all_strong_usd"] - totals["actual_mix_usd"], abs=1e-5
    )
    assert totals["saving_usd"] == pytest.approx(
        sum(s["saving_usd"] for s in summary["sessions"]), abs=1e-4
    )

    for session in summary["sessions"]:
        assert session["saving_usd"] == pytest.approx(
            session["all_strong_usd"] - session["actual_mix_usd"], abs=1e-5
        )
        if session["all_strong_usd"]:
            assert session["saving_pct"] == pytest.approx(
                100.0 * session["saving_usd"] / session["all_strong_usd"], abs=1e-1
            )


# --------------------------------------------------------------------------
# The database is a real tamias request log, stamped simulated
# --------------------------------------------------------------------------


def test_database_is_a_normal_simulated_request_log(tmp_path) -> None:
    _generate(tmp_path)
    db = tmp_path / "out" / "agent-3weeks.db"
    with sqlite3.connect(db) as conn:
        rows = list(
            conn.execute(
                "SELECT model_requested, model_used, decision_action, price_simulated, "
                "price_sheet, cost_usd, input_tokens FROM requests"
            )
        )
    assert rows
    assert all(row[0] == "strong" for row in rows)
    assert all(row[3] == 1 for row in rows)  # every amount is stamped simulated
    assert all(row[4] == PRICE_SHEET for row in rows)
    assert all(row[6] is not None for row in rows)  # no UNKNOWN token counts
    cheap = sum(1 for row in rows if row[1] == "cheap")
    assert any(row[2] == "SWITCH" for row in rows)
    assert cheap > 0
    with sqlite3.connect(db) as conn:
        summary = json.loads((tmp_path / "out" / "agent-3weeks.json").read_text(encoding="utf-8"))
    assert summary["totals"]["cheap_requests"] == cheap


# --------------------------------------------------------------------------
# tamias report recognises it and labels every amount
# --------------------------------------------------------------------------


def test_report_labels_the_history_as_simulated(tmp_path, capsys) -> None:
    _generate(tmp_path)
    db = tmp_path / "out" / "agent-3weeks.db"
    code = report_main(["report", "--db", str(db), "--prices", str(REPO_ROOT / PRICE_SHEET)])
    out = capsys.readouterr().out
    assert code == 0
    assert "SIMULATED PRICES, NOT REAL SAVINGS" in out
    assert "realised saving" in out
    assert "price provenance: prices.simulated.toml" in out
