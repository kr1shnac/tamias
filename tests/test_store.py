"""Tests for the SQLite request log.

The two properties that matter here are that UNKNOWN stays distinguishable
from zero after a round trip through the database, and that the schema has
nowhere to put prompt or response text.  No test touches the network.
"""

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias.store import COLUMNS, Store  # noqa: E402
from tamias.types import CostBreakdown, Decision, Usage  # noqa: E402

SHEET_DATE = "2026-10-04"
TS = "2026-10-04T12:00:00Z"

KNOWN_USAGE = Usage(1_000, 200, 200, 0)
UNKNOWN_USAGE = Usage(None, None, None, None)
KNOWN_COST = CostBreakdown(0.00246, "usd = (0.00246)", SHEET_DATE)
UNKNOWN_COST = CostBreakdown(None, "usd = unknown: output", SHEET_DATE)
STAY = Decision(action="STAY", target_model=None, reason="already on the cheap model")
SWITCH = Decision(
    action="SWITCH", target_model="model-b", reason="request 3 of 3", persisted_only=True
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "requests.sqlite")
    try:
        yield opened
    finally:
        opened.close()


def log(
    store: Store,
    usage: Usage = KNOWN_USAGE,
    cost: CostBreakdown = KNOWN_COST,
    decision: Decision = STAY,
    latency_ms: int | None = 120,
    ts: str = TS,
) -> int:
    return store.log_request(
        ts,
        "session-1",
        "model-a",
        "model-a",
        usage,
        cost,
        latency_ms,
        "200",
        decision,
    )


def test_log_request_persists_every_field(store: Store) -> None:
    row_id = log(store)

    (row,) = store.rows()
    assert row["id"] == row_id == 1
    assert row["ts"] == TS
    assert row["session_id"] == "session-1"
    assert row["model_requested"] == "model-a"
    assert row["model_used"] == "model-a"
    assert row["input_tokens"] == 1_000
    assert row["output_tokens"] == 200
    assert row["cached_input_tokens"] == 200
    assert row["cache_write_tokens"] == 0
    assert row["cost_usd"] == 0.00246
    assert row["price_sheet_date"] == SHEET_DATE
    assert row["latency_ms"] == 120
    assert row["status"] == "200"
    assert row["decision_action"] == "STAY"
    assert row["decision_target_model"] is None
    assert row["decision_reason"] == "already on the cheap model"


def test_unknown_fields_are_stored_as_null_never_as_zero(store: Store) -> None:
    log(store, usage=UNKNOWN_USAGE, cost=UNKNOWN_COST, latency_ms=None)

    (row,) = store.rows()
    for field in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens"):
        assert row[field] is None, f"{field} must be NULL, not 0"
        assert row[field] != 0
    assert row["cost_usd"] is None
    assert row["latency_ms"] is None


def test_nulls_survive_a_reopen_of_the_database_file(tmp_path: Path) -> None:
    path = tmp_path / "requests.sqlite"
    first = Store(path)
    try:
        log(first, usage=UNKNOWN_USAGE, cost=UNKNOWN_COST, latency_ms=None)
    finally:
        first.close()

    # Read the file with plain sqlite3 to prove the values really are NULL on
    # disk, not merely None through this module's row factory.
    with sqlite3.connect(path) as reader:
        stored = reader.execute(
            "SELECT input_tokens, output_tokens, cached_input_tokens, cache_write_tokens,"
            " cost_usd, latency_ms, price_sheet_date FROM requests"
        ).fetchone()
    assert stored == (None, None, None, None, None, None, SHEET_DATE)


def test_zero_fields_are_stored_as_zero_not_as_null(store: Store) -> None:
    # Zero is a real measurement and must not be confused with unknown.
    log(
        store,
        usage=Usage(0, 0, 0, 0),
        cost=CostBreakdown(0.0, "usd = 0.0", SHEET_DATE),
        latency_ms=0,
    )

    (row,) = store.rows()
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)
    assert (row["cached_input_tokens"], row["cache_write_tokens"]) == (0, 0)
    assert row["cost_usd"] == 0.0
    assert row["latency_ms"] == 0


def test_stay_decision_records_no_target_model(store: Store) -> None:
    # A STAY decision has no target, so the column stays NULL rather than
    # repeating the model that was already in use.
    log(store, decision=STAY)

    (row,) = store.rows()
    assert row["decision_action"] == "STAY"
    assert row["decision_target_model"] is None


def test_shadow_decision_is_recorded_alongside_the_model_actually_used(store: Store) -> None:
    # In shadow mode the switch was not acted on, but it is still logged, and
    # model_used still names the model that really served the request.
    store.log_request(
        TS,
        "session-1",
        "model-a",
        "model-a",
        KNOWN_USAGE,
        KNOWN_COST,
        120,
        "200",
        SWITCH,
    )

    (row,) = store.rows()
    assert row["model_requested"] == "model-a"
    assert row["model_used"] == "model-a"
    assert row["decision_action"] == "SWITCH"
    assert row["decision_target_model"] == "model-b"
    assert row["decision_reason"] == "request 3 of 3"


def test_store_creates_the_database_file_and_table(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "requests.sqlite"

    created = Store(path)
    try:
        assert path.exists()
        assert created.rows() == []
    finally:
        created.close()

    # Reopening the same path must not fail on the existing table.
    reopened = Store(path)
    try:
        log(reopened, usage=UNKNOWN_USAGE, cost=UNKNOWN_COST)
        assert len(reopened.rows()) == 1
    finally:
        reopened.close()


def test_rows_are_returned_oldest_first(store: Store) -> None:
    log(store, ts="2026-10-04T12:00:00Z")
    log(store, ts="2026-10-04T12:00:01Z")
    log(store, ts="2026-10-04T12:00:02Z")

    ids = [row["id"] for row in store.rows()]
    timestamps = [row["ts"] for row in store.rows()]
    assert ids == sorted(ids)
    assert timestamps == [
        "2026-10-04T12:00:00Z",
        "2026-10-04T12:00:01Z",
        "2026-10-04T12:00:02Z",
    ]


def test_schema_has_no_column_for_request_or_response_text(store: Store) -> None:
    log(store)
    (row,) = store.rows()

    # Every string in the row is one of the fixed metadata fields below: there
    # is no column that could hold a prompt, a response or any other free text.
    text_columns = {
        key for key, value in zip(row.keys(), row, strict=True) if isinstance(value, str)
    }
    assert text_columns == {
        "ts",
        "session_id",
        "model_requested",
        "model_used",
        "price_sheet_date",
        "status",
        "decision_action",
        "decision_reason",
    }
    for forbidden in ("prompt", "content", "text", "message", "body", "completion"):
        assert forbidden not in " ".join(row.keys())


def test_columns_are_the_documented_set(store: Store) -> None:
    log(store)
    (row,) = store.rows()

    assert tuple(row.keys()) == ("id", *COLUMNS)
    assert COLUMNS == (
        "ts",
        "session_id",
        "model_requested",
        "model_used",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "cost_usd",
        "price_sheet_date",
        "latency_ms",
        "status",
        "decision_action",
        "decision_target_model",
        "decision_reason",
        "effort_requested",
        "effort_used",
        "decision_target_effort",
    )
