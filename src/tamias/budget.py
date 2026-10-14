"""Per-key limits: the company credit ceiling and the rates around it.

:mod:`tamias.keys` answers *who* is calling.  This module answers *how much they
may spend, on what, and how fast*, and it is what makes a company credit real
rather than a convention: the limits live in the gateway, the arithmetic happens
before the caller reaches a provider, and there is no request a caller can send
to lift them.

Four limits are enforced, each optional and independent:

* a **USD budget** per key, optionally measured over a rolling period;
* a **per-model sub-cap**, so one expensive model cannot consume a budget meant
  for all of them;
* **requests per minute** (``rpm``);
* **tokens per minute** (``tpm``).

The money limits use reserve-then-settle:

* **Reserve.**  Before a request is forwarded, the gateway reserves a fixed
  ceiling (``request_ceiling_usd``) against the key and against the named model.
  If settled spend plus outstanding reservations plus this ceiling would exceed
  the key's budget -- or the model's sub-cap -- the request is refused with
  ``402`` and never reaches the provider.
* **Settle.**  When the request is logged -- the one point where its real cost is
  known -- the reservation is released and the *actual* computed cost is
  recorded.  A budget is therefore spent by what was used, not by what was
  guessed.

The rate limits use the same shape for tokens: admission reserves an estimate of
the prompt's size, and settlement replaces it with the token count the upstream
reported.  ``rpm`` counts requests already logged plus requests still in flight,
so a burst cannot slip past it; ``tpm`` counts recorded tokens plus the estimates
held by requests still in flight.

Every settled request becomes one row in ``budget_events`` -- key, model, time,
USD and tokens -- so spend, sub-caps and both windows are aggregates over one
append-only table.  Reservations are rows in ``budget_reservations``; since every
request reserves the same money ceiling, a key's outstanding rows are
interchangeable for the money arithmetic, and one is released per settlement.  A
row a crashed or unfinished request never settles is ignored, and dropped, after
:data:`DEFAULT_RESERVATION_TTL_SECONDS`: it stops counting against the key the
moment it is stale, and the key's next request deletes it, so a leak cannot
lock a key out and cannot grow the table without bound.

Limits are stated here rather than hidden:

* **Money is reserved as a flat ceiling, not a token estimate.**  A request that
  costs more than ``request_ceiling_usd`` can overshoot a money limit once, by
  the difference, at the moment it is exhausted.  The operator sets the ceiling
  to their largest plausible request.  The test is
  ``spent + reserved + ceiling <= limit``, so a limit smaller than one ceiling
  admits nothing -- the honest reading of "you cannot afford a request this
  large" -- rather than admitting everything.
* **Tokens are reserved as an estimate** of ``len(body) / 4``; only prompt bytes
  are counted, so a request asking for a very large completion can overshoot
  ``tpm`` by its output.  When the upstream reports no usage at all, the
  estimate is recorded in place of a zero, so an unmeasured request is never
  free of the counter.
* **The USD limits require a priced model.**  A model the price sheet cannot
  price (:func:`tamias.pricing.can_price`) produces a cost of UNKNOWN, which a
  budget cannot spend against, so a key with a USD limit may not reach it.
* **An unmeasured cost is charged the ceiling.**  When the price sheet cannot
  finish the arithmetic for a request's usage -- a partial usage object, or cache
  rates the sheet does not quote -- the request settles at the ceiling it
  reserved, not at zero, because a budget that reads an unmeasured request as
  free is not a ceiling at all.  This is deliberately conservative: an operator
  whose sheet lacks a rate will see the budget move faster than their true spend
  until they quote it.
* **Only the metered routes are counted.**  A key carrying any limit may not use
  the unlogged passthrough route when its body names a model: the gateway will
  not claim to bound what it does not measure.

Spend and reservations share the key store's SQLite file and connection.  They
are mutable per-key control state, exactly like the keys themselves, and one
connection keeps a single writer.
"""

from __future__ import annotations

