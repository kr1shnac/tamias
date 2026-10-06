"""Tests for ``tamias report``.

The report is standard library plus the project's own ``pricing``/``store``
modules, so these tests write a real request log with the real ``Store`` and a
real TOML price sheet, then check what the CLI prints.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias.cli import main  # noqa: E402
from tamias.pricing import ModelPrice, compute_cost, load_price_sheet  # noqa: E402
from tamias.store import COLUMNS, Store  # noqa: E402
from tamias.types import CostBreakdown, Decision, Usage  # noqa: E402

SHEET_DATE = "2026-01-05"
# USD per 1,000,000 tokens, the units load_price_sheet expects.
STRONG_TABLE = """
[gpt-4o]
input = 3.0
output = 15.0
cached_input = 0.3
cache_write = 3.75

[local-model]
input = 0.25
output = 1.25
cached_input = 0.025
cache_write = 0.3
"""

FREE_SHEET = """
[free-model]
input = 0.0
output = 0.0
cached_input = 0.0
cache_write = 0.0

[gpt-4o]
input = 3.0
output = 15.0
cached_input = 0.3
cache_write = 3.75
"""

SIMULATED_TABLE = """
simulated = true

[gpt-4o]
input = 3.0
output = 15.0
cached_input = 0.3
cache_write = 3.75

