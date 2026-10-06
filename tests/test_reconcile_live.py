"""Unit tests for generation-cost reconciliation: the arithmetic and the safety rules.

The four promises under test are that the script never prints its key, never sends
anything but the documented ``GET /api/v1/generation`` lookup, reads the key from
the environment alone, and prints PASS or FAIL with a matching exit code on every
path.  The HTTP client is stubbed, so nothing here touches the network and no key
is ever needed.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# A made-up value, not a credential: the point is that the script cannot leak
# whatever string it is given.
FAKE_KEY = "sk-live-0000000000000000-not-a-real-key"


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_live.py"
    spec = importlib.util.spec_from_file_location("reconcile_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compare_rows_handles_match_mismatch_and_unknowns() -> None:
    reconcile = _module()
    rows = reconcile.compare_rows(
        [
            {"id": 1, "provider_cost_usd": 0.1, "cost_usd": 0.12, "lookup_cost_usd": 0.1},
            {"id": 2, "provider_cost_usd": 0.2, "cost_usd": 0.18, "lookup_cost_usd": 0.200002},
            {"id": 3, "provider_cost_usd": None, "cost_usd": 0.0, "lookup_cost_usd": 0.0},
        ],
        tolerance=1e-6,
    )

    assert [row.matches for row in rows] == [True, False, None]
    assert rows[0].difference == 0.0
    assert rows[1].difference == pytest.approx(0.000002)
    assert reconcile.passed(rows) is False


# --- the safety rules ---------------------------------------------------------

CREATE_LOG = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    generation_id TEXT,
    provider_cost_usd REAL,
    cost_usd REAL
)
"""


def _log(tmp_path: Path, rows: tuple[tuple[int, str, float | None, float | None], ...]) -> Path:
    """A request log with the four columns the script reads."""
    path = tmp_path / "requests.db"
    with sqlite3.connect(path) as conn:
        conn.execute(CREATE_LOG)
        conn.executemany(
            "INSERT INTO requests (id, generation_id, provider_cost_usd, cost_usd)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
    return path


class _LookupResponse:
    def __init__(self, total_cost: float) -> None:
        self._payload = {"data": {"id": "gen-123", "total_cost": total_cost}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class RecordingClient:
    """Stands in for ``httpx.Client`` and records calls instead of making them.

    Nothing leaves the machine and no key is needed: the stub accepts the headers
    it is given and answers every lookup with the same documented total.
    """

    calls: list[dict[str, Any]] = []
    sent_headers: dict[str, str] = {}

    def __init__(self, headers: dict[str, str] | None = None, **_ignored: Any) -> None:
        type(self).sent_headers = dict(headers or {})

    def __enter__(self) -> RecordingClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, str] | None = None) -> Any:
        type(self).calls.append({"method": "GET", "url": url, "params": dict(params or {})})
        return _LookupResponse(0.1)


def test_only_the_documented_lookup_endpoint_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One GET, to the generation lookup docs/BILLED-COST.md documents.

    The assertion is on the recorded calls rather than on the source, so adding a
    completion request or a second endpoint is what makes this fail.
    """
    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.1, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)
    RecordingClient.calls = []

    reconcile.run(db, FAKE_KEY, 1e-6)

    assert [call["method"] for call in RecordingClient.calls] == ["GET"]
    assert [call["url"] for call in RecordingClient.calls] == [reconcile.LOOKUP_URL]
    assert RecordingClient.calls[0]["params"] == {"id": "gen-123"}
    assert reconcile.LOOKUP_URL == "https://openrouter.ai/api/v1/generation"
    assert "chat/completions" not in reconcile.LOOKUP_URL


def test_the_key_travels_as_a_bearer_header_and_never_reaches_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The key is sent in the Authorization header and printed nowhere."""
    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.1, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)
    monkeypatch.setenv("TAMIAS_TEST_KEY", FAKE_KEY)

    code = reconcile.main(["--db", str(db), "--api-key-env", "TAMIAS_TEST_KEY"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines()[-1] == "PASS"
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err
    assert RecordingClient.sent_headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_no_flag_passes_a_key_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key cannot be given on the command line, so it cannot leak into a shell.

    ``--api-key-env`` names the variable; there is deliberately no flag whose
    value is the key itself.  Abbreviations are off, so ``--api-key`` is refused
    outright rather than quietly meaning "the variable named by the next word".
    """
    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.1, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)
    monkeypatch.setenv("TAMIAS_TEST_KEY", FAKE_KEY)

    with pytest.raises(SystemExit) as rejected:
        reconcile.main(["--db", str(db), "--api-key", "not-a-flag-value"])

    assert rejected.value.code == 2, "a key-valued flag exists when argparse should refuse it"
    assert reconcile.DEFAULT_KEY_ENV == "OPENROUTER_API_KEY"

    # The full flag still works, and it names a variable rather than a key.
    assert reconcile.main(["--db", str(db), "--api-key-env", "TAMIAS_TEST_KEY"]) == 0
    assert RecordingClient.sent_headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_a_missing_key_prints_fail_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every path prints PASS or FAIL: an unset key is a FAIL, not a silent exit 2."""
    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.1, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)
    monkeypatch.delenv("TAMIAS_TEST_KEY", raising=False)

    code = reconcile.main(["--db", str(db), "--api-key-env", "TAMIAS_TEST_KEY"])

    captured = capsys.readouterr()
    assert code != 0
    assert "FAIL" in captured.out.splitlines()
    assert "TAMIAS_TEST_KEY" in captured.err, "the variable name is safe to report"


def test_a_log_with_no_generation_ids_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing to reconcile is not a pass: the check was not made."""
    reconcile = _module()
    db = _log(tmp_path, ())
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)

    code = reconcile.run(db, FAKE_KEY, 1e-6)

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out.splitlines()[-1] == "FAIL"


def test_a_billed_total_that_disagrees_with_the_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A disagreement is a FAIL with exit 1, and the difference is shown."""
    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.2, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", RecordingClient)

    code = reconcile.run(db, FAKE_KEY, 1e-6)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.splitlines()[-1] == "FAIL"


def test_a_lookup_error_prints_fail_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transport error is a FAIL as well, and its message carries no key."""

    class FailingClient(RecordingClient):
        def get(self, url: str, params: dict[str, str] | None = None) -> Any:
            raise httpx.ConnectError("connection refused", request=None)

    reconcile = _module()
    db = _log(tmp_path, ((1, "gen-123", 0.1, 0.12),))
    monkeypatch.setattr(reconcile.httpx, "Client", FailingClient)
    monkeypatch.setenv("TAMIAS_TEST_KEY", FAKE_KEY)

    code = reconcile.main(["--db", str(db), "--api-key-env", "TAMIAS_TEST_KEY"])

    captured = capsys.readouterr()
    assert code == 2
    assert "FAIL" in captured.out.splitlines()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err
