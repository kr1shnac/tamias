"""Tests for the local dashboard: the data layer first, then the web layer.

Every database here is built with the real :class:`tamias.store.Store`, and
every amount is priced with the real :mod:`tamias.pricing` module, so the
dashboard is tested against the same code path the proxy and ``tamias report``
use.  The only substitution is the price sheet: a small TOML written per test
so a run never depends on a checked-in rate sheet being current.

The shape (a) fixture is the one the task pins down: eight requests, rows 5
and 8 rewritten onto the cheap model, the rest staying on the strong one.  The
two switched rows' savings must add up to 0.0302168.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tamias.dashboard import build_rows, create_dashboard_app, summary
from tamias.pricing import compute_cost, load_price_sheet
from tamias.store import Store
from tamias.types import Decision, Usage

STRONG = "nvidia/nemotron-3-ultra-550b-a55b:free"
CHEAP = "nvidia/nemotron-3.5-lightning:free"
FREE = "local/free-model"
SESSION = "dash-session"
SHEET_DATE = "2026-10-04"

# The invented rates of prices.openrouter-sim.toml, repeated here so the tests
# pin their own arithmetic instead of inheriting whatever the checked-in sheet
# says.  cache_write is 0.0, so a row that never reported cache writes is still
# priceable.
PRICES = f"""
simulated = true
date = "{SHEET_DATE}"

["{STRONG}"]
input = 3.0
output = 15.0
cached_input = 0.30
cache_write = 0.0

["{CHEAP}"]
input = 0.25
output = 1.25
cached_input = 0.03
cache_write = 0.0

["{FREE}"]
input = 0.0
output = 0.0
cached_input = 0.0
cache_write = 0.0
"""

REAL_PRICES = f"""
simulated = false
date = "{SHEET_DATE}"

["{STRONG}"]
input = 3.0
output = 15.0
cached_input = 0.30
cache_write = 0.0

["{CHEAP}"]
input = 0.25
output = 1.25
cached_input = 0.03
cache_write = 0.0

["{FREE}"]
input = 0.0
output = 0.0
cached_input = 0.0
cache_write = 0.0
"""


@pytest.fixture
def sheet_path(tmp_path: Path) -> Path:
    path = tmp_path / "prices-sim.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


@pytest.fixture
def sheet(sheet_path: Path):
    return load_price_sheet(sheet_path)


def _log(
    store: Store,
    *,
    ts: str,
    model_requested: str,
    model_used: str,
    usage: Usage,
    latency_ms: int | None,
    decision: Decision,
    sheet,
) -> int:
    """Append one request, storing the cost the proxy would have logged."""
    return store.log_request(
        ts=ts,
        session_id=SESSION,
        model_requested=model_requested,
        model_used=model_used,
        usage=usage,
        cost=compute_cost(model_used, usage, sheet),
        latency_ms=latency_ms,
        status="200",
        decision=decision,
        price_sheet=sheet.source,
        price_simulated=sheet.simulated,
    )


def _stay() -> Decision:
    return Decision(action="STAY", target_model=None, reason="no switch needed")


def _no_tokens() -> Usage:
    """Every count UNKNOWN, which is what an upstream that reports no usage gives."""
    return Usage(
        input_tokens=None,
        output_tokens=None,
        cached_input_tokens=None,
        cache_write_tokens=None,
    )


def _switched() -> Decision:
    return Decision(action="SWITCH", target_model=CHEAP, reason="easy tool call")


def _active_db(path: Path, sheet) -> Path:
    """The eight-request active shape: rows 5 and 8 land on the cheap model.

    Rows 5 and 8 carry the token counts the task pins, and the six unchanged
    rows carry counts of their own so the totals are not eight copies of one
    request.
    """
    store = Store(path)
    plan = [
        (1200, 80, 200),
        (980, 61, 0),
        (1430, 95, 512),
        (760, 44, 0),
        (7946, 46, 0),
        (1890, 72, 300),
        (640, 39, 0),
        (8434, 53, 6528),
    ]
    for index, (inp, out, cached) in enumerate(plan, start=1):
        switched = index in (5, 8)
        usage = Usage(
            input_tokens=inp,
            output_tokens=out,
            cached_input_tokens=cached,
            cache_write_tokens=None,
        )
        _log(
            store,
            ts=f"2026-10-04T10:{index:02d}:00",
            model_requested=STRONG,
            model_used=CHEAP if switched else STRONG,
            usage=usage,
            latency_ms=120 + index,
            decision=_switched() if switched else _stay(),
            sheet=sheet,
        )
    store.close()
    return path


# --- (a) the eight-row active shape ---------------------------------------


def test_two_switched_rows_save_exactly_the_expected_sum(tmp_path: Path, sheet) -> None:
    """Rows 5 and 8 are the only rewrites, and their savings sum to 0.0302168."""
    rows = build_rows(_active_db(tmp_path / "active.db", sheet), sheet)

    assert len(rows) == 8
    switched = [row for row in rows if row["switched"]]
    assert [row["id"] for row in switched] == [5, 8]
    assert sum(row["saved"] for row in switched) == pytest.approx(0.0302168, abs=1e-6)


def test_every_row_carries_the_fields_the_page_renders(tmp_path: Path, sheet) -> None:
    rows = build_rows(_active_db(tmp_path / "active.db", sheet), sheet)
    first = rows[0]
    for key in (
        "id",
        "session",
        "model_requested",
        "model_used",
        "decision",
        "effort_used",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "latency_ms",
        "actual_cost",
        "baseline_cost",
        "saved",
        "switched",
        "priced",
    ):
        assert key in first, f"build_rows dropped {key}"
    assert first["session"] == SESSION
    assert first["decision"] == "STAY"
    assert rows[4]["decision"] == "SWITCH"


def test_baseline_is_the_requested_model_priced_on_the_same_tokens(tmp_path: Path, sheet) -> None:
    """Baseline re-prices the row's own tokens, it does not invent new ones."""
    rows = build_rows(_active_db(tmp_path / "active.db", sheet), sheet)
    switched = rows[4]
    usage = Usage(
        input_tokens=switched["input_tokens"],
        output_tokens=switched["output_tokens"],
        cached_input_tokens=switched["cached_input_tokens"],
        cache_write_tokens=None,
    )
    assert switched["actual_cost"] == compute_cost(CHEAP, usage, sheet).usd
    assert switched["baseline_cost"] == compute_cost(STRONG, usage, sheet).usd
    assert switched["saved"] == switched["baseline_cost"] - switched["actual_cost"]
    assert switched["priced"] is True


