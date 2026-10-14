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
table as an audit trail while failing authentication.  Nothing here enforces a
budget yet: this module is identity only.
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
from pathlib import Path
from threading import Lock

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
    revoked_at TEXT
);
"""

_INSERT = (
    f"INSERT INTO {KEY_TABLE} "
    "(key_id, token_hash, label, project, models, created_at, revoked_at) "
    "VALUES (?, ?, ?, ?, ?, ?, NULL)"
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

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

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
        with self._lock:
            self._conn.executescript(_CREATE)
            self._conn.commit()
        if self._path is not None:
            # The table holds key material, if only as hashes; keep it private.
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        self._conn.close()

    def issue(
        self,
        *,
        label: str = "",
        project: str | None = None,
        models: Iterable[str] = (),
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

        stamp = _now()
        with self._lock:
            for _ in range(8):
                key_id, token = mint_key()
                try:
                    self._conn.execute(
                        _INSERT,
                        (key_id, hash_token(token), label, project, json.dumps(allow), stamp),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    continue
                return Key(key_id, label, project, tuple(allow), stamp, None), token
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
        )
