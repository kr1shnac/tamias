"""Company API keys: per-caller identity for the gateway.

``--auth-token`` is a single shared secret: it says "someone the operator
trusts" and nothing more - no identity, no project, no scope.  A company needs
more than that.  A key answers three questions the shared token cannot: who is
calling, which project the spend belongs to, and which models the caller may
reach.

A key is a random ``vk-`` token.  Only its SHA-256 hash is stored, so a copy of
the keys database does not yield a usable credential, and ``list``/``show``
never print the secret.  The public ``key_id`` (the token's prefix) is what an
operator sees and revokes.

Revocation is a timestamp rather than a delete, so a revoked key stays in the
table as an audit trail while failing authentication.

A key may also carry a budget: a USD ceiling and the period it is measured over.
The arithmetic that enforces it lives in :mod:`tamias.budget`; a :class:`KeyStore`
hands that module its own connection so keys and spend share one writer rather
than racing two connections over the same file.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock

from tamias.budget import (
    DEFAULT_RESERVATION_TTL_SECONDS,
    BudgetError,
    BudgetLedger,
    Limits,
    parse_usd,
)
from tamias.store import sanitize_project

KEY_PREFIX = "vk-"
_TOKEN_BYTES = 32
KEY_TABLE = "api_keys"

_CREATE = f"""
CREATE TABLE IF NOT EXISTS {KEY_TABLE} (
    key_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    project TEXT,
    models TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    budget_usd TEXT,
    period_days INTEGER,
    rpm INTEGER,
    tpm INTEGER,
    model_caps TEXT
);
"""

# Columns added after the first release.  ``CREATE TABLE IF NOT EXISTS`` does not
# touch a table that already exists, so a database written before budgets needs
# them added in place; both are nullable, so an old row simply has no budget.
_ADDED_COLUMNS = {
    "budget_usd": "TEXT",
    "period_days": "INTEGER",
    "rpm": "INTEGER",
    "tpm": "INTEGER",
    "model_caps": "TEXT",
}

_INSERT = (
    f"INSERT INTO {KEY_TABLE} "
    "(key_id, token_hash, label, project, models, created_at, revoked_at, "
    "budget_usd, period_days, rpm, tpm, model_caps) "
    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)"
)


class KeysError(ValueError):
    """A key could not be minted, read or validated."""


def hash_token(token: str) -> str:
    """The lookup value stored for a token.

    A plain SHA-256 is right here, unlike for a password: the input is 32 bytes
    of ``secrets`` output, so there is no low-entropy guess to slow down.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_key() -> tuple[str, str]:
    """Return ``(key_id, token)``.  The token is shown once and never stored."""
    key_id = f"{KEY_PREFIX}{secrets.token_hex(4)}"
    token = f"{key_id}-{secrets.token_urlsafe(_TOKEN_BYTES)}"
    return key_id, token


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stored_usd(value: object) -> Decimal | None:
    """A budget read back from the column; malformed text reads as no budget."""
    try:
        return parse_usd(value)
    except BudgetError:
        return None


def _stored_caps(value: object) -> tuple[tuple[str, Decimal], ...]:
    """Model sub-caps read back from the column; a malformed row caps nothing."""
    if not isinstance(value, str) or not value:
        return ()
    try:
        raw = json.loads(value)
    except ValueError:
        return ()
    if not isinstance(raw, list):
        return ()
    caps: list[tuple[str, Decimal]] = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        name, amount = entry
        if not isinstance(name, str) or not name:
            continue
        try:
            cap = parse_usd(amount)
        except BudgetError:
            continue
        if cap is not None:
            caps.append((name, cap))
    return tuple(sorted(caps))


@dataclass(frozen=True, slots=True)
class Key:
    """One company key's identity, without its secret.

    ``models`` is the allowlist: empty means "no restriction", never
    "no access".  A non-empty tuple is enforced by the gateway before a request
    is forwarded, so a restricted key cannot reach a model it does not list.
    """

    key_id: str
    label: str
    project: str | None
    models: tuple[str, ...]
    created_at: str
    revoked_at: str | None = None
    budget_usd: Decimal | None = None
    period_days: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    model_caps: tuple[tuple[str, Decimal], ...] = ()

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def limits(self) -> Limits:
        """This key's limits, in the shape the ledger enforces."""
        return Limits(
            budget_usd=self.budget_usd,
            period_days=self.period_days,
            rpm=self.rpm,
            tpm=self.tpm,
            model_caps=self.model_caps,
        )

    @property
    def limited(self) -> bool:
        """True when the gateway must enforce any limit for this key."""
        return self.limits.enforced

    @property
    def unrestricted(self) -> bool:
        """True when this key may reach any model in the registry."""
        return not self.models