def test_an_unswitched_row_saves_nothing(tmp_path: Path, sheet) -> None:
    rows = build_rows(_active_db(tmp_path / "active.db", sheet), sheet)
    staying = rows[0]
    assert staying["switched"] is False
    assert staying["actual_cost"] == staying["baseline_cost"]
    assert staying["saved"] == 0.0


# --- (b) every token count UNKNOWN ---------------------------------------


def _null_tokens_db(path: Path, sheet) -> Path:
    store = Store(path)
    for index, model in enumerate((STRONG, CHEAP), start=1):
        _log(
            store,
            ts=f"2026-10-04T11:{index:02d}:00",
            model_requested=STRONG,
            model_used=model,
            usage=Usage(
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
            ),
            latency_ms=None,
            decision=_stay(),
            sheet=sheet,
        )
    store.close()
    return path


def test_all_null_tokens_price_nothing_and_saving_pct_stays_unknown(tmp_path: Path, sheet) -> None:
    rows = build_rows(_null_tokens_db(tmp_path / "nulls.db", sheet), sheet)
    assert len(rows) == 2

    report = summary(rows, simulated=sheet.simulated)
    assert report["n_requests"] == 2
    assert report["n_priced"] == 0
    assert report["saving_pct"] is None


def test_unknown_counts_are_counted_not_dropped(tmp_path: Path, sheet) -> None:
    """An unpriced row is still a request: it appears in n_requests and n_priced."""
    rows = build_rows(_null_tokens_db(tmp_path / "nulls.db", sheet), sheet)
    report = summary(rows, simulated=sheet.simulated)

    assert report["n_requests"] == 2, "an unknown cost must not remove the request"
    assert report["n_unpriced"] == 2
    for row in rows:
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["actual_cost"] is None
        assert row["baseline_cost"] is None
        assert row["saved"] is None
        assert row["priced"] is False