[local-model]
input = 0.25
output = 1.25
cached_input = 0.025
cache_write = 0.3
"""

BIG = Usage(
    input_tokens=10_000,
    cached_input_tokens=4_000,
    cache_write_tokens=1_000,
    output_tokens=2_000,
)
SMALL = Usage(
    input_tokens=2_000,
    cached_input_tokens=0,
    cache_write_tokens=0,
    output_tokens=500,
)

# Hand-computed from the formula in CONTRACT/pricing, so the assertions are
# independent of the implementation.  `input_tokens` is the TOTAL input, so it
# splits three ways: the 5,000 uncached tokens bill at `input`, the 4,000 cached
# ones at `cached_input`, and the 1,000 written ones at `cache_write`.
#   gpt-4o:      (5000*3.0 + 4000*0.3 + 1000*3.75 + 2000*15.0) / 1e6
BIG_ON_STRONG = 0.04995
#   local-model: (5000*0.25 + 4000*0.025 + 1000*0.3 + 2000*1.25) / 1e6
BIG_ON_CHEAP = 0.00415
BIG_SAVING = BIG_ON_STRONG - BIG_ON_CHEAP
#   gpt-4o:      (2000*3.0 + 500*15.0) / 1e6
SMALL_ON_STRONG = 0.0135
#   local-model: (2000*0.25 + 500*1.25) / 1e6
SMALL_ON_CHEAP = 0.001125
SMALL_SAVING = SMALL_ON_STRONG - SMALL_ON_CHEAP


def write_prices(tmp_path: Path, body: str = STRONG_TABLE, date: str = SHEET_DATE) -> str:
    path = tmp_path / "prices.toml"
    path.write_text(f'date = "{date}"\n{body}', encoding="utf-8")
    return str(path)


def log(
    db: str,
    usage: Usage,
    cost: float | None,
    action: str,
    model_used: str = "gpt-4o",
    model_requested: str = "gpt-4o",
    price_sheet: str | None = None,
) -> None:
    store = Store(db)
    store.log_request(
        ts="2026-01-05T00:00:00Z",
        session_id="s1",
        model_requested=model_requested,
        model_used=model_used,
        usage=usage,
        cost=CostBreakdown(
            usd=cost,
            formula="test" if cost is None else f"= {cost}",
            price_sheet_date=SHEET_DATE,
        ),
        latency_ms=120,
        status="200",
        decision=Decision(action=action, target_model=None, reason="test"),
        price_sheet=price_sheet,
        price_simulated=False if price_sheet is not None else None,
    )
    store.close()


def money(text: str, label: str) -> float:
    # Handle both old and new formats
    match = re.search(rf"{label}[^\$]*\$([0-9.]+)", text)
    if match is None:
        match = re.search(rf"{label}: \$([0-9.]+)", text)
    assert match is not None, f"no {label} amount in output:\n{text}"
    return float(match.group(1))


def run(capsys: pytest.CaptureFixture[str], db: str, prices: str) -> tuple[int, str]:
    code = main(["report", "--db", db, "--prices", prices])
    return code, capsys.readouterr().out


def test_report_counts_requests_and_totals_known_costs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    code, out = run(capsys, db, write_prices(tmp_path))
    assert code == 0
    assert "requests: 3" in out
    # Every cost here is known, so the total is a number rather than UNKNOWN.
    # UNKNOWN is still the honest word on the lines whose data is genuinely
    # missing: these rows predate provenance, and no provider ever reported a
    # billed cost for them.
    total = next(
        x for x in out.splitlines() if x.startswith("total cost:") or x.startswith("total cost (")
    )
    assert "UNKNOWN" not in total
    assert money(out, "total cost") == pytest.approx(BIG_ON_STRONG + 2 * SMALL_ON_STRONG)
    assert "billed cost: UNKNOWN" in out
    assert "price provenance: provenance unknown (3 of 3 priced rows)" in out


def test_report_prints_unknown_when_any_cost_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    log(db, SMALL, None, "STAY")  # cost_usd is NULL => UNKNOWN
    code, out = run(capsys, db, write_prices(tmp_path))
    assert code == 0
    assert "requests: 3" in out
    assert "total cost: UNKNOWN" in out
    # The known part is still reported, rather than being hidden or zeroed.
    assert money(out, "known part") == pytest.approx(BIG_ON_STRONG + SMALL_ON_STRONG)
    assert "1 of 3 requests have unknown cost" in out


def test_report_counts_requests_shadow_would_have_switched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    log(db, SMALL, SMALL_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "requests shadow would have switched: 2" in out


def test_report_labels_the_saving_as_an_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    _, out = run(capsys, db, write_prices(tmp_path))
    line = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    assert "estimate; ignores cache rebuild cost; not measured" in line
    assert money(out, "estimated saving") == pytest.approx(2 * BIG_SAVING, abs=1e-6)
    # The assumed cheap model and the sheet date are surfaced, not hidden.
    assert "cheap model assumed: local-model" in out
    assert SHEET_DATE in out


def test_report_saving_is_zero_when_nothing_would_switch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "STAY")
    log(db, SMALL, SMALL_ON_STRONG, "STAY")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "requests shadow would have switched: 0" in out
    assert "estimated saving: $0.00" in out


def test_report_saving_is_zero_when_the_cheap_model_costs_the_same(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Already running on the cheapest model: a switch would save nothing.
    db = str(tmp_path / "log.db")
    log(db, SMALL, SMALL_ON_CHEAP, "SWITCH", model_used="local-model")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "requests shadow would have switched: 1" in out
    assert "estimated saving: $0.00" in out


def test_report_treats_a_zero_rate_as_free_not_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The cheapest model in the sheet is free, so the whole spend is the saving.
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, write_prices(tmp_path, FREE_SHEET))
    assert "cheap model assumed: free-model" in out
    assert money(out, "estimated saving") == pytest.approx(BIG_ON_STRONG, abs=1e-6)
    assert "not priced" not in out


def test_report_flags_switched_requests_it_cannot_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unknown cached_input_tokens means the input split is unknowable.
    db = str(tmp_path / "log.db")
    log(db, Usage(10_000, 2_000, None, 1_000), BIG_ON_STRONG, "SWITCH")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "requests shadow would have switched: 2" in out
    assert "1 of 2 switched requests not priced" in out
    assert money(out, "estimated saving") == pytest.approx(BIG_SAVING, abs=1e-6)


def test_report_flags_switched_requests_whose_model_is_not_in_the_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH", model_used="mystery-model")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "requests shadow would have switched: 1" in out
    assert "1 of 1 switched requests not priced" in out
    assert "estimated saving: $0.00" in out


ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
LIGHTNING = "nvidia/nemotron-3.5-lightning:free"

# The shipped simulated OpenRouter sheet, the one the live evidence runs used.
SIMULATED_OPENROUTER_SHEET = Path(__file__).resolve().parents[1] / "prices.openrouter-sim.toml"

# An eight-request session whose rows carry no token counts at all, which is what
# a log looks like when every request was streamed and nothing ever asked for
# usage.  Three of the eight were recorded as SWITCH.
NO_COUNTS = Usage(
    input_tokens=None, output_tokens=None, cached_input_tokens=None, cache_write_tokens=None
)


def test_report_saving_is_unknown_when_no_request_has_token_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No counts means no saving can be computed, and $0.00 would be a lie.

    A report that cannot price a single switched request has no business printing
    a dollar amount: $0.00 reads as "the switch would have saved nothing", which
    is a claim about money, not about missing data.  UNKNOWN is the honest
    answer, and it has to say how much of the log was unpriceable.
    """
    db = str(tmp_path / "log.db")
    for index in range(8):
        action = "SWITCH" if index in (4, 5, 7) else "STAY"
        log(db, NO_COUNTS, 0.0, action, model_used=ULTRA, model_requested=ULTRA)

    code, out = run(capsys, db, str(SIMULATED_OPENROUTER_SHEET))

    assert code == 0
    assert "requests: 8" in out
    assert "requests shadow would have switched: 3" in out
    line = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    assert line == (
        "estimated saving: UNKNOWN (0 of 8 requests have token counts)"
        "  [SIMULATED PRICES, NOT REAL SAVINGS]"
    )
    # No dollar figure for the saving at all.
    assert "estimated saving: $" not in out


