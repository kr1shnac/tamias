"""SQLite log of one row per proxied request.

Only metadata is stored: token counts, cost, latency, status and the routing
decision.  No prompt or response text is ever accepted by this module.  Two
columns are written from outside, and both are held to a shape rather than to
trust: ``price_sheet`` comes from configuration alone -- the path of the price
sheet the row was priced with, never from a request or a response -- and
``generation_id`` is the provider's own response id, which
:func:`sanitize_generation_id` reduces to a bare identifier or NULL.

UNKNOWN token counts, costs and latencies are stored as SQL NULL.  They are
never coerced to 0, so "the upstream did not say" stays distinguishable from
"the upstream said zero" after the fact.
"""

import re
import sqlite3
import threading
from pathlib import Path

from tamias.types import CostBreakdown, Decision, Usage

__all__ = [
    "Store",
    "COLUMNS",
    "sanitize_generation_id",
    "sanitize_project",
    "sanitize_effort",
    "GENERATION_ID_MAX_CHARS",
    "EFFORT_MAX_CHARS",
    "EFFORT_VALUES",
]

TABLE = "requests"

# The provider's response id is the only value in this log that arrives inside a
# response body, so it is admitted on shape rather than on trust.  A real id is a
# short opaque token: letters, digits and the separators OpenRouter's own examples
# use, which is why `-` and `_` are kept and why prose with spaces in it is not.
# This is a shape guarantee, not a secrecy guarantee: it bounds what a column can
# hold, it does not prove the value is meaningless.
GENERATION_ID_MAX_CHARS = 128
_GENERATION_ID = re.compile(r"[A-Za-z0-9_.:-]+")
PROJECT_MAX_CHARS = 64
_PROJECT = re.compile(r"[A-Za-z0-9_.-]+")
# Effort reaches the log from the *client's* request body (reasoning.effort or
# reasoning_effort), so unlike decision_target_effort it is caller-supplied.
# It is therefore admitted by vocabulary rather than by shape: effort is a
# closed set of short literals by specification, so only those literals can
# ever reach the row. A prompt, a key, or any other string is stored as NULL.
# Shape alone would not do this -- `sk-or-v1-abc123` has no spaces and would
# pass a character class -- whereas a fixed set of six values cannot hold
# arbitrary text by construction.
EFFORT_VALUES = frozenset({"low", "medium", "high", "minimal", "none", "auto"})
EFFORT_MAX_CHARS = max(len(value) for value in EFFORT_VALUES)


def sanitize_effort(value: object) -> str | None:
    """Return `value` when it names a known effort level, else None (SQL NULL)."""
    if not isinstance(value, str):
        return None
    return value if value in EFFORT_VALUES else None


def sanitize_project(value: object) -> str | None:
    """A project is a bounded identifier, never arbitrary request text."""
    if not isinstance(value, str) or not value or len(value) > PROJECT_MAX_CHARS:
        return None
    return value if _PROJECT.fullmatch(value) else None


def sanitize_generation_id(value: object) -> str | None:
    """Return `value` when it is a bare provider id, else None (SQL NULL).

    Anything else -- a number, a dict, an empty string, an id with a space in it,
    or one longer than :data:`GENERATION_ID_MAX_CHARS` -- is stored as NULL, so
    "the provider said no usable id" stays distinguishable from a real one.
    """
    if not isinstance(value, str):
        return None
    if not _GENERATION_ID.fullmatch(value) or len(value) > GENERATION_ID_MAX_CHARS:
        return None
    return value


# A new column is appended at the end, never inserted in the middle: an existing
# log gets it from ALTER TABLE, which can only add at the end, so a fresh
# CREATE TABLE has to declare the same order or `SELECT *` would return the same
# log's columns in two different sequences depending on how old the file is.
COLUMNS = (
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
    "price_sheet",
    "price_simulated",
    "provider_cost_usd",
    "generation_id",
    "project",
    "effort_requested",
    "effort_used",
    "decision_target_effort",
)

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
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
    latency_ms INTEGER,
    status TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_target_model TEXT,
    decision_reason TEXT NOT NULL,
    price_sheet TEXT,
    price_simulated INTEGER,
    provider_cost_usd REAL,
    generation_id TEXT,
    project TEXT,
    effort_requested TEXT,
    effort_used TEXT,
    decision_target_effort TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_session ON {TABLE}(session_id);
"""

_INSERT = f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"

_SELECT = f"SELECT * FROM {TABLE} ORDER BY id"


class Store:
    """Append-only request log backed by a local SQLite file.

    The schema is created on first use, so pointing a Store at a path in a
    directory that does not exist yet is fine.  The special path ":memory:"
    gives a private in-memory database, which is handy for tests.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        target = db_path if str(db_path) == ":memory:" else self._path
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_CREATE_TABLE)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add nullable audit fields to logs created by older tamias versions."""
        present = {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({TABLE})")}
        for name, definition in (
            ("price_sheet", "TEXT"),
            ("price_simulated", "INTEGER"),
            ("provider_cost_usd", "REAL"),
            ("generation_id", "TEXT"),
            ("project", "TEXT"),
            ("effort_requested", "TEXT"),
            ("effort_used", "TEXT"),
            ("decision_target_effort", "TEXT"),
        ):
            if name not in present:
                self._conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {definition}")

    def log_request(
        self,
        ts: str,
        session_id: str,
        model_requested: str,
        model_used: str,
        usage: Usage,
        cost: CostBreakdown,
        latency_ms: int | None,
        status: str,
        decision: Decision,
        *,
        price_sheet: str | None = None,
        price_simulated: bool | None = None,
        generation_id: object = None,
        project: object = None,
        effort_requested: str | None = None,
        effort_used: str | None = None,
        decision_target_effort: str | None = None,
    ) -> int:
        """Append one request and return its row id.

        Every None field is written as NULL.  `decision` is always persisted,
        including a shadow-mode decision that was not acted on; the
        target_model of a STAY decision is NULL rather than the current model.
        `generation_id` is whatever the response carried: it is passed through
        :func:`sanitize_generation_id`, so only a bare id reaches the row.
        """
        row = (
            ts,
            session_id,
            model_requested,
            model_used,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_input_tokens,
            usage.cache_write_tokens,
            cost.usd,
            cost.price_sheet_date,
            latency_ms,
            status,
            decision.action,
            decision.target_model,
            decision.reason,
            price_sheet,
            price_simulated,
            usage.provider_cost_usd,
            sanitize_generation_id(generation_id),
            sanitize_project(project),
            sanitize_effort(effort_requested),
            sanitize_effort(effort_used),
            sanitize_effort(decision_target_effort),
        )
        with self._lock:
            cursor = self._conn.execute(_INSERT, row)
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def rows(self) -> list[sqlite3.Row]:
        """Every logged request, oldest first."""
        with self._lock:
            return list(self._conn.execute(_SELECT))

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()
