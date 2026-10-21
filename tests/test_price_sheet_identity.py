"""Tests for D2: a price sheet's identity is its name, not the path as typed.

``tamias report`` compared ``str(prices_path)`` against the sheet names stored
with the rows.  The stored name is the path that happened to be typed when the
log was written, so the same file spelled differently -- ``./prices.toml``,
``prices.toml``, an absolute path -- was treated as a *different* price basis,
and the report blanked a saving that was in fact computable.

These tests pin the fixed behaviour, and pin the content signal the fix adds:
every row records the date of the sheet that priced it, and ``report`` now
compares that date as well as the name.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _sheet(tmp_path: Path, name: str = "prices.toml", date: str = "2026-10-21") -> Path:
    path = tmp_path / name
    path.write_text(
        f'date = "{date}"\nsimulated = true\n\n'
        '["mock-strong"]\ninput = 3.0\noutput = 15.0\ncached_input = 0.3\n\n'
        '["mock-cheap"]\ninput = 0.5\noutput = 1.5\ncached_input = 0.05\n'
    )
    return path


def _log(tmp_path: Path, sheet_name: str, sheet_date: str = "2026-10-21") -> Path:
    """A log with two switched rows, priced by the sheet named ``sheet_name``."""
    db = tmp_path / "requests.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY, ts TEXT, session_id TEXT,
            model_requested TEXT, model_used TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            cached_input_tokens INTEGER, cache_write_tokens INTEGER,
            cost_usd REAL, computed_list_cost_usd REAL,
            cost_source TEXT, price_sheet_date TEXT, latency_ms INTEGER,
            status TEXT, decision_action TEXT, decision_target_model TEXT,
            price_sheet TEXT, price_simulated INTEGER
        )
        """
    )
    for index in range(2):
        conn.execute(
            "INSERT INTO requests (ts, session_id, model_requested, model_used,"
            " input_tokens, output_tokens, cached_input_tokens, cache_write_tokens,"
            " cost_usd, computed_list_cost_usd, cost_source, price_sheet_date,"
            " latency_ms, status, decision_action, decision_target_model,"
            " price_sheet, price_simulated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-10-21T00:00:00.000+00:00", f"s{index}",
                "mock-strong", "mock-cheap",
                1000, 200, 400, None,
                0.00062, 0.00062, "computed", sheet_date, 5, "200", "SWITCH", "mock-cheap",
                sheet_name, 1,
            ),
        )
    conn.commit()
    conn.close()
    return db


def _report(db: Path, prices: Path) -> str:
    """Run `tamias report` and return its output."""
    result = subprocess.run(
        [
            sys.executable, "-m", "tamias.cli", "report",
            "--db", str(db), "--prices", str(prices),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_same_sheet_spelled_differently_still_computes_a_saving(
    tmp_path: Path,
) -> None:
    """The defect: the same file, spelled differently, blanked the saving."""
    sheet = _sheet(tmp_path)
    db = _log(tmp_path, "prices.toml")

    # Read it back through the very same file, spelled three ways.
    for spelling in (sheet, tmp_path / "prices.toml", sheet.resolve()):
        out = _report(db, spelling)
        assert "estimated saving: UNKNOWN" not in out, (
            f"{spelling} blanked a computable saving:\n{out}"
        )
        assert "differs from stored price sheet" not in out, out


def test_a_genuinely_different_sheet_still_warns(tmp_path: Path) -> None:
    """The warning is a correctness feature and must survive the fix."""
    sheet = _sheet(tmp_path, name="other.toml")
    db = _log(tmp_path, "prices.toml")

    out = _report(db, sheet)

    assert "differs from stored price sheet" in out, out


def test_a_sheet_with_the_same_name_but_a_different_date_warns(tmp_path: Path) -> None:
    """The content signal: same name, different rates behind it."""
    sheet = _sheet(tmp_path, name="prices.toml", date="2026-11-01")
    db = _log(tmp_path, "prices.toml", sheet_date="2026-10-21")

    out = _report(db, sheet)

    assert "is dated 2026-11-01" in out, out
    assert "different price basis" in out, out


def test_a_sheet_matching_the_recorded_date_does_not_warn(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path, name="prices.toml", date="2026-10-21")
    db = _log(tmp_path, "prices.toml", sheet_date="2026-10-21")

    out = _report(db, sheet)

    assert "different price basis" not in out, out
    assert "is dated" not in out, out
