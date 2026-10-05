"""SQLite log of one row per proxied request.

Only metadata is stored: token counts, cost, latency, status and the routing
decision.  No prompt or response text is ever accepted by this module, and the
schema has no column that could hold it.

UNKNOWN token counts, costs and latencies are stored as SQL NULL.  They are
never coerced to 0, so "the upstream did not say" stays distinguishable from
"the upstream said zero" after the fact.
"""

import sqlite3
import threading
from pathlib import Path

from tamias.types import CostBreakdown, Decision, Usage

__all__ = ["Store", "COLUMNS"]

TABLE = "requests"

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
    "price_sheet",
    "price_simulated",
    "provider_cost_usd",
    "latency_ms",
    "status",
    "decision_action",
    "decision_target_model",
    "decision_reason",
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
    price_sheet TEXT,
    price_simulated INTEGER,
    provider_cost_usd REAL,
    latency_ms INTEGER,
    status TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_target_model TEXT,
    decision_reason TEXT NOT NULL
)
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
            self._conn.execute(_CREATE_TABLE)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add nullable audit fields to logs created by older tamias versions."""
        present = {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({TABLE})")}
        for name, definition in (
            ("price_sheet", "TEXT"),
            ("price_simulated", "INTEGER"),
            ("provider_cost_usd", "REAL"),
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
    ) -> int:
        """Append one request and return its row id.

        Every None field is written as NULL.  `decision` is always persisted,
        including a shadow-mode decision that was not acted on; the
        target_model of a STAY decision is NULL rather than the current model.
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
            price_sheet,
            price_simulated,
            usage.provider_cost_usd,
            latency_ms,
            status,
            decision.action,
            decision.target_model,
            decision.reason,
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