def test_switched_is_counted_even_when_the_row_cannot_be_priced(tmp_path: Path, sheet) -> None:
    """A switch nobody can price is still a switch; only the money is unknown."""
    store = Store(tmp_path / "unpriced-switch.db")
    _log(
        store,
        ts="2026-10-04T12:00:00",
        model_requested=STRONG,
        model_used=CHEAP,
        usage=_no_tokens(),
        latency_ms=10,
        decision=_switched(),
        sheet=sheet,
    )
    store.close()

    rows = build_rows(tmp_path / "unpriced-switch.db", sheet)
    report = summary(rows, simulated=sheet.simulated)
    assert rows[0]["switched"] is True
    assert rows[0]["saved"] is None
    assert report["n_switched"] == 1
    assert report["share_cheap"] == 1.0
    assert report["saving_total"] == 0.0
    assert report["saving_pct"] is None


# --- (c) an old schema without the optional columns ----------------------

OLD_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT NOT NULL,
    model_requested TEXT NOT NULL,
    model_used TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,
    price_sheet_date TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_target_model TEXT,
    decision_reason TEXT NOT NULL
)
"""


def _old_schema_db(path: Path) -> Path:
    """A log written before latency_ms and the effort columns existed."""
    with sqlite3.connect(path) as conn:
        conn.execute(OLD_SCHEMA)
        conn.execute(
            """
            INSERT INTO requests
            (ts, session_id, model_requested, model_used, input_tokens, output_tokens,
             cached_input_tokens, cache_write_tokens, cost_usd, price_sheet_date,
             status, decision_action, decision_target_model, decision_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-10-04T09:00:00",
                SESSION,
                STRONG,
                CHEAP,
                7946,
                46,
                0,
                None,
                None,
                SHEET_DATE,
                "200",
                "SWITCH",
                CHEAP,
                "easy tool call",
            ),
        )
    return path


def test_old_schema_still_loads_with_the_optional_columns_as_none(tmp_path: Path, sheet) -> None:
    rows = build_rows(_old_schema_db(tmp_path / "old.db"), sheet)

    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] == 7946
    assert row["model_used"] == CHEAP
    assert row["decision"] == "SWITCH"
    assert row["switched"] is True
    for absent in ("latency_ms", "effort_requested", "effort_used"):
        assert row[absent] is None, f"{absent} should read as unknown, not missing"


def test_old_schema_row_is_still_priced(tmp_path: Path, sheet) -> None:
    """Losing latency_ms costs us a latency, never the cost arithmetic."""
    row = build_rows(_old_schema_db(tmp_path / "old.db"), sheet)[0]
    assert row["priced"] is True
    assert row["saved"] == pytest.approx(0.022484, abs=1e-6)


def test_stored_provenance_controls_the_dashboard_banner(tmp_path: Path, sheet) -> None:
    """A row priced with a simulated sheet remains simulated when displayed."""
    db = tmp_path / "stored-provenance.db"
    opened = Store(db)
    try:
        _log(
            opened,
            ts="2026-10-04T00:00:00Z",
            model_requested=STRONG,
            model_used=STRONG,
            usage=Usage(10, 2, 0, 0),
            latency_ms=1,
            decision=_stay(),
            sheet=sheet,
        )
    finally:
        opened.close()

    report = summary(build_rows(db, sheet), simulated=False)
    assert report["simulated"] is True
    assert report["price_sheets"] == [str(sheet.source)]
    assert report["provenance_unknown"] == 0


def test_summary_reconciles_provider_cost_on_rows_with_both_values() -> None:
    rows = [
        {
            "priced": True,
            "actual_cost": 0.12,
            "baseline_cost": 0.12,
            "switched": False,
            "provider_cost_usd": 0.10,
        }
    ]
    report = summary(rows)
    assert report["provider_billed_total"] == pytest.approx(0.10)
    assert report["provider_difference"] == pytest.approx(-0.02)


