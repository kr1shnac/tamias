#!/usr/bin/env python3
"""Compare logged OpenRouter costs with its documented generation lookup.

This script only performs ``GET /api/v1/generation?id=<generation-id>`` for
existing log rows.  It never creates a completion.  ``docs/BILLED-COST.md``
confirms that lookup's ``data.total_cost`` field; chat-completion placement of
the stored response ID remains UNVERIFIED, so missing or malformed IDs are
reported rather than inferred.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

LOOKUP_URL = "https://openrouter.ai/api/v1/generation"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"


@dataclass(frozen=True)
class ComparedRow:
    request: int
    billed_db: float | None
    billed_lookup: float | None
    computed: float | None
    difference: float | None
    matches: bool | None


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def compare_rows(rows: list[dict[str, Any]], tolerance: float = 1e-6) -> list[ComparedRow]:
    """Compare lookup totals to logged provider totals without inventing values."""
    compared: list[ComparedRow] = []
    for row in rows:
        db = _number(row.get("provider_cost_usd"))
        lookup = _number(row.get("lookup_cost_usd"))
        difference = lookup - db if lookup is not None and db is not None else None
        compared.append(
            ComparedRow(
                request=int(row["id"]),
                billed_db=db,
                billed_lookup=lookup,
                computed=_number(row.get("cost_usd")),
                difference=difference,
                matches=abs(difference) <= tolerance if difference is not None else None,
            )
        )
    return compared


def passed(rows: list[ComparedRow]) -> bool:
    """A reconciliation passes only when every attempted comparison matches."""
    return bool(rows) and all(row.matches is True for row in rows)


def _rows(db_path: Path) -> list[dict[str, Any]]:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
        required = {"id", "generation_id", "provider_cost_usd", "cost_usd"}
        missing = required - columns
        if missing:
            raise ValueError(f"request log lacks required columns: {', '.join(sorted(missing))}")
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, generation_id, provider_cost_usd, cost_usd FROM requests "
                "WHERE generation_id IS NOT NULL AND generation_id != '' ORDER BY id"
            )
        ]


def _lookup(client: httpx.Client, generation_id: str) -> float | None:
    response = client.get(LOOKUP_URL, params={"id": generation_id})
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return _number(data.get("total_cost")) if isinstance(data, dict) else None


def _money(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"${value:.6f}"


def run(db_path: Path, token: str, tolerance: float) -> int:
    """Reconcile what is logged and print PASS or FAIL; the exit code follows.

    Only ``GET {LOOKUP_URL}`` is ever issued, with the token from the caller's
    environment.  The token is never printed, never logged and never placed in an
    error message; every exit therefore prints PASS or FAIL.
    """
    rows = _rows(db_path)
    if not rows:
        print("No rows with generation IDs (chat response ID mapping is UNVERIFIED).")
        print("FAIL")
        return 1

    looked_up: list[dict[str, Any]] = []
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=20.0) as client:
        for row in rows:
            looked_up.append({**row, "lookup_cost_usd": _lookup(client, str(row["generation_id"]))})
    compared = compare_rows(looked_up, tolerance)
    print("request  billed in DB  billed per lookup  computed  lookup - DB")
    for row in compared:
        print(
            f"{row.request:7d}  {_money(row.billed_db):>12}  {_money(row.billed_lookup):>17}  "
            f"{_money(row.computed):>8}  {_money(row.difference):>11}"
        )
    outcome = "PASS" if passed(compared) else "FAIL"
    print(outcome)
    return 0 if outcome == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile logged OpenRouter billed cost by generation ID",
        # No abbreviations: `--api-key` must not be silently accepted as
        # `--api-key-env`, so the only way to name the variable is the full flag.
        allow_abbrev=False,
    )
    parser.add_argument("--db", required=True, type=Path, help="Tamias SQLite request log")
    parser.add_argument(
        "--api-key-env", default=DEFAULT_KEY_ENV, help="environment variable holding the lookup key"
    )
    parser.add_argument("--tolerance", default=1e-6, type=float, help="USD comparison tolerance")
    args = parser.parse_args(argv)
    # The key comes from the environment and from nowhere else: --api-key-env
    # names the variable, so there is no way to pass a key on the command line
    # where it would land in a shell history or a process listing.
    token = os.environ.get(args.api_key_env)
    if not token:
        print(f"reconcile_live: {args.api_key_env} is not set", file=sys.stderr)
        print("FAIL")
        return 2
    try:
        return run(args.db, token, args.tolerance)
    except (OSError, sqlite3.DatabaseError, ValueError, httpx.HTTPError) as exc:
        # The message is printed, not the token: a transport or status error
        # carries the request URL, never the Authorization header.
        print(f"reconcile_live: {exc}", file=sys.stderr)
        print("FAIL")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