import contextvars
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import Any

from tamias.types import CostBreakdown, Usage

__all__ = [
    "DEFAULT_REQUEST_CEILING_USD",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "RATE_WINDOW_SECONDS",
    "BudgetError",
    "BudgetLedger",
    "BudgetedLog",
    "CURRENT_KEY_ID",
    "Limits",
    "Reserve",
    "estimate_tokens",
    "from_micros",
    "parse_usd",
    "to_micros",
]

MICROS_PER_USD = Decimal(1_000_000)
DEFAULT_REQUEST_CEILING_USD = Decimal("0.25")
DEFAULT_RESERVATION_TTL_SECONDS = 1800
RATE_WINDOW_SECONDS = 60
BYTES_PER_TOKEN = 4

EVENTS_TABLE = "budget_events"
RESERVATION_TABLE = "budget_reservations"
# The table's first name, used for one commit before it carried tokens as well
# as money.  A database written then is renamed in place rather than abandoned.
LEGACY_SPEND_TABLE = "budget_spend"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    model TEXT,
    ts TEXT NOT NULL,
    usd_micros INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS {EVENTS_TABLE}_by_key ON {EVENTS_TABLE} (key_id, ts);
CREATE TABLE IF NOT EXISTS {RESERVATION_TABLE} (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    model TEXT,
    ceiling_micros INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS {RESERVATION_TABLE}_by_key ON {RESERVATION_TABLE} (key_id);
"""

# Columns added after the tables first existed.  ``CREATE TABLE IF NOT EXISTS``
# does not touch a table that already exists, so an older database is upgraded
# in place.
_EVENT_COLUMNS = {"model": "TEXT", "tokens": "INTEGER NOT NULL DEFAULT 0"}
_RESERVATION_COLUMNS = {
    "model": "TEXT",
    "token_estimate": "INTEGER NOT NULL DEFAULT 0",
}

# The key a request is being settled against.  ``proxy.authorize`` sets it from
# the authenticated request; ``BudgetedLog`` reads it when the request is logged.
# A context variable is what lets a streamed request settle: the streaming
# generator runs in the same request task, after the handler has returned, so it
# still sees the value -- ``tests/test_gateway_budget.py`` pins this.
CURRENT_KEY_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tamias_budget_key_id", default=None
)


class BudgetError(ValueError):
    """A limit could not be parsed."""


@dataclass(frozen=True, slots=True)
class Limits:
    """The limits a key carries.  Every field is optional and independent."""

    budget_usd: Decimal | None = None
    period_days: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    model_caps: tuple[tuple[str, Decimal], ...] = ()

    @property
    def enforced(self) -> bool:
        """True when any limit applies, so the gateway must meter the key."""
        return (
            self.budget_usd is not None
            or self.rpm is not None
            or self.tpm is not None
            or bool(self.model_caps)
        )

    @property
    def priced(self) -> bool:
        """True when enforcing the limits needs a price for the model."""
        return self.budget_usd is not None or bool(self.model_caps)

    def model_cap(self, model: str | None) -> Decimal | None:
        if model is None:
            return None
        for name, cap in self.model_caps:
            if name == model:
                return cap
        return None


@dataclass(frozen=True, slots=True)
class Reserve:
    """The outcome of asking to reserve one request against a key's limits.

    ``reason`` is ``"ok"`` or the first limit that refused the request, and the
    numbers beside it are what that limit compared, so a caller can state the
    refusal as arithmetic rather than as a bare "denied".
    """

    allowed: bool
    reason: str = "ok"
    model: str | None = None
    spent_usd: Decimal = Decimal("0")
    reserved_usd: Decimal = Decimal("0")
    cap_usd: Decimal | None = None
    used: int = 0
    limit: int | None = None

    @property
    def remaining_usd(self) -> Decimal | None:
        if self.cap_usd is None:
            return None
        return self.cap_usd - self.spent_usd - self.reserved_usd


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
    infinite amounts are refused: a limit is not a debt, and a NaN would poison
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


def estimate_tokens(body: bytes | None) -> int:
    """A prompt's size in tokens, estimated from its bytes.

    Text is roughly four bytes per token for the mixed prose and JSON a
    completion request carries.  This is an estimate the reservation is settled
    against, not a measurement; a request with no body reserves nothing.
    """
    if not body:
        return 0
    return max(1, math.ceil(len(body) / BYTES_PER_TOKEN))


def _tokens_of(usage: Usage | None) -> int | None:
    """Prompt plus completion tokens, or None when the upstream reported none."""
    if usage is None:
        return None
    parts = [
        count
        for count in (usage.input_tokens, usage.output_tokens)
        if isinstance(count, int) and not isinstance(count, bool)
    ]
    return sum(parts) if parts else None


def _usd_of(cost: CostBreakdown | None) -> Decimal | None:
    """The cost's USD, or None when it could not be computed.

    A ``CostBreakdown`` whose ``usd`` is None is not a free request: it is one the
    price sheet could not finish the arithmetic for.  Returning None keeps that
    distinction, so settlement can charge the ceiling instead of pretending the
    request cost nothing.
    """
    if cost is None or cost.usd is None:
        return None
    return Decimal(str(cost.usd))


class BudgetLedger:
    """Events and reservations in the key store's SQLite file.

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
            self._rename_legacy_table()
            self._conn.executescript(SCHEMA)
            self._ensure_columns(EVENTS_TABLE, _EVENT_COLUMNS)
            self._ensure_columns(RESERVATION_TABLE, _RESERVATION_COLUMNS)
            self._conn.commit()

    def _rename_legacy_table(self) -> None:
        tables = {
            str(row["name"])
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if LEGACY_SPEND_TABLE in tables and EVENTS_TABLE not in tables:
            self._conn.execute(
                f"ALTER TABLE {LEGACY_SPEND_TABLE} RENAME TO {EVENTS_TABLE}"
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        present = {
            str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in columns.items():
            if name not in present:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    # --- reads ----------------------------------------------------------------

    def spend_micros(self, key_id: str, since: str | None, model: str | None = None) -> int:
        sql = f"SELECT COALESCE(SUM(usd_micros), 0) FROM {EVENTS_TABLE} WHERE key_id = ?"
        args: list[object] = [key_id]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        if model is not None:
            sql += " AND model = ?"
            args.append(model)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def reserved_micros(self, key_id: str, cutoff: str, model: str | None = None) -> int:
        sql = (
            f"SELECT COALESCE(SUM(ceiling_micros), 0) FROM {RESERVATION_TABLE} "
            "WHERE key_id = ? AND created_at >= ?"
        )
        args: list[object] = [key_id, cutoff]
        if model is not None:
            sql += " AND model = ?"
            args.append(model)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def requests_in_window(self, key_id: str, cutoff: str) -> int:
        """Requests logged plus requests still in flight, inside the window."""
        logged = self._conn.execute(
            f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE key_id = ? AND ts >= ?",
            (key_id, cutoff),
        ).fetchone()[0]
        in_flight = self._conn.execute(
            f"SELECT COUNT(*) FROM {RESERVATION_TABLE} WHERE key_id = ? AND created_at >= ?",
            (key_id, cutoff),
        ).fetchone()[0]
        return int(logged) + int(in_flight)

    def tokens_in_window(self, key_id: str, cutoff: str) -> int:
        """Tokens logged plus the estimates held by requests still in flight."""
        logged = self._conn.execute(
            f"SELECT COALESCE(SUM(tokens), 0) FROM {EVENTS_TABLE} "
            "WHERE key_id = ? AND ts >= ?",
            (key_id, cutoff),
        ).fetchone()[0]
        in_flight = self._conn.execute(
            f"SELECT COALESCE(SUM(token_estimate), 0) FROM {RESERVATION_TABLE} "
            "WHERE key_id = ? AND created_at >= ?",
            (key_id, cutoff),
        ).fetchone()[0]
        return int(logged) + int(in_flight)

    # --- writes ---------------------------------------------------------------

    def try_reserve(
        self,
        key_id: str,
        *,
        limits: Limits,
        model: str | None,
        ceiling_usd: Decimal,
        token_estimate: int = 0,
        now: datetime | None = None,
    ) -> Reserve:
        """Reserve one request against ``limits``, or explain the refusal.

        Limits are applied in order of specificity: the rate limits first (they
        are retryable and distinct from running out of money), then a model's
        sub-cap, then the key's total budget.
        """
        moment = now or datetime.now(UTC)
        # Two cutoffs, because they answer two different questions: a money
        # reservation is held until it is settled or the TTL expires, while a
        # rate window only asks what happened in the last minute.
        window = now_stamp(moment - timedelta(seconds=RATE_WINDOW_SECONDS))
        held = now_stamp(moment - timedelta(seconds=self._ttl))
        since = None if limits.period_days is None else now_stamp(
            moment - timedelta(days=limits.period_days)
        )
        ceiling = to_micros(ceiling_usd)
        with self._lock:
            # A hold left behind by a request that never settled would otherwise
            # accumulate for the key's lifetime.  Dropping this key's own stale
            # rows here keeps the table bounded and self-healing, without a
            # background task or a sweep of rows belonging to other keys.
            self._conn.execute(
                f"DELETE FROM {RESERVATION_TABLE} WHERE key_id = ? AND created_at < ?",
                (key_id, held),
            )
            if limits.rpm is not None:
                used = self.requests_in_window(key_id, window)
                if used >= limits.rpm:
                    return Reserve(
                        allowed=False, reason="rpm", model=model, used=used, limit=limits.rpm
                    )
            if limits.tpm is not None:
                used_tokens = self.tokens_in_window(key_id, window)
                if used_tokens + token_estimate > limits.tpm:
                    return Reserve(
                        allowed=False,
                        reason="tpm",
                        model=model,
                        used=used_tokens,
                        limit=limits.tpm,
                    )
            cap = limits.model_cap(model)
            if cap is not None:
                spent = self.spend_micros(key_id, since, model)
                reserved = self.reserved_micros(key_id, held, model)
                if spent + reserved + ceiling > to_micros(cap):
                    return Reserve(
                        allowed=False,
                        reason="model_cap",
                        model=model,
                        spent_usd=from_micros(spent),
                        reserved_usd=from_micros(reserved),
                        cap_usd=cap,
                    )
            if limits.budget_usd is not None:
                spent = self.spend_micros(key_id, since)
                reserved = self.reserved_micros(key_id, held)
                if spent + reserved + ceiling > to_micros(limits.budget_usd):
                    return Reserve(
                        allowed=False,
                        reason="budget",
                        model=model,
                        spent_usd=from_micros(spent),
                        reserved_usd=from_micros(reserved),
                        cap_usd=limits.budget_usd,
                    )
            self._conn.execute(
                f"INSERT INTO {RESERVATION_TABLE} "
                "(key_id, model, ceiling_micros, token_estimate, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, model, ceiling, token_estimate, now_stamp(moment)),
            )
            self._conn.commit()
            return Reserve(allowed=True, model=model)

    def settle(
        self,
        key_id: str,
        *,
        usd: Decimal | None,
        model: str | None = None,
        tokens: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Release one reservation for ``key_id`` and record what was spent.

        The reservation is matched on the model when one is available, so
        settling one model's request cannot release another's sub-cap.  When the
        upstream reported no token count, the reservation's estimate is recorded
        instead of a bare zero.  Returns whether a reservation was released.

        The reservation's own age is irrelevant here: it is released because the
        request it belonged to ended, not because a window passed.

        ``usd`` of None means the cost could not be computed.  The request is then
        charged the ceiling it reserved rather than zero, because a budget that
        reads an unmeasured request as free is not a ceiling at all -- the same
        rule the token estimate follows.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            row = self._take_reservation(key_id, model)
            if usd is None:
                micros = int(row["ceiling_micros"]) if row is not None else 0
            else:
                micros = to_micros(usd)
            if tokens is None:
                tokens = int(row["token_estimate"]) if row is not None else 0
            self._conn.execute(
                f"INSERT INTO {EVENTS_TABLE} (key_id, model, ts, usd_micros, tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, model, now_stamp(moment), micros, tokens),
            )
            self._conn.commit()
            return row is not None

    def _take_reservation(self, key_id: str, model: str | None) -> sqlite3.Row | None:
        """Remove one of the key's reservations, preferring the settled model.

        A model-scoped sub-cap needs the settle to release the *same* model's
        hold, or one model's spending would silently free another's.  The oldest
        hold is taken next, because a settlement must release something: an
        upstream that reports a different model than the request named (or none
        at all) would otherwise leak a reservation for the full TTL.
        """
        columns = "reservation_id, token_estimate, ceiling_micros"
        if model is None:
            row = self._conn.execute(
                f"SELECT {columns} FROM {RESERVATION_TABLE} "
                "WHERE key_id = ? AND model IS NULL ORDER BY reservation_id LIMIT 1",
                (key_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                f"SELECT {columns} FROM {RESERVATION_TABLE} "
                "WHERE key_id = ? AND model = ? ORDER BY reservation_id LIMIT 1",
                (key_id, model),
            ).fetchone()
        if row is None:
            row = self._conn.execute(
                f"SELECT {columns} FROM {RESERVATION_TABLE} "
                "WHERE key_id = ? ORDER BY reservation_id LIMIT 1",
                (key_id,),
            ).fetchone()
        if row is not None:
            self._conn.execute(
                f"DELETE FROM {RESERVATION_TABLE} WHERE reservation_id = ?",
                (row["reservation_id"],),
            )
        return row

def _measurement(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Decimal | None, str, int | None]:
    """The USD, model and token count a ``log_request`` row carries.

    ``log_request`` takes usage, cost and model positionally in every caller this
    gateway has, but a keyword call is accepted too, and the two objects are
    found by type rather than by position so a signature change cannot silently
    settle zero.  The model is the only string of the three that cannot be told
    from its neighbours by type, so it is read positionally, guarded by an
    ``isinstance`` check.  A USD of None means the cost was UNKNOWN.
    """
    cost = kwargs.get("cost")
    if not isinstance(cost, CostBreakdown):
        cost = next((arg for arg in args if isinstance(arg, CostBreakdown)), None)
    usage = kwargs.get("usage")
    if not isinstance(usage, Usage):
        usage = next((arg for arg in args if isinstance(arg, Usage)), None)
    model = kwargs.get("model_used")
    if not isinstance(model, str):
        model = args[3] if len(args) > 3 and isinstance(args[3], str) else ""
    return _usd_of(cost), model, _tokens_of(usage)


class BudgetedLog:
    """A request log that also settles the caller's budget reservation.

    Every metered route already funnels through one ``log_request`` call at the
    point where the response's real cost and usage are known, so settlement is
    placed there rather than duplicated beside three handlers.  The key is read
    from :data:`CURRENT_KEY_ID`.
    """

    def __init__(self, log: Any, ledger: BudgetLedger) -> None:
        self._log = log
        self._ledger = ledger

    def log_request(self, *args: Any, **kwargs: Any) -> int:
        rowid = self._log.log_request(*args, **kwargs)
        key_id = CURRENT_KEY_ID.get()
        if key_id is not None:
            usd, model, tokens = _measurement(args, kwargs)
            self._ledger.settle(key_id, usd=usd, model=model, tokens=tokens)
        return rowid

    def __getattr__(self, name: str) -> Any:
        # Anything the log does besides metering -- the session salt, the price
        # sheet, a close -- still belongs to the wrapped log.
        return getattr(self._log, name)