@pytest.mark.parametrize(
    ("rows", "billed_total", "billed_rows", "difference"),
    [
        pytest.param(
            [
                {"priced": True, "actual_cost": 0.12, "provider_cost_usd": 0.10},
                {"priced": True, "actual_cost": 0.20, "provider_cost_usd": 0.20},
            ],
            0.30,
            2,
            -0.02,
            id="all-billed",
        ),
        pytest.param(
            [
                {"priced": True, "actual_cost": 0.12, "provider_cost_usd": 0.10},
                {"priced": True, "actual_cost": 0.20, "provider_cost_usd": None},
            ],
            0.10,
            1,
            -0.02,
            id="partial-billed",
        ),
        pytest.param(
            [
                {"priced": True, "actual_cost": 0.12, "provider_cost_usd": None},
                {"priced": True, "actual_cost": 0.20, "provider_cost_usd": None},
            ],
            None,
            0,
            None,
            id="no-billed-rows",
        ),
        pytest.param(
            [{"priced": True, "actual_cost": 0.0, "provider_cost_usd": 0.0}],
            0.0,
            1,
            0.0,
            id="free-model-billed-zero",
        ),
    ],
)
def test_billed_summary_uses_only_provider_cost(
    rows: list[dict[str, object]],
    billed_total: float | None,
    billed_rows: int,
    difference: float | None,
) -> None:
    """Billed money is provider-reported; list-price arithmetic never fills it in."""
    report = summary([{"switched": False, "baseline_cost": 0.0, **row} for row in rows])

    assert report["provider_rows"] == billed_rows
    assert report["provider_missing"] == len(rows) - billed_rows
    assert report.get("provider_billed_total") == pytest.approx(billed_total)
    assert report.get("provider_difference") == pytest.approx(difference)


def test_dashboard_labels_billed_computed_and_difference() -> None:
    """The live page exposes the three separate accounting values per request."""
    from tamias import dashboard

    assert "Billed by OpenRouter" in dashboard.PAGE
    assert "Computed from list prices" in dashboard.PAGE
    assert "Difference (billed - computed)" in dashboard.PAGE
    assert "spent (billed by OpenRouter)" in dashboard.PAGE


def test_summary_warns_when_requested_sheet_differs_from_stored() -> None:
    rows = [
        {
            "priced": True,
            "actual_cost": 0.12,
            "baseline_cost": 0.12,
            "switched": False,
            "price_sheet": "stored.toml",
            "price_simulated": False,
        }
    ]
    report = summary(rows, requested_price_sheet="requested.toml")
    assert "WARNING" in report["price_sheet_warning"]
    assert "stored.toml" in report["price_sheet_warning"]


# --- (d) reading leaves the file alone -----------------------------------


def test_reading_never_writes_to_the_log(tmp_path: Path, sheet) -> None:
    """The dashboard watches a log the proxy is still writing to.

    It opens read-only, so the bytes it finds are the bytes it leaves: no
    journal, no vacuum, no updated header.  If it ever opened read-write, a
    poll every second would rewrite the page around the proxy's own writes.
    """
    path = _active_db(tmp_path / "active.db", sheet)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    build_rows(path, sheet)
    summary(build_rows(path, sheet), simulated=sheet.simulated)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_writable_database_is_opened_read_only(tmp_path: Path, sheet) -> None:
    """Even a writable file is refused a write, which is the point of mode=ro."""
    path = _active_db(tmp_path / "active.db", sheet)
    path.chmod(0o644)
    rows = build_rows(path, sheet)
    assert len(rows) == 8
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.execute("DELETE FROM requests")


# --- summary arithmetic --------------------------------------------------


def test_summary_totals_only_the_priced_rows(tmp_path: Path, sheet) -> None:
    """A row that cannot be priced must not quietly drag the totals to zero."""
    store = Store(tmp_path / "mixed.db")
    _log(
        store,
        ts="2026-10-04T13:00:00",
        model_requested=STRONG,
        model_used=CHEAP,
        usage=Usage(7946, 46, 0, None),
        latency_ms=100,
        decision=_switched(),
        sheet=sheet,
    )
    _log(
        store,
        ts="2026-10-04T13:01:00",
        model_requested=STRONG,
        model_used=STRONG,
        usage=_no_tokens(),
        latency_ms=None,
        decision=_stay(),
        sheet=sheet,
    )
    store.close()

    report = summary(build_rows(tmp_path / "mixed.db", sheet), simulated=sheet.simulated)
    assert report["n_requests"] == 2
    assert report["n_priced"] == 1
    assert report["n_unpriced"] == 1
    assert report["actual_total"] == pytest.approx(0.002044, abs=1e-9)
    assert report["baseline_total"] == pytest.approx(0.024528, abs=1e-9)
    assert report["saving_total"] == pytest.approx(0.022484, abs=1e-6)


def test_saving_pct_is_unknown_when_the_baseline_is_zero(tmp_path: Path, sheet) -> None:
    """A free baseline makes the ratio 0/0, which is not 0% saved."""
    store = Store(tmp_path / "zero.db")
    _log(
        store,
        ts="2026-10-04T14:00:00",
        model_requested=FREE,
        model_used=STRONG,
        usage=Usage(100, 10, 0, None),
        latency_ms=5,
        decision=_stay(),
        sheet=sheet,
    )
    store.close()

    report = summary(build_rows(tmp_path / "zero.db", sheet), simulated=sheet.simulated)
    assert report["n_priced"] == 1, "the row is priced; only the denominator is zero"
    assert report["baseline_total"] == 0.0
    assert report["actual_total"] > 0.0
    assert report["saving_pct"] is None