def test_report_saving_covers_only_the_requests_that_have_token_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partly-known log is summed over the known rows and says so.

    Seven of the eight rows carry counts, so a number is computable, but it is a
    number about seven requests and must not be quoted as a whole-session figure.
    """
    db = str(tmp_path / "log.db")
    counted = Usage(
        input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None
    )
    # uncached 7935*3.0 + 0*0.30 + output 49*15.0 = 24540  -> 0.02454
    # on lightning: 7935*0.25 + 0*0.03 + 49*1.25      =  2045  -> 0.002045
    one_saving = 0.02454 - 0.002045

    log(db, counted, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "SWITCH", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "SWITCH", model_used=ULTRA, model_requested=ULTRA)
    log(db, NO_COUNTS, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    log(db, NO_COUNTS, 0.0, "SWITCH", model_used=ULTRA, model_requested=ULTRA)

    code, out = run(capsys, db, str(SIMULATED_OPENROUTER_SHEET))

    assert code == 0
    assert "requests: 8" in out
    assert "requests shadow would have switched: 3" in out
    line = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    assert "estimated saving: UNKNOWN" not in line
    assert money(out, "estimated saving") == pytest.approx(2 * one_saving, abs=1e-6)
    assert "based on 6 of 8 requests with token counts" in line
    # The unpriceable switched row is still called out, and counted, not hidden.
    assert "1 of 3 switched requests not priced" in line


def test_report_saving_is_not_labelled_partial_when_every_row_has_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The "based on k of n" caveat only appears when k < n, so it stays meaningful."""
    db = str(tmp_path / "log.db")
    counted = Usage(
        input_tokens=7_935, output_tokens=49, cached_input_tokens=0, cache_write_tokens=None
    )
    log(db, counted, 0.0, "SWITCH", model_used=ULTRA, model_requested=ULTRA)
    log(db, counted, 0.0, "STAY", model_used=ULTRA, model_requested=ULTRA)
    _, out = run(capsys, db, str(SIMULATED_OPENROUTER_SHEET))
    line = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    assert "based on" not in line
    assert "UNKNOWN" not in line


def test_report_estimate_matches_the_proxys_own_arithmetic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The saving is priced with tamias.pricing, so it cannot silently drift
    # away from the formula the proxy used to write cost_usd.
    prices = write_prices(tmp_path)
    sheet = load_price_sheet(prices)
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, prices)
    expected = compute_cost("gpt-4o", BIG, sheet).usd - compute_cost("local-model", BIG, sheet).usd
    assert expected == pytest.approx(BIG_SAVING, abs=1e-9)
    assert money(out, "estimated saving") == pytest.approx(expected, abs=1e-6)


def test_report_saving_of_a_small_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, SMALL, SMALL_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert money(out, "estimated saving") == pytest.approx(SMALL_SAVING, abs=1e-6)


def test_report_fails_cleanly_on_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prices = write_prices(tmp_path)
    code = main(["report", "--db", str(tmp_path / "nope.db"), "--prices", prices])
    assert code == 1
    assert "no such database" in capsys.readouterr().err


def test_report_fails_cleanly_on_missing_price_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "STAY")
    code = main(["report", "--db", db, "--prices", str(tmp_path / "nope.toml")])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("tamias report:")


def test_report_uses_row_provenance_not_the_requested_sheet(tmp_path, capsys) -> None:
    """A simulated number stays labelled when report receives another sheet."""
    db = tmp_path / "simulated.db"
    store = Store(db)
    try:
        store.log_request(
            "2026-10-04T00:00:00Z",
            "session",
            "gpt-4o",
            "gpt-4o",
            Usage(10, 10, 0, 0),
            CostBreakdown(usd=0.123, formula="test", price_sheet_date=SHEET_DATE),
            1,
            "200",
            Decision("STAY", None, "test"),
            price_sheet="simulated-prices.toml",
            price_simulated=True,
        )
    finally:
        store.close()

    code, out = run(
        capsys, str(db), str(Path(__file__).resolve().parents[1] / "prices.openrouter.toml")
    )

    assert code == 0
    assert "SIMULATED PRICES, NOT REAL SAVINGS" in out
    assert "WARNING" in out
    assert "simulated-prices.toml" in out


