"""Per-key budgets: a company credit ceiling the gateway enforces itself.

:mod:`tamias.keys` answers *who* is calling.  This module answers *how much they
may spend*, and it is what makes a company credit real rather than a convention:
the ceiling lives in the gateway, the arithmetic happens before the caller
reaches a provider, and there is no request a caller can send to lift it.

The model is reserve-then-settle:

* **Reserve.**  Before a request is forwarded, the gateway reserves a fixed
  ceiling (``request_ceiling_usd``) against the key.  If settled spend plus
  outstanding reservations plus this ceiling would exceed the key's budget, the
  request is refused with ``402`` and never reaches the provider.
* **Settle.**  When the request is logged -- the one point where its real cost
  is known -- the reservation is released and the *actual* computed cost is
  recorded.  A budget is therefore spent by what was used, not by what was
  guessed.

Reservations are rows in ``budget_reservations``; settling deletes one of the
key's rows and inserts the real cost into ``budget_spend``.  Since every request
reserves the same ceiling, a key's outstanding rows are interchangeable, so a
settlement may release any one of them.  A row a crashed or unfinished request
never settles is ignored -- and swept -- after
:data:`DEFAULT_RESERVATION_TTL_SECONDS`, so a leak cannot lock a key out forever.

Three limits are deliberate and stated here rather than hidden:

* **The reservation is a flat ceiling, not a token estimate.**  A request that
  costs more than ``request_ceiling_usd`` can overshoot the budget once, by the
  difference, at the moment the budget is exhausted.  The operator sets the
  ceiling to their largest plausible request.  The test is
  ``spent + reserved + ceiling <= budget``, so a budget smaller than one ceiling
  admits nothing -- the honest reading of "you cannot afford a request this
  large" -- rather than admitting everything.
* **Only priced models are counted.**  A model the price sheet cannot price
  (:func:`tamias.pricing.can_price`) produces a cost of UNKNOWN, which a budget
  cannot spend against, so a budgeted key may not reach it.
* **Only the metered routes are billed.**  A key with a budget may not use the
  unlogged passthrough route when its body names a model: the gateway will not
  claim to bound spend it does not measure.

Spend and reservations share the key store's SQLite file and connection.  They
are mutable per-key control state, exactly like the keys themselves, and one
connection keeps a single writer.
"""

from __future__ import annotations

import contextvars
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import Any

from tamias.types import CostBreakdown

__all__ = [
    "DEFAULT_REQUEST_CEILING_USD",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "BudgetError",
    "BudgetLedger",
    "BudgetState",
    "BudgetedLog",
    "CURRENT_KEY_ID",
    "from_micros",
    "parse_usd",
    "to_micros",
]

MICROS_PER_USD = Decimal(1_000_000)
DEFAULT_REQUEST_CEILING_USD = Decimal("0.25")
DEFAULT_RESERVATION_TTL_SECONDS = 1800