def test_share_cheap_is_the_switched_fraction(tmp_path: Path, sheet) -> None:
    report = summary(build_rows(_active_db(tmp_path / "active.db", sheet), sheet), simulated=False)
    assert report["n_requests"] == 8
    assert report["n_switched"] == 2
    assert report["share_cheap"] == 0.25


def test_cumulative_lines_agree_with_the_totals(tmp_path: Path, sheet) -> None:
    """The chart's last point is the headline number, or the chart lies."""
    report = summary(build_rows(_active_db(tmp_path / "active.db", sheet), sheet), simulated=False)
    assert len(report["cumulative_actual"]) == 8
    assert len(report["cumulative_baseline"]) == 8
    assert report["cumulative_actual"][-1] == pytest.approx(report["actual_total"], abs=1e-12)
    assert report["cumulative_baseline"][-1] == pytest.approx(report["baseline_total"], abs=1e-12)
    running = report["cumulative_actual"]
    assert all(later >= earlier for earlier, later in zip(running, running[1:], strict=False))


def test_cumulative_lines_hold_still_across_an_unpriced_row(tmp_path: Path, sheet) -> None:
    store = Store(tmp_path / "gap.db")
    _log(
        store,
        ts="2026-10-04T15:00:00",
        model_requested=STRONG,
        model_used=CHEAP,
        usage=Usage(7946, 46, 0, None),
        latency_ms=100,
        decision=_switched(),
        sheet=sheet,
    )
    _log(
        store,
        ts="2026-10-04T15:01:00",
        model_requested=STRONG,
        model_used=STRONG,
        usage=_no_tokens(),
        latency_ms=None,
        decision=_stay(),
        sheet=sheet,
    )
    store.close()

    report = summary(build_rows(tmp_path / "gap.db", sheet), simulated=sheet.simulated)
    assert report["cumulative_actual"] == [report["cumulative_actual"][0]] * 2
    assert report["cumulative_baseline"] == [report["cumulative_baseline"][0]] * 2


# --- an empty database ---------------------------------------------------


def test_an_empty_database_is_a_blank_page_not_an_error(tmp_path: Path, sheet) -> None:
    """The proxy creates the log before its first request, so zero rows is normal."""
    empty = tmp_path / "empty.db"
    Store(empty).close()

    rows = build_rows(empty, sheet)
    assert rows == []

    report = summary(rows, simulated=sheet.simulated)
    assert report["n_requests"] == 0
    assert report["n_priced"] == 0
    assert report["n_switched"] == 0
    assert report["n_unpriced"] == 0
    assert report["share_cheap"] is None
    assert report["saving_pct"] is None
    assert report["actual_total"] == 0.0
    assert report["baseline_total"] == 0.0
    assert report["saving_total"] == 0.0
    assert report["cumulative_actual"] == []
    assert report["cumulative_baseline"] == []


def test_a_missing_database_reads_as_empty(tmp_path: Path, sheet) -> None:
    assert build_rows(tmp_path / "not-created-yet.db", sheet) == []


# --- the web layer --------------------------------------------------------


