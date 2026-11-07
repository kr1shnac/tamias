"""Remembered provider limits: which model is cooling down, and until when.

A 429, a 402 ("insufficient credits"), a gated 403 and a 503 all mean the same
operational thing -- *do not send this model there right now*.  Tamias records
that fact the first time a provider says it, and routing skips the model until
the cooldown expires.  Without this memory every request rediscovers the limit,
which is how a routing gateway turns one provider's rate limit into a stream of
failed conversations.

The ledger is a small SQLite table beside the request log, so a restart does not
forget a one-hour lockout.  Strikes escalate: repeated failures on the same model
back off further (60s, 5m, 15m, 1h) instead of retrying a hard limit every
minute.  A ``Retry-After`` header, when the provider sends one, always wins when
it asks for longer than the ladder would.

Nothing here holds a credential, a request body or a provider response body: a
row is ``(provider, model, until, status, reason, strikes)``.

The second table is *quota*: the requests-remaining number a provider publishes
in its own response headers.  It is not a cooldown -- a cooldown says "this
model is refused right now", a quota says "this provider has N left before it
starts refusing".  Tamias records only what a provider actually sent and never
interpolates: no header means no quota line, which is why most providers show
none.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

#: Statuses that mean "this model is unavailable for now".  A 400 or a 422 is a
#: request problem and must not cool the model down.
RETRYABLE_STATUSES = frozenset({402, 403, 429, 500, 502, 503, 504, 529})

#: A 404 from an OpenAI-compatible upstream means the model is not routed there
#: at all: retired, never granted to this account, or served on a different wire.
#: It is not a *rate* limit, so the short ladder is wrong (it would retry a model
#: that will never answer), but ignoring it is worse -- that is how a gateway
#: picks the same dead model on every request.  So it is remembered separately
#: and for much longer, and reported with its own reason.
UNAVAILABLE_STATUSES = frozenset({404})

#: How long a 404 keeps a model out of routing.  Long enough that a retired
#: model is not rediscovered every minute; short enough that a model the
#: provider restores is picked up the same day.
UNAVAILABLE_SECONDS = 12 * 3600

#: A pseudo-status for "the connection never produced a response" -- a refused
#: connection, DNS failure or timeout.  Not an HTTP code, so it is kept outside
#: :data:`RETRYABLE_STATUSES`; without it a provider that accepts a connection and
#: then hangs would cost one request timeout after another.
TRANSPORT_FAILURE = 0

#: Statuses that are about the *provider's* credential, not one model, so the
#: whole provider is skipped until the operator fixes the key.
PROVIDER_STATUSES = frozenset({401})

#: Header names that report a *requests* quota.  Different providers use
#: different conventions, and a few send both styles; tamias reads them all
#: rather than guessing one house style.  Anything absent is simply absent --
#: no default, no "probably 1000 left".
QUOTA_REMAINING_HEADERS = (
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining",
    "ratelimit-remaining",
)
QUOTA_RESET_HEADERS = (
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset",
    "ratelimit-reset",
)

#: A quota older than this is not worth showing: a provider that has not
#: answered since the window started has told us nothing new.
QUOTA_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class Quota:
    """What one provider published about its own request quota."""

    provider: str
    remaining: int | None
    resets_in: float | None
    seen: float


def _first_int(headers: Mapping[str, str], names: Iterable[str]) -> int | None:
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            return int(float(text))
        except ValueError:
            continue
    return None


def _duration_seconds(text: str) -> float | None:
    """``"1m30s"`` -> 90.  A plain number is not a duration and returns ``None``.

    Several providers send a human duration rather than seconds; parsing it
    keeps their window readable without inventing one for the ones that send a
    number (those take the float path first).
    """
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    stripped = text.strip()
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", stripped)
    if not matches:
        return None
    # The whole value must be the duration; "1m30s extra" is not one.
    consumed = "".join(f"{value}{unit}" for value, unit in matches)
    if stripped.replace(" ", "") != consumed:
        return None
    return sum(float(value) * units[unit] for value, unit in matches)


def _first_seconds(headers: Mapping[str, str], names: Iterable[str]) -> float | None:
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        # Most send seconds; some send a duration string like "1m30s"; a few
        # send an HTTP-date, which parse_retry_after already understands.
        try:
            seconds: float | None = float(text)
        except ValueError:
            seconds = _duration_seconds(text)
        if seconds is None:
            seconds = parse_retry_after(text)
        if seconds is None:
            continue
        return max(0.0, seconds)
    return None


def quota_from_headers(provider: str, headers: Mapping[str, str]) -> Quota | None:
    """Read a provider's published quota, or ``None`` when it published none.

    A provider that names no window and no remaining count has said nothing,
    and inventing a remaining number would put a percentage on screen that no
    provider stands behind.
    """
    remaining = _first_int(headers, QUOTA_REMAINING_HEADERS)
    resets = _first_seconds(headers, QUOTA_RESET_HEADERS)
    if remaining is None and resets is None:
        return None
    return Quota(
        provider=provider,
        remaining=remaining,
        resets_in=resets,
        seen=time.time(),
    )


def retryable(status: int) -> bool:
    """Whether ``status`` says a different model could serve the same request.

    This is the question the proxy actually asks before failing over, so both
    a transient limit and a model that is not offered on this provider count.
    """
    return status in RETRYABLE_STATUSES or status in UNAVAILABLE_STATUSES

#: Escalating cooldown ladder, in seconds, indexed by strike count.
BACKOFF_SECONDS = (60, 300, 900, 3600)

_TABLE = "model_limits"
_QUOTA_TABLE = "provider_quota"


@dataclass(frozen=True)
class Limit:
    """One remembered lockout."""

    provider: str
    model: str
    until: float
    status: int
    reason: str
    strikes: int

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.until - (time.time() if now is None else now))

    def describe(self) -> str:
        return f"{self.provider}/{self.model} until {int(self.remaining())}s ({self.reason})"


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header, or ``None``.

    RFC 9110 allows both forms: delta-seconds (``120``) and an HTTP-date
    (``Wed, 21 Oct 2015 07:28:00 GMT``).  Providers in the wild send both, and
    more than one sends a date where a number was expected.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        current = time.time() if now is None else now
        return max(0.0, when.timestamp() - current)
    if seconds < 0 or seconds != seconds:  # NaN guard
        return None
    return seconds


class LimitLedger:
    """Per-model cooldowns, persisted so a restart keeps them."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = Path(path)
        if str(path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        target = path if str(path) == ":memory:" else self._path
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    until REAL NOT NULL,
    status INTEGER NOT NULL,
    reason TEXT NOT NULL,
    strikes INTEGER NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (provider, model)
);