SPEND_TABLE = "budget_spend"
RESERVATION_TABLE = "budget_reservations"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {SPEND_TABLE} (
    key_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    usd_micros INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS {SPEND_TABLE}_by_key ON {SPEND_TABLE} (key_id, ts);
CREATE TABLE IF NOT EXISTS {RESERVATION_TABLE} (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    ceiling_micros INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {RESERVATION_TABLE}_by_key ON {RESERVATION_TABLE} (key_id);
"""

# The key a request is being settled against.  ``proxy.authorize`` sets it from
# the authenticated request; ``BudgetedLog`` reads it when the request is logged.
# A context variable is what lets a streamed request settle: the streaming
# generator runs in the same request task, after the handler has returned, so it
# still sees the value -- ``tests/test_gateway_budget.py`` pins this.
CURRENT_KEY_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tamias_budget_key_id", default=None
)


class BudgetError(ValueError):
    """A budget or a request ceiling could not be parsed."""


def now_stamp(when: datetime | None = None) -> str:
    """An ISO-8601 UTC stamp that sorts as text, matching the rest of the store."""
    moment = datetime.now(UTC) if when is None else when
    return moment.isoformat(timespec="milliseconds")


def to_micros(usd: Decimal) -> int:
    """USD as integer micro-dollars, so sums in SQL stay exact."""
    return int((usd * MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def from_micros(micros: int) -> Decimal:
    return (Decimal(micros) / MICROS_PER_USD).quantize(Decimal("0.000001"))


def parse_usd(value: object) -> Decimal | None:
    """A non-negative USD amount, or None for an absent one.

    Accepts what a CLI argument or a stored column carries.  Negative, NaN and
    infinite amounts are refused: a budget is not a debt, and a NaN would poison
    every comparison it reached.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise BudgetError("a USD amount must be a number, not a boolean")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetError(f"{value!r} is not a USD amount") from exc
    if not amount.is_finite():
        raise BudgetError(f"{value!r} is not a finite USD amount")
    if amount < 0:
        raise BudgetError(f"{value!r} is not a non-negative USD amount")
    return amount


@dataclass(frozen=True, slots=True)
class BudgetState:
    """A key's budget and what the gateway's own ledger says it has spent.

    ``remaining_usd`` is what is left after both settled spend and outstanding
    reservations are subtracted, so it never describes money that in-flight
    requests may already be spending.
    """

    budget_usd: Decimal | None
    period_days: int | None
    spent_usd: Decimal
    reserved_usd: Decimal

    @property
    def enforced(self) -> bool:
        return self.budget_usd is not None

    @property
    def remaining_usd(self) -> Decimal | None:
        if self.budget_usd is None:
            return None
        return self.budget_usd - self.spent_usd - self.reserved_usd


class BudgetLedger:
    """Spend and reservations in the key store's SQLite file.

    Every check-then-write happens under the store's lock, so two concurrent
    requests cannot both settle against the same remaining budget on the same
    connection.  A second process writing the same file is serialised by SQLite
    itself.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Lock,
        *,
        ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise BudgetError("the reservation TTL must be a positive number of seconds")
        self._conn = conn
        self._lock = lock
        self._ttl = ttl_seconds
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _since(self, period_days: int | None, moment: datetime) -> str | None:
        if period_days is None:
            return None
        return now_stamp(moment - timedelta(days=period_days))

    def _cutoff(self, moment: datetime) -> str:
        return now_stamp(moment - timedelta(seconds=self._ttl))

    def spent_micros(self, key_id: str, since: str | None) -> int:
        sql = f"SELECT COALESCE(SUM(usd_micros), 0) FROM {SPEND_TABLE} WHERE key_id = ?"
        args: list[object] = [key_id]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def reserved_micros(self, key_id: str, cutoff: str) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(ceiling_micros), 0) FROM {RESERVATION_TABLE} "
            "WHERE key_id = ? AND created_at >= ?",
            (key_id, cutoff),
        ).fetchone()
        return int(row[0])

    def state(
        self,
        key_id: str,
        *,
        budget_usd: Decimal | None,
        period_days: int | None,
        now: datetime | None = None,
    ) -> BudgetState:
        moment = now or datetime.now(UTC)
        with self._lock:
            spent = self.spent_micros(key_id, self._since(period_days, moment))
            reserved = self.reserved_micros(key_id, self._cutoff(moment))
        return BudgetState(
            budget_usd=budget_usd,
            period_days=period_days,
            spent_usd=from_micros(spent),
            reserved_usd=from_micros(reserved),
        )

    def try_reserve(
        self,
        key_id: str,
        *,
        ceiling_usd: Decimal,
        budget_usd: Decimal,
        period_days: int | None,
        now: datetime | None = None,
    ) -> bool:
        """Reserve ``ceiling_usd`` for one request, or refuse it.

        False means the request would take the key past its budget and must not
        be forwarded.  A True result has written the reservation, so the spend is
        committed even if the request is never logged.
        """
        moment = now or datetime.now(UTC)
        ceiling = to_micros(ceiling_usd)
        budget = to_micros(budget_usd)
        with self._lock:
            spent = self.spent_micros(key_id, self._since(period_days, moment))
            reserved = self.reserved_micros(key_id, self._cutoff(moment))
            if spent + reserved + ceiling > budget:
                return False
            self._conn.execute(
                f"INSERT INTO {RESERVATION_TABLE} (key_id, ceiling_micros, created_at) "
                "VALUES (?, ?, ?)",
                (key_id, ceiling, now_stamp(moment)),
            )
            self._conn.commit()
            return True

    def settle(self, key_id: str, *, usd: Decimal, now: datetime | None = None) -> bool:
        """Release one reservation for ``key_id`` and record what was spent.

        Returns whether a reservation was released.  Settlement is recorded even
        when there was none -- an uncapped request's real cost still belongs in
        the ledger -- but a zero cost inserts no spend row, so a free request
        leaves no money behind.
        """
        moment = now or datetime.now(UTC)
        micros = to_micros(usd)
        with self._lock:
            row = self._conn.execute(
                f"SELECT reservation_id FROM {RESERVATION_TABLE} "
                "WHERE key_id = ? ORDER BY reservation_id LIMIT 1",
                (key_id,),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    f"DELETE FROM {RESERVATION_TABLE} WHERE reservation_id = ?",
                    (row[0],),
                )
            if micros:
                self._conn.execute(
                    f"INSERT INTO {SPEND_TABLE} (key_id, ts, usd_micros) VALUES (?, ?, ?)",
                    (key_id, now_stamp(moment), micros),
                )
            self._conn.commit()
            return row is not None

    def sweep(self, now: datetime | None = None) -> int:
        """Drop reservations older than the TTL; returns how many were dropped."""
        moment = now or datetime.now(UTC)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM {RESERVATION_TABLE} WHERE created_at < ?",
                (self._cutoff(moment),),
            )
            self._conn.commit()
            return cursor.rowcount


def _cost_usd_in(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Decimal:
    """The USD a ``log_request`` row carries, or zero when it could not be computed.

    ``log_request`` takes the cost positionally in every caller this gateway has,
    but a keyword call is accepted too, and the cost object is found by type
    rather than by position so a signature change cannot silently settle zero.
    """
    cost = kwargs.get("cost")
    if not isinstance(cost, CostBreakdown):
        cost = next((arg for arg in args if isinstance(arg, CostBreakdown)), None)
    if cost is None or cost.usd is None:
        return Decimal(0)
    return Decimal(str(cost.usd))


class BudgetedLog:
    """A request log that also settles the caller's budget reservation.

    Every metered route already funnels through one ``log_request`` call at the
    point where the response's real cost is known, so settlement is placed there
    rather than duplicated beside three handlers.  The key is read from
    :data:`CURRENT_KEY_ID`.
    """

    def __init__(self, log: Any, ledger: BudgetLedger) -> None:
        self._log = log
        self._ledger = ledger

    def log_request(self, *args: Any, **kwargs: Any) -> int:
        rowid = self._log.log_request(*args, **kwargs)
        key_id = CURRENT_KEY_ID.get()
        if key_id is not None:
            self._ledger.settle(key_id, usd=_cost_usd_in(args, kwargs))
        return rowid

    def __getattr__(self, name: str) -> Any:
        # Anything the log does besides metering -- the session salt, the price
        # sheet, a close -- still belongs to the wrapped log.
        return getattr(self._log, name)