def test_the_page_is_served_and_says_what_it_is(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Tamias" in body
    assert "Tamias live routing" in body


def test_the_page_is_one_self_contained_document(tmp_path: Path, sheet_path: Path) -> None:
    """No CDN, no external font, no fetched script: the page runs offline.

    The only absolute URL it may mention is the SVG namespace, which is an XML
    identifier the browser never fetches.
    """
    app = create_dashboard_app(tmp_path / "empty.db", sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text

    assert "<script src" not in body
    assert "<link" not in body
    assert "@import" not in body
    assert "url(http" not in body
    for url in re.findall(r"https?://[^\s\"'()<>]+", body):
        assert url == "http://www.w3.org/2000/svg", f"the page would fetch {url}"


def test_the_page_never_assembles_html_from_data(tmp_path: Path, sheet_path: Path) -> None:
    """innerHTML would turn a logged model name into markup.

    The log is metadata, but a model id is still text from the outside world,
    so the page is built with textContent and the table is written empty.
    """
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text

    # Assignments, not the words: the script explains itself in comments.
    for pattern in (
        r"\.innerHTML\s*=[^=]",
        r"\.outerHTML\s*=[^=]",
        r"insertAdjacentHTML\s*\(",
        r"document\.write\s*\(",
    ):
        assert not re.search(pattern, body), f"{pattern} would turn logged text into markup"
    assert "textContent" in body


def test_a_simulated_sheet_banners_every_amount(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text

    assert "SIMULATED PRICES - NOT REAL SAVINGS" in body


def test_the_simulated_banner_ships_hidden_and_is_raised_only_by_the_api(
    tmp_path: Path, sheet_path: Path
) -> None:
    """The banner is a live region the summary flag switches on.

    It is present in the document for every sheet, so the string alone proves
    nothing; what matters is that it starts hidden and that only a sheet which
    declares itself simulated flips it.
    """
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text
        summary_payload = client.get("/api/summary").json()

    assert 'id="sim-banner"' in body
    assert re.search(r'id="sim-banner"[^>]*\shidden', body), "the banner must start hidden"
    assert summary_payload["simulated"] is True


def test_the_endpoints_agree_with_the_data_layer(tmp_path: Path, sheet_path: Path) -> None:
    """What the page draws is what build_rows and summary computed."""
    sheet = load_price_sheet(sheet_path)
    path = _active_db(tmp_path / "active.db", sheet)
    app = create_dashboard_app(path, sheet_path)

    with TestClient(app) as client:
        served_rows = client.get("/api/rows").json()
        served_summary = client.get("/api/summary").json()

    rows = build_rows(path, sheet)
    expected = summary(rows, simulated=sheet.simulated)

    assert len(served_rows) == len(rows) == 8
    assert [r["id"] for r in served_rows] == [r["id"] for r in rows]
    assert [r["switched"] for r in served_rows] == [r["switched"] for r in rows]
    assert [r["model_used"] for r in served_rows] == [r["model_used"] for r in rows]
    assert sum(r["saved"] for r in served_rows if r["switched"]) == pytest.approx(
        0.0302168, abs=1e-6
    )

    assert served_summary["n_requests"] == expected["n_requests"] == 8
    assert served_summary["n_priced"] == expected["n_priced"]
    assert served_summary["n_switched"] == expected["n_switched"] == 2
    assert served_summary["share_cheap"] == expected["share_cheap"] == 0.25
    assert served_summary["actual_total"] == pytest.approx(expected["actual_total"], abs=1e-12)
    assert served_summary["baseline_total"] == pytest.approx(expected["baseline_total"], abs=1e-12)
    assert served_summary["saving_pct"] == pytest.approx(expected["saving_pct"], abs=1e-12)


def test_the_page_survives_an_empty_database(tmp_path: Path, sheet_path: Path) -> None:
    empty = tmp_path / "empty.db"
    Store(empty).close()
    app = create_dashboard_app(empty, sheet_path)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/rows").json() == []
        report = client.get("/api/summary").json()

    assert report["n_requests"] == 0
    assert report["saving_pct"] is None


def test_a_real_sheet_cannot_hide_a_banner_the_stored_rows_earned(tmp_path: Path) -> None:
    """The banner follows the rows, not the sheet the dashboard is pointed at.

    The amounts on the page were priced when the rows were written, so the flag
    that labels them is the one stored beside each row.  Handing the page a real
    sheet afterwards changes what it re-prices with; it does not relabel a
    simulated number that is already sitting in the log.
    """
    sim = tmp_path / "prices-sim.toml"
    sim.write_text(PRICES, encoding="utf-8")
    real = tmp_path / "prices-real.toml"
    real.write_text(REAL_PRICES, encoding="utf-8")
    db = _active_db(tmp_path / "active.db", load_price_sheet(sim))

    with TestClient(create_dashboard_app(db, sim)) as client:
        simulated_sheet = client.get("/api/summary").json()

    with TestClient(create_dashboard_app(db, real)) as client:
        real_sheet = client.get("/api/summary").json()

    assert simulated_sheet["simulated"] is True
    assert real_sheet["simulated"] is True, "a real sheet hid a simulated stored number"
    # The rows still say which sheet priced them, and the mismatch is called out.
    assert real_sheet["price_sheets"] == [str(sim)]
    assert "prices-sim.toml" in real_sheet["price_sheet_warning"]
    assert real_sheet["provenance_unknown"] == 0


def test_a_simulated_sheet_cannot_raise_a_banner_the_rows_have_not_earned(
    tmp_path: Path,
) -> None:
    """The other direction: the sheet argument cannot fabricate a banner either.

    Rows written with a real sheet recorded that they were, so the page's own
    arithmetic being an illustration is no reason to call their stored numbers
    simulated.  A banner that appears here would be tamias warning about money it
    has already proven is real.
    """
    sim = tmp_path / "prices-sim.toml"
    sim.write_text(PRICES, encoding="utf-8")
    real = tmp_path / "prices-real.toml"
    real.write_text(REAL_PRICES, encoding="utf-8")
    db = _active_db(tmp_path / "active.db", load_price_sheet(real))

    with TestClient(create_dashboard_app(db, real)) as client:
        real_sheet = client.get("/api/summary").json()

    with TestClient(create_dashboard_app(db, sim)) as client:
        simulated_sheet = client.get("/api/summary").json()

    assert "simulated" not in real_sheet
    assert "simulated" not in simulated_sheet, "the sheet argument faked the banner"
    assert simulated_sheet["price_sheets"] == [str(real)]
    assert simulated_sheet["provenance_unknown"] == 0
    assert "WARNING" in simulated_sheet["price_sheet_warning"]


def test_a_legacy_row_is_reported_as_provenance_unknown(tmp_path: Path, sheet) -> None:
    """A row with no stored flag cannot be attributed to any sheet.

    It was written before provenance existed, so the honest summary is "unknown",
    not a borrowed sheet name -- and, with a real sheet in hand, no banner.
    """
    rows = build_rows(_old_schema_db(tmp_path / "old.db"), sheet)

    report = summary(rows, simulated=False)

    assert report["n_priced"] == 1
    assert report["provenance_unknown"] == 1
    assert report["price_sheets"] == []
    assert "simulated" not in report
    assert "price_sheet_warning" not in report

    # The one legacy case that still raises it: when the stored rows cannot say
    # anything at all, a sheet that declares itself simulated is labelled as the
    # clearly marked fallback it is.
    assert summary(rows, simulated=True)["simulated"] is True


def test_live_polling_and_replay_controls_are_offered(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text

    for control in ("Live", "Replay", "Play", "Pause", "Restart"):
        assert control in body, f"the {control} control is missing"
    for speed in ("0.5", "1", "2", "5"):
        assert f'data-speed="{speed}"' in body
    assert "api/rows" in body, "live mode has nothing to poll"


def test_a_compare_run_is_drawn_as_a_third_line(tmp_path: Path, sheet_path: Path) -> None:
    """--compare-db is the measured run; the baseline line is the estimate."""
    sheet = load_price_sheet(sheet_path)
    baseline_db = _active_db(tmp_path / "compare.db", sheet)
    app = create_dashboard_app(
        _active_db(tmp_path / "active.db", sheet), sheet_path, compare_db=baseline_db
    )

    with TestClient(app) as client:
        report = client.get("/api/summary").json()
        body = client.get("/").text

    expected = summary(build_rows(baseline_db, sheet), simulated=sheet.simulated)
    assert report["compare_total"] == pytest.approx(expected["actual_total"], abs=1e-12)
    assert "compare" in body


def test_no_compare_run_means_no_compare_numbers(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        assert "compare_total" not in client.get("/api/summary").json()


def test_the_app_takes_a_fresh_read_on_every_request(tmp_path: Path, sheet_path: Path) -> None:
    """Live mode has to see rows logged after the server started."""
    sheet = load_price_sheet(sheet_path)
    db = tmp_path / "growing.db"
    Store(db).close()
    app = create_dashboard_app(db, sheet_path)

    with TestClient(app) as client:
        assert client.get("/api/rows").json() == []
        _log(
            Store(db),
            ts="2026-10-04T16:00:00",
            model_requested=STRONG,
            model_used=CHEAP,
            usage=Usage(7946, 46, 0, None),
            latency_ms=42,
            decision=_switched(),
            sheet=sheet,
        )
        assert len(client.get("/api/rows").json()) == 1


# --- the command line -----------------------------------------------------


def test_every_element_the_script_looks_up_exists_in_the_page(
    tmp_path: Path, sheet_path: Path
) -> None:
    """Every getElementById in the script must resolve to an element.

    A typo here is invisible to every other test: the page still returns 200 and
    every API test still passes, but the first missing reference throws a
    TypeError on load and the page renders half-blank.  So the ids the script
    asks for are read out of the script and checked against the markup.
    """
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    with TestClient(create_dashboard_app(db, sheet_path)) as client:
        body = client.get("/").text

    script = re.search(r"<script>(.*?)</script>", body, re.S).group(1)
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    assert wanted, "no element lookups found; the script shape changed"
    for element_id in sorted(wanted):
        assert f'id="{element_id}"' in body, (
            f"the script reads #{element_id}, which is not in the page"
        )


def test_every_element_the_script_uses_is_bound_before_use(
    tmp_path: Path, sheet_path: Path
) -> None:
    """``el.foo`` must exist in the element map, or the page throws on load.

    Writing ``el.cardSavingNote.textContent`` without ever adding cardSavingNote
    to the map leaves ``undefined`` there, and the assignment raises a TypeError
    that silently kills the rest of the render.  Nothing else notices: the page
    still serves 200 and every endpoint still answers, so the gap is only
    visible if the map and its uses are compared against each other.
    """
    app = create_dashboard_app(tmp_path / "empty.db", sheet_path)
    with TestClient(app) as client:
        body = client.get("/").text

    script = re.search(r"<script>(.*?)</script>", body, re.S).group(1)
    mapping = re.search(r"var el = \{(.*?)\n  \};", script, re.S)
    assert mapping, "the element map is gone; this test needs updating"
    bound = set(re.findall(r"^\s{4}(\w+):", mapping.group(1), re.M))
    used = set(re.findall(r"\bel\.(\w+)", script))

    assert bound, "the element map is empty"
    unbound = sorted(used - bound)
    assert not unbound, f"used but never bound in the el map: {', '.join(unbound)}"


def test_the_entry_point_takes_the_documented_flags() -> None:
    from tamias.dashboard import build_parser

    args = build_parser().parse_args(
        ["--db", "r.db", "--prices", "p.toml", "--compare-db", "c.db", "--port", "8010"]
    )
    assert args.db == "r.db"
    assert args.prices == "p.toml"
    assert args.compare_db == "c.db"
    assert args.port == 8010


def test_the_entry_point_binds_loopback_and_nothing_else() -> None:
    """The log holds a session's traffic; the page is not for the network."""
    from tamias.dashboard import build_parser

    args = build_parser().parse_args(["--db", "r.db", "--prices", "p.toml"])
    assert args.host == "127.0.0.1"
    assert not hasattr(args, "compare_db") or args.compare_db is None


def test_module_entry_point_exists() -> None:
    """`python -m tamias.dashboard` has to be a real command, not a docstring."""
    import tamias.dashboard as dashboard

    assert callable(dashboard.main)
    assert dashboard.__doc__ is not None

def test_grouping_by_project_field(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE requests ADD COLUMN project TEXT")
        conn.execute("UPDATE requests SET project = 'p1' WHERE id <= 4")
        conn.execute("UPDATE requests SET project = 'p2' WHERE id > 4")
        
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        nav = client.get("/api/nav").json()
        assert "p1" in nav["projects"]
        assert nav["projects"]["p1"]["count"] == 4
        assert nav["projects"]["p2"]["count"] == 4
        
        rows_p1 = client.get("/api/rows?project=p1").json()
        assert len(rows_p1) == 4
        
        summary_p2 = client.get("/api/summary?project=p2").json()
        assert summary_p2["n_requests"] == 4

def test_grouping_by_session_when_no_project_field(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        nav = client.get("/api/nav").json()
        assert "dash-session" in nav["projects"]
        assert nav["projects"]["dash-session"]["count"] == 8
        
        rows = client.get("/api/rows?project=dash-session").json()
        assert len(rows) == 8
        
        empty = client.get("/api/rows?project=valid-but-unknown").json()
        assert empty == []

def test_invalid_and_unknown_filter_values(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        assert client.get("/api/rows?project=invalid/path").status_code == 400
        assert client.get("/api/summary?session=invalid!value").status_code == 400
        assert client.get("/api/rows?project=valid_unknown").json() == []

def test_no_inner_html_in_served_page(tmp_path: Path, sheet_path: Path) -> None:
    db = _active_db(tmp_path / "active.db", load_price_sheet(sheet_path))
    app = create_dashboard_app(db, sheet_path)
    with TestClient(app) as client:
        html = client.get("/").text
        assert "innerHTML" not in html