class KeyStore:
    """Company keys in a local SQLite file.

    Kept in its own file rather than the request log: the log is append-only
    metering, while this table is mutable control state, and a deployment may
    want to keep the log and vendor the keys separately.
    """

    def __init__(self, db_path: str | Path) -> None:
        in_memory = str(db_path) == ":memory:"
        target = db_path if in_memory else Path(db_path)
        if not in_memory:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._path = None if in_memory else Path(target)
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        self._ledger: BudgetLedger | None = None
        with self._lock:
            self._conn.executescript(_CREATE)
            self._ensure_columns()
            self._conn.commit()
        if self._path is not None:
            # The table holds key material, if only as hashes; keep it private.
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def _ensure_columns(self) -> None:
        existing = {
            str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({KEY_TABLE})")
        }
        for name, declaration in _ADDED_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {KEY_TABLE} ADD COLUMN {name} {declaration}")

    def close(self) -> None:
        self._conn.close()

    def budget(self, *, ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS) -> BudgetLedger:
        """The spend ledger, sharing this store's connection and lock.

        Built lazily and cached, so a store that never enforces a budget -- every
        ``tamias admin key`` command, most tests -- never creates the tables.
        """
        if self._ledger is None:
            self._ledger = BudgetLedger(self._conn, self._lock, ttl_seconds=ttl_seconds)
        return self._ledger

    def issue(
        self,
        *,
        label: str = "",
        project: str | None = None,
        models: Iterable[str] = (),
        budget_usd: object = None,
        period_days: int | None = None,
        rpm: int | None = None,
        tpm: int | None = None,
        model_caps: Iterable[tuple[str, object]] = (),
    ) -> tuple[Key, str]:
        """Mint a key.  Returns the stored :class:`Key` and the token to hand out.

        The token is returned exactly once; only its hash is kept, so it cannot
        be recovered later.
        """
        label = label.strip()
        if project is not None:
            cleaned_project = sanitize_project(project)
            if cleaned_project is None:
                raise KeysError(
                    "project must match [A-Za-z0-9_.-]+ and be at most 64 characters"
                )
            project = cleaned_project

        allow: list[str] = []
        for model in models:
            if not isinstance(model, str) or not model.strip():
                raise KeysError("model ids must be non-empty strings")
            model = model.strip()
            if model not in allow:
                allow.append(model)

        try:
            budget = parse_usd(budget_usd)
        except BudgetError as exc:
            raise KeysError(str(exc)) from exc
        if period_days is not None:
            if (
                isinstance(period_days, bool)
                or not isinstance(period_days, int)
                or period_days <= 0
            ):
                raise KeysError("period_days must be a positive whole number of days")
            if budget is None:
                raise KeysError("period_days means nothing without a budget")
        for name, rate in (("rpm", rpm), ("tpm", tpm)):
            if rate is not None and (
                isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0
            ):
                raise KeysError(f"{name} must be a positive whole number")
        caps: list[tuple[str, Decimal]] = []
        for model, amount in model_caps:
            if not isinstance(model, str) or not model.strip():
                raise KeysError("a model cap needs a non-empty model id")
            try:
                parsed = parse_usd(amount)
            except BudgetError as exc:
                raise KeysError(f"model cap for {model!r}: {exc}") from exc
            if parsed is None:
                raise KeysError(f"model cap for {model!r} needs a USD amount")
            cleaned = model.strip()
            caps = [(name, cap) for name, cap in caps if name != cleaned]
            caps.append((cleaned, parsed))
        caps.sort()

        stamp = _now()
        with self._lock:
            for _ in range(8):
                key_id, token = mint_key()
                try:
                    self._conn.execute(
                        _INSERT,
                        (
                            key_id,
                            hash_token(token),
                            label,
                            project,
                            json.dumps(allow),
                            stamp,
                            None if budget is None else str(budget),
                            period_days,
                            rpm,
                            tpm,
                            json.dumps([[name, str(cap)] for name, cap in caps]) if caps else None,
                        ),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    continue
                return Key(
                    key_id,
                    label,
                    project,
                    tuple(allow),
                    stamp,
                    None,
                    budget,
                    period_days,
                    rpm,
                    tpm,
                    tuple(caps),
                ), token
        raise KeysError("could not mint a unique key id")

    def get(self, key_id: str) -> Key | None:
        row = self._conn.execute(
            f"SELECT * FROM {KEY_TABLE} WHERE key_id = ?", (key_id,)
        ).fetchone()
        return None if row is None else self._to_key(row)

    def list(self) -> list[Key]:
        rows = self._conn.execute(
            f"SELECT * FROM {KEY_TABLE} ORDER BY created_at, key_id"
        ).fetchall()
        return [self._to_key(row) for row in rows]

    def revoke(self, key_id: str) -> bool:
        """Mark a key revoked.  False when it is unknown or already revoked."""
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE {KEY_TABLE} SET revoked_at = ? "
                "WHERE key_id = ? AND revoked_at IS NULL",
                (_now(), key_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def authenticate(self, token: str) -> Key | None:
        """The key behind a token, or None when unknown, malformed or revoked."""
        if not isinstance(token, str) or not token.startswith(KEY_PREFIX):
            return None
        row = self._conn.execute(
            f"SELECT * FROM {KEY_TABLE} WHERE token_hash = ?", (hash_token(token),)
        ).fetchone()
        if row is None:
            return None
        key = self._to_key(row)
        return None if key.revoked else key

    @staticmethod
    def _to_key(row: sqlite3.Row) -> Key:
        try:
            models = tuple(json.loads(row["models"]))
        except (TypeError, ValueError):
            models = ()
        return Key(
            key_id=str(row["key_id"]),
            label=str(row["label"]),
            project=row["project"],
            models=models,
            created_at=str(row["created_at"]),
            revoked_at=row["revoked_at"],
            budget_usd=_stored_usd(row["budget_usd"]),
            period_days=row["period_days"],
            rpm=row["rpm"],
            tpm=row["tpm"],
            model_caps=_stored_caps(row["model_caps"]),
        )