CREATE TABLE IF NOT EXISTS {_QUOTA_TABLE} (
    provider TEXT PRIMARY KEY,
    remaining INTEGER,
    resets_at REAL,
    updated REAL NOT NULL
);
"""
            )
            self._conn.commit()
        if str(path) != ":memory:":
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass

    # -- reads ---------------------------------------------------------------

    def get(self, provider: str, model: str, now: float | None = None) -> Limit | None:
        """The active lockout for one model, or ``None`` when it may run."""
        current = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {_TABLE} WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        if row is None:
            return None
        if float(row["until"]) <= current:
            return None
        return Limit(
            provider=str(row["provider"]),
            model=str(row["model"]),
            until=float(row["until"]),
            status=int(row["status"]),
            reason=str(row["reason"]),
            strikes=int(row["strikes"]),
        )

    def is_limited(self, provider: str, model: str, now: float | None = None) -> bool:
        return self.get(provider, model, now) is not None

    def provider_limited(self, provider: str, now: float | None = None) -> Limit | None:
        """The provider-wide lockout (``model == "*"``), if one is active."""
        return self.get(provider, "*", now)

    def active(self, now: float | None = None) -> tuple[Limit, ...]:
        """Every lockout still in force, soonest-expiring first."""
        current = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {_TABLE} WHERE until > ? ORDER BY until",
                (current,),
            ).fetchall()
        return tuple(
            Limit(
                provider=str(row["provider"]),
                model=str(row["model"]),
                until=float(row["until"]),
                status=int(row["status"]),
                reason=str(row["reason"]),
                strikes=int(row["strikes"]),
            )
            for row in rows
        )

    # -- writes --------------------------------------------------------------

    def record(
        self,
        provider: str,
        model: str,
        status: int,
        *,
        retry_after: float | None = None,
        now: float | None = None,
    ) -> Limit | None:
        """Remember that ``provider`` refused ``model``; return the new lockout.

        Returns ``None`` for a status that says nothing about availability, so a
        caller can pass every non-2xx response without first classifying it.
        """
        if status in PROVIDER_STATUSES:
            target = "*"
            reason = f"credential rejected ({status})"
        elif status == TRANSPORT_FAILURE:
            target = model
            reason = "transport failure"
        elif status in UNAVAILABLE_STATUSES:
            target = model
            reason = f"not offered for this account ({status})"
        elif status in RETRYABLE_STATUSES:
            target = model
            reason = f"upstream {status}"
        else:
            return None

        current = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                f"SELECT strikes FROM {_TABLE} WHERE provider = ? AND model = ?",
                (provider, target),
            ).fetchone()
            strikes = int(row["strikes"]) if row is not None else 0
            strikes += 1
            if status in UNAVAILABLE_STATUSES:
                # A model that is not offered does not become available because
                # it was asked again: one long expiry, no escalating ladder.
                seconds = UNAVAILABLE_SECONDS
            else:
                ladder = BACKOFF_SECONDS[min(strikes - 1, len(BACKOFF_SECONDS) - 1)]
                if retry_after is not None and retry_after > 0:
                    seconds = max(ladder, retry_after)
                else:
                    seconds = ladder
            until = current + seconds
            self._conn.execute(
                f"INSERT OR REPLACE INTO {_TABLE} "
                "(provider, model, until, status, reason, strikes, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, target, until, status, reason, strikes, current),
            )
            self._conn.commit()
        return Limit(
            provider=provider,
            model=target,
            until=until,
            status=status,
            reason=reason,
            strikes=strikes,
        )

    def clear(self, provider: str, model: str | None = None) -> int:
        """Forget a lockout -- the model answered, or the operator fixed the key."""
        with self._lock:
            if model is None:
                cursor = self._conn.execute(
                    f"DELETE FROM {_TABLE} WHERE provider = ?", (provider,)
                )
            else:
                cursor = self._conn.execute(
                    f"DELETE FROM {_TABLE} WHERE provider = ? AND model = ?",
                    (provider, model),
                )
            self._conn.commit()
        return cursor.rowcount

    def quotas(self, now: float | None = None) -> tuple[Quota, ...]:
        """What each provider last published about its request quota.

        Nothing is invented: a provider that sends no quota headers has no row,
        and a row older than :data:`QUOTA_TTL_SECONDS` is dropped rather than
        shown, because a window that has since reset would make a stale count
        look like the truth.
        """
        current = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                f"SELECT provider, remaining, resets_at, updated FROM {_QUOTA_TABLE} "
                "WHERE updated > ? ORDER BY provider",
                (current - QUOTA_TTL_SECONDS,),
            ).fetchall()
        return tuple(
            Quota(
                provider=str(row["provider"]),
                remaining=None if row["remaining"] is None else int(row["remaining"]),
                resets_in=(
                    None if row["resets_at"] is None else max(0.0, row["resets_at"] - current)
                ),
                seen=float(row["updated"]),
            )
            for row in rows
        )

    def record_quota(self, quota: Quota | None, now: float | None = None) -> None:
        """Remember what a provider published, or forget it if it said nothing."""
        if quota is None:
            return
        current = time.time() if now is None else now
        resets_at = None if quota.resets_in is None else current + quota.resets_in
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {_QUOTA_TABLE} "
                "(provider, remaining, resets_at, updated) VALUES (?, ?, ?, ?)",
                (quota.provider, quota.remaining, resets_at, current),
            )
            self._conn.commit()

    def prune(self, now: float | None = None) -> int:
        """Drop expired rows so the table does not grow without bound."""
        current = time.time() if now is None else now
        with self._lock:
            cursor = self._conn.execute(f"DELETE FROM {_TABLE} WHERE until <= ?", (current,))
            stale = self._conn.execute(
                f"DELETE FROM {_QUOTA_TABLE} WHERE updated <= ?",
                (current - QUOTA_TTL_SECONDS,),
            )
            self._conn.commit()
        return cursor.rowcount + stale.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def empty_ledger() -> LimitLedger:
    return LimitLedger(":memory:")


def ledger_for(path: str | Path | None) -> LimitLedger:
    """The ledger for a serve run: beside the log, or in memory when there is none."""
    if path is None:
        return empty_ledger()
    return LimitLedger(path)
