"""The budget ledger's arithmetic: reserve, settle, caps, rates, windows.

These tests pin the claims :mod:`tamias.budget` makes: a reservation is refused
before a limit would be exceeded, settlement records what was really spent, a
per-model sub-cap is independent of the key's total, and both rate windows count
what is in flight as well as what has been logged.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias.budget import (
    DEFAULT_RESERVATION_TTL_SECONDS,
    RATE_WINDOW_SECONDS,
    BudgetError,
    BudgetLedger,
    Limits,
    estimate_tokens,
    from_micros,
    parse_usd,
    to_micros,
)
from tamias.keys import KeysError, KeyStore

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
CEILING = Decimal("0.25")
# Any cutoff early enough to include every row this suite writes.
EPOCH = "1970-01-01T00:00:00.000+00:00"


@pytest.fixture
def store() -> KeyStore:
    key_store = KeyStore(":memory:")
    yield key_store
    key_store.close()


@pytest.fixture
def ledger(store: KeyStore) -> BudgetLedger:
    return store.budget()


# --- parsing ------------------------------------------------------------------


def test_usd_round_trips_through_micro_dollars() -> None:
    assert to_micros(Decimal("1.234567")) == 1_234_567
    assert from_micros(1_234_567) == Decimal("1.234567")
    assert to_micros(Decimal("0")) == 0
    # Half a micro-dollar rounds to a whole one rather than being truncated away.
    assert to_micros(Decimal("0.0000005")) == 1


@pytest.mark.parametrize("value", ["-1", "-0.01", float("nan"), float("inf"), "abc"])
def test_an_unusable_usd_amount_is_refused(value: object) -> None:
    with pytest.raises(BudgetError):
        parse_usd(value)


def test_absent_and_zero_usd_are_distinct() -> None:
    assert parse_usd(None) is None
    assert parse_usd("") is None
    assert parse_usd("0") == Decimal("0")


def test_tokens_are_estimated_from_the_body_and_never_zero_for_a_body() -> None:
    assert estimate_tokens(None) == 0
    assert estimate_tokens(b"") == 0
    assert estimate_tokens(b"x") == 1
    assert estimate_tokens(b"x" * 400) == 100


def test_limits_report_whether_they_need_a_price() -> None:
    assert not Limits().enforced
    assert not Limits().priced
    assert Limits(rpm=5).enforced and not Limits(rpm=5).priced
    assert Limits(tpm=5).enforced and not Limits(tpm=5).priced
    assert Limits(budget_usd=Decimal("1")).enforced and Limits(budget_usd=Decimal("1")).priced
    caps = Limits(model_caps=(("m", Decimal("1")),))
    assert caps.enforced and caps.priced
    assert caps.model_cap("m") == Decimal("1")
    assert caps.model_cap("other") is None
    assert caps.model_cap(None) is None


# --- reserve and settle -------------------------------------------------------


def test_a_reservation_is_refused_when_it_would_exceed_the_budget(
    ledger: BudgetLedger,
) -> None:
    key = "vk-1"
    budget = Limits(budget_usd=Decimal("0.30"))
    first = ledger.try_reserve(key, limits=budget, model="m", ceiling_usd=CEILING, now=NOW)
    assert first.allowed
    # 0 settled + 0.25 reserved + 0.25 needed = 0.50 > 0.30: refused.
    second = ledger.try_reserve(key, limits=budget, model="m", ceiling_usd=CEILING, now=NOW)
    assert not second.allowed
    assert second.reason == "budget"
    assert second.cap_usd == Decimal("0.30")
    assert second.reserved_usd == Decimal("0.25")
    assert second.remaining_usd == Decimal("0.05")


def test_settlement_spends_the_real_cost_not_the_ceiling(ledger: BudgetLedger) -> None:
    key = "vk-1"
    budget = Limits(budget_usd=Decimal("0.35"))
    assert ledger.try_reserve(key, limits=budget, model="m", ceiling_usd=CEILING, now=NOW).allowed
    assert ledger.settle(key, usd=Decimal("0.10"), model="m", tokens=7, now=NOW) is True
    assert ledger.spend_micros(key, None) == to_micros(Decimal("0.10"))
    # The reservation is released, so only the real cost is held against the key:
    # 0.10 spent + 0 held + 0.25 needed = 0.35 <= 0.35.
    assert ledger.reserved_micros(key, EPOCH) == 0
    assert ledger.try_reserve(key, limits=budget, model="m", ceiling_usd=CEILING, now=NOW).allowed


def test_a_budget_smaller_than_one_ceiling_admits_nothing(ledger: BudgetLedger) -> None:
    small = Limits(budget_usd=Decimal("0.10"))
    refused = ledger.try_reserve("vk-1", limits=small, model="m", ceiling_usd=CEILING, now=NOW)
    assert not refused.allowed
    assert refused.reason == "budget"
    assert refused.spent_usd == 0
    assert refused.remaining_usd == Decimal("0.10")


def test_settlement_always_records_an_event_even_for_a_free_request(
    ledger: BudgetLedger,
) -> None:
    ledger.settle("vk-1", usd=Decimal("0"), model="m", tokens=5, now=NOW)
    assert ledger.spend_micros("vk-1", None) == 0
    assert ledger.requests_in_window("vk-1", EPOCH) == 1


def test_settlement_uses_the_reservation_estimate_when_usage_is_unreported(
    ledger: BudgetLedger,
) -> None:
    """An unmeasured request is never counted as zero tokens."""
    key = "vk-1"
    ledger.try_reserve(
        key, limits=Limits(rpm=5), model="m", ceiling_usd=CEILING, token_estimate=42, now=NOW
    )
    assert ledger.settle(key, usd=Decimal("0"), model="m", tokens=None, now=NOW) is True
    assert ledger.tokens_in_window(key, EPOCH) == 42


def test_settlement_matches_the_model_so_sub_caps_stay_independent(
    ledger: BudgetLedger,
) -> None:
    key = "vk-1"
    limits = Limits(
        budget_usd=Decimal("100"), model_caps=(("a", Decimal("1")), ("b", Decimal("1")))
    )
    assert ledger.try_reserve(key, limits=limits, model="a", ceiling_usd=CEILING, now=NOW).allowed
    assert ledger.try_reserve(key, limits=limits, model="b", ceiling_usd=CEILING, now=NOW).allowed
    # Settling b must release b's reservation, not a's.
    ledger.settle(key, usd=Decimal("0.5"), model="b", tokens=1, now=NOW)
    assert ledger.reserved_micros(key, EPOCH, "a") == to_micros(CEILING)
    assert ledger.reserved_micros(key, EPOCH, "b") == 0
    assert ledger.spend_micros(key, None, "b") == to_micros(Decimal("0.5"))
    assert ledger.spend_micros(key, None, "a") == 0


def test_a_model_sub_cap_is_enforced_independently_of_the_total(
    ledger: BudgetLedger,
) -> None:
    key = "vk-1"
    limits = Limits(budget_usd=Decimal("100"), model_caps=(("cheap", Decimal("0.30")),))
    assert ledger.try_reserve(
        key, limits=limits, model="cheap", ceiling_usd=CEILING, now=NOW
    ).allowed
    refused = ledger.try_reserve(
        key, limits=limits, model="cheap", ceiling_usd=CEILING, now=NOW
    )
    assert not refused.allowed and refused.reason == "model_cap"
    assert refused.cap_usd == Decimal("0.30")
    # The same key still reaches a model with no cap: the total is untouched.
    assert ledger.try_reserve(
        key, limits=limits, model="other", ceiling_usd=CEILING, now=NOW
    ).allowed


def test_the_period_is_a_window_over_the_events(ledger: BudgetLedger) -> None:
    key = "vk-1"
    limits = Limits(budget_usd=Decimal("0.30"), period_days=7)
    ledger.settle(key, usd=Decimal("0.30"), model="m", tokens=1, now=NOW)
    # Inside the window the spend counts, so another ceiling does not fit.
    assert not ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW
    ).allowed
    # Past the window it does not.
    later = NOW + timedelta(days=8)
    assert ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=later
    ).allowed


def test_an_unknown_cost_is_charged_the_ceiling_not_zero(ledger: BudgetLedger) -> None:
    """A request the sheet could not price is not allowed to read as free."""
    key = "vk-1"
    limits = Limits(budget_usd=Decimal("0.30"))
    assert ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW).allowed
    assert ledger.settle(key, usd=None, model="m", tokens=5, now=NOW) is True
    assert ledger.spend_micros(key, None) == to_micros(CEILING)
    # With no reservation to charge against, there is nothing to charge.
    ledger.settle("vk-2", usd=None, model="m", tokens=5, now=NOW)
    assert ledger.spend_micros("vk-2", None) == 0


# --- rate windows -------------------------------------------------------------


def test_rpm_counts_logged_and_in_flight_requests(ledger: BudgetLedger) -> None:
    key = "vk-1"
    limits = Limits(rpm=2)
    assert ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW).allowed
    # The first request is still in flight, so it already fills half the window.
    assert ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW).allowed
    third = ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW)
    assert not third.allowed and third.reason == "rpm"
    assert third.used == 2 and third.limit == 2


def test_rpm_refills_once_the_window_has_passed(ledger: BudgetLedger) -> None:
    key = "vk-1"
    limits = Limits(rpm=1)
    assert ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW).allowed
    assert not ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW
    ).allowed
    later = NOW + timedelta(seconds=RATE_WINDOW_SECONDS + 1)
    assert ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=later
    ).allowed


def test_tpm_counts_the_estimate_held_by_a_request_in_flight(ledger: BudgetLedger) -> None:
    key = "vk-1"
    limits = Limits(tpm=20)
    assert ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, token_estimate=15, now=NOW
    ).allowed
    refused = ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, token_estimate=15, now=NOW
    )
    assert not refused.allowed and refused.reason == "tpm"
    assert refused.used == 15 and refused.limit == 20
    # Settled as 7 real tokens, the window then has room for another estimate.
    ledger.settle(key, usd=Decimal("0"), model="m", tokens=7, now=NOW)
    assert ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, token_estimate=13, now=NOW
    ).allowed


# --- housekeeping -------------------------------------------------------------


def test_a_stale_money_reservation_stops_counting_and_is_dropped(
    ledger: BudgetLedger,
) -> None:
    key = "vk-1"
    limits = Limits(budget_usd=Decimal("0.30"))
    assert ledger.try_reserve(key, limits=limits, model="m", ceiling_usd=CEILING, now=NOW).allowed
    assert ledger.reserved_micros(key, EPOCH) == to_micros(CEILING)
    # Money is held until settlement or the TTL, not for the 60-second rate window.
    five_minutes = NOW + timedelta(minutes=5)
    assert not ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=five_minutes
    ).allowed
    stale = NOW + timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS + 1)
    assert ledger.try_reserve(
        key, limits=limits, model="m", ceiling_usd=CEILING, now=stale
    ).allowed
    # The stale row is gone, not merely ignored: one hold remains, the new one.
    assert ledger.reserved_micros(key, EPOCH) == to_micros(CEILING)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rpm": 0},
        {"rpm": -1},
        {"tpm": 0},
        {"tpm": True},
        {"model_caps": [("m", "-1")]},
        {"model_caps": [("m", "abc")]},
        {"model_caps": [("", "1")]},
        {"model_caps": [("m", None)]},
    ],
)
def test_the_key_store_refuses_an_unusable_limit(store: KeyStore, kwargs: dict) -> None:
    with pytest.raises(KeysError):
        store.issue(label="alice", **kwargs)


def test_a_repeated_model_cap_keeps_the_last_amount_and_sorts(store: KeyStore) -> None:
    key, _ = store.issue(
        label="alice", model_caps=[("z", "1.00"), ("a", "2.00"), ("z", "3.00")]
    )
    assert key.model_caps == (("a", Decimal("2.00")), ("z", Decimal("3.00")))


def test_a_database_from_before_the_rename_is_migrated_in_place(tmp_path) -> None:
    """The tables' first name is not abandoned, and its rows are not lost."""
    path = tmp_path / "keys.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE budget_spend (
            spend_id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            usd_micros INTEGER NOT NULL
        );
        CREATE TABLE budget_reservations (
            reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT NOT NULL,
            ceiling_micros INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO budget_spend (key_id, ts, usd_micros)
            VALUES ('vk-old', '2026-10-01T00:00:00.000+00:00', 250000);
        INSERT INTO budget_reservations (key_id, ceiling_micros, created_at)
            VALUES ('vk-old', 250000, '2026-10-01T00:00:00.000+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = KeyStore(str(path))
    try:
        ledger = store.budget()
        # The old rows survived, under the new name and columns.
        assert ledger.spend_micros("vk-old", None) == 250_000
        assert ledger.reserved_micros("vk-old", EPOCH) == 250_000
        # The added columns exist, so a fresh write works on the migrated file.
        ledger.settle("vk-old", usd=Decimal("0.10"), model="m", tokens=3, now=NOW)
        assert ledger.spend_micros("vk-old", None) == 350_000
        assert ledger.tokens_in_window("vk-old", EPOCH) == 3
    finally:
        store.close()

    conn = sqlite3.connect(path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "budget_spend" not in names
    assert "budget_events" in names


def test_a_budget_reads_back_from_the_key_store(store: KeyStore) -> None:
    """The store and the ledger share one file, so a key's limits survive a read."""
    key, _ = store.issue(
        label="alice",
        budget_usd="2.50",
        period_days=30,
        rpm=10,
        tpm=1000,
        model_caps=[("m", "1.00")],
    )
    reread = store.get(key.key_id)
    assert reread is not None
    assert reread.budget_usd == Decimal("2.50")
    assert reread.period_days == 30
    assert reread.rpm == 10 and reread.tpm == 1000
    assert reread.model_caps == (("m", Decimal("1.00")),)
    assert reread.limited
    assert reread.limits.model_cap("m") == Decimal("1.00")