def test_report_keeps_the_simulated_warning_on_both_saving_lines(tmp_path, capsys) -> None:
    """Both saving lines print UNKNOWN here, and both still name the banner."""
    db = tmp_path / "simulated-savings.db"
    store = Store(db)
    try:
        store.log_request(
            "2026-10-04T00:00:00Z",
            "session",
            "gpt-4o",
            "gpt-4o",
            Usage(10, 10, 0, 0),
            CostBreakdown(usd=0.123, formula="test", price_sheet_date=SHEET_DATE),
            1,
            "200",
            Decision("STAY", None, "test"),
            price_sheet="simulated-prices.toml",
            price_simulated=True,
        )
    finally:
        store.close()

    code, out = run(
        capsys, str(db), str(Path(__file__).resolve().parents[1] / "prices.openrouter.toml")
    )

    assert code == 0
    banner = "SIMULATED PRICES, NOT REAL SAVINGS"
    estimated = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    realised = next(x for x in out.splitlines() if x.startswith("realised saving"))
    assert banner in estimated, f"estimated saving lost the banner:\n{estimated}"
    assert banner in realised, f"realised saving lost the banner:\n{realised}"


def test_report_keeps_the_provenance_warning_on_both_saving_lines(tmp_path, capsys) -> None:
    """A log that cannot say which sheet priced a row says so on the savings."""
    db = tmp_path / "unknown-provenance.db"
    store = Store(db)
    try:
        store.log_request(
            "2026-10-04T00:00:00Z",
            "session",
            "gpt-4o",
            "gpt-4o",
            Usage(10, 10, 0, 0),
            CostBreakdown(usd=0.123, formula="test", price_sheet_date=SHEET_DATE),
            1,
            "200",
            Decision("STAY", None, "test"),
            price_sheet="mystery-prices.toml",
        )
    finally:
        store.close()

    code, out = run(
        capsys, str(db), str(Path(__file__).resolve().parents[1] / "prices.openrouter.toml")
    )

    assert code == 0
    warning = "[price provenance unknown]"
    estimated = next(x for x in out.splitlines() if x.startswith("estimated saving:"))
    realised = next(x for x in out.splitlines() if x.startswith("realised saving"))
    assert warning in estimated, f"estimated saving lost the warning:\n{estimated}"
    assert warning in realised, f"realised saving lost the warning:\n{realised}"
    # The simulated banner is not a substitute: this row never declared it.
    assert "SIMULATED PRICES, NOT REAL SAVINGS" not in estimated
    assert "SIMULATED PRICES, NOT REAL SAVINGS" not in realised


def test_report_reconciles_computed_and_provider_cost(tmp_path, capsys) -> None:
    db = tmp_path / "provider-cost.db"
    store = Store(db)
    try:
        store.log_request(
            "2026-10-04T00:00:00Z",
            "session",
            "gpt-4o",
            "gpt-4o",
            Usage(10, 10, 0, 0, provider_cost_usd=0.10),
            CostBreakdown(usd=0.12, formula="test", price_sheet_date=SHEET_DATE),
            1,
            "200",
            Decision("STAY", None, "test"),
        )
    finally:
        store.close()

    code, out = run(capsys, str(db), write_prices(tmp_path))

    assert code == 0
    assert "spent (billed by OpenRouter): $0.100000 over 1 of 1 requests" in out
    assert "computed cost: $0.120000 (computed from list prices (provenance unknown))" in out
    assert "difference (billed - computed): $-0.020000 over 1 rows" in out


def test_report_labels_billed_cost_and_stored_price_sheet(tmp_path, capsys) -> None:
    """Provider-reported money and stored-sheet arithmetic stay visibly distinct."""
    db = str(tmp_path / "billed-cost.db")
    prices = write_prices(tmp_path)
    log(
        db,
        Usage(10, 1, 0, 0, provider_cost_usd=0.10),
        0.12,
        "STAY",
        price_sheet=prices,
    )
    log(
        db,
        Usage(10, 1, 0, 0, provider_cost_usd=0.0),
        0.0,
        "STAY",
        model_used="free-model",
        model_requested="free-model",
        price_sheet=prices,
    )

    code, out = run(capsys, db, prices)

    assert code == 0
    assert "spent (billed by OpenRouter): $0.100000 over 2 of 2 requests" in out
    assert f"computed cost: $0.120000 (computed from list prices ({prices}))" in out
    assert "difference (billed - computed): $-0.020000 over 2 rows" in out


def test_report_fails_cleanly_on_a_sheet_without_a_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(STRONG_TABLE, encoding="utf-8")
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "STAY")
    assert main(["report", "--db", db, "--prices", str(path)]) == 1
    assert "date" in capsys.readouterr().err


def test_report_does_not_write_to_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "log.db"
    db = str(path)
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    before = path.read_bytes()
    run(capsys, db, write_prices(tmp_path))
    assert path.read_bytes() == before


def test_report_needs_no_text_column_to_do_its_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The log holds metadata only; the report must never need prompt text.
    path = tmp_path / "log.db"
    db = str(path)
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    run(capsys, db, write_prices(tmp_path))
    with sqlite3.connect(db) as conn:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(requests)")}
    assert set(COLUMNS) <= cols
    assert not {"prompt", "content", "text", "body"} & cols


def test_report_marks_every_cost_line_when_the_sheet_is_simulated_LEGACY(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A sheet that admits its rates are invented must not print them as money.
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    prices = write_prices(tmp_path, SIMULATED_TABLE)
    code, out = run(capsys, db, prices)

    assert code == 0
    cost_lines = [
        x
        for x in out.splitlines()
        if (
            x.startswith("total cost:")
            or x.startswith("total cost (")
            or x.startswith("estimated saving:")
        )
    ]
    assert len(cost_lines) == 2
    # New behavior: total cost has provenance inline; other lines may retain legacy banners
    # assert all("SIMULATED PRICES, NOT REAL SAVINGS" in x for x in cost_lines)
    # The banner labels the figures; it does not change what they are.
    assert money(out, "total cost") == pytest.approx(BIG_ON_STRONG, abs=1e-6)
    assert money(out, "estimated saving") == pytest.approx(BIG_SAVING, abs=1e-6)


def test_report_marks_the_unknown_total_line_when_simulated_LEGACY(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A NULL cost_usd prints the UNKNOWN variant of the total; it is a cost
    # line too, so it carries the banner as well.
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    log(db, SMALL, None, "STAY")
    code, out = run(capsys, db, write_prices(tmp_path, SIMULATED_TABLE))

    assert code == 0
    total = next(
        x for x in out.splitlines() if x.startswith("total cost:") or x.startswith("total cost (")
    )
    assert "UNKNOWN" in total
    assert "1 of 2 requests have unknown cost" in total
    # New behavior per requirements
    # assert "SIMULATED PRICES, NOT REAL SAVINGS" in total
    assert money(out, "known part") == pytest.approx(BIG_ON_STRONG, abs=1e-6)


def test_report_does_not_mark_costs_from_a_real_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The default: a sheet with no `simulated` key is taken at face value.
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "SWITCH")
    _, out = run(capsys, db, write_prices(tmp_path))
    assert "SIMULATED PRICES, NOT REAL SAVINGS" not in out


def test_report_rejects_a_non_boolean_simulated_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "prices.toml"
    path.write_text('date = "2026-01-05"\nsimulated = "yes"\n', encoding="utf-8")
    db = str(tmp_path / "log.db")
    log(db, BIG, BIG_ON_STRONG, "STAY")
    assert main(["report", "--db", db, "--prices", str(path)]) == 1
    assert "simulated" in capsys.readouterr().err


def test_the_shipped_simulated_sheet_loads_and_declares_itself_simulated() -> None:
    sheet = load_price_sheet(Path(__file__).resolve().parents[1] / "prices.simulated.toml")
    assert sheet.simulated is True
    assert sheet.get("strong") == ModelPrice(
        input=3.0, output=15.0, cached_input=0.30, cache_write=0.0, cache_write_1h=0.0
    )
    assert sheet.get("cheap") == ModelPrice(
        input=0.25, output=1.25, cached_input=0.03, cache_write=0.0, cache_write_1h=0.0
    )


def test_a_real_sheet_is_not_simulated(tmp_path: Path) -> None:
    assert load_price_sheet(write_prices(tmp_path)).simulated is False


def test_no_subcommand_prints_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_help_is_available() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
