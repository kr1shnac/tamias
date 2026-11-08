"""`tamias reconcile` must compare real billed cost, row by row.

The command used to list rows with a generation id and write ``difference:
null``: it ignored ``--since``/``--until``/``--tolerance``, never used the
stored billed cost, and could not fail.  These tests defend the promises in
``docs/RECONCILE.md``: a match is a match, a mismatch is visible and exits
non-zero, a missing billed cost is UNKNOWN rather than zero, and the optional
OpenRouter lookup never prints the key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias import reconcile  # noqa: E402
from tamias.cli import main  # noqa: E402
from tamias.reconcile import (  # noqa: E402
    EMPTY,
    FAIL,
    INCOMPLETE,
    PASS,
    aggregate_invoice_days,
    apply_invoice,
    compare_rows,
    load_invoice,
    lookup_generation_costs,
    summarize,
)
from tamias.store import Store  # noqa: E402
from tamias.types import CostBreakdown, Decision, Usage  # noqa: E402

FAKE_KEY = "sk-or-v1-not-a-real-key-000000000000"


def log(
    db: Path,
    *,
    ts: str = "2026-01-05T00:00:00Z",
    computed: float | None = 0.10,
    billed: float | None = 0.10,
    generation: str | None = None,
    model_requested: str = "strong/model",
    model_used: str = "cheap/model",
    route: str | None = None,
) -> None:
    store = Store(db)
    store.log_request(
        ts=ts,
        session_id="session-1234",
        model_requested=model_requested,
        model_used=model_used,
        usage=Usage(10, 5, 0, 0, provider_cost_usd=billed),
        cost=CostBreakdown(
            usd=computed,
            formula="test" if computed is None else f"= {computed}",
            price_sheet_date="2026-01-05",
        ),
        latency_ms=1,
        status="200",
        decision=Decision(action="SWITCH", target_model=model_used, reason="test"),
        generation_id=generation,
        route=route,
    )
    store.close()


def run(
    capsys: pytest.CaptureFixture[str], db: Path, *args: str
) -> tuple[int, str, str]:
    code = main(["reconcile", "--db", str(db), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the arithmetic, without a database -------------------------------------


def test_a_match_within_tolerance_is_a_match():
    rows = compare_rows(
        [
            {
                "id": 1,
                "computed_list_cost_usd": 0.10,
                "provider_cost_usd": 0.1000004,
            }
        ],
        tolerance=1e-6,
    )

    assert rows[0].difference == pytest.approx(0.0000004)
    assert rows[0].matches is True
    assert summarize(rows).status == PASS


def test_a_mismatch_is_visible_and_a_missing_bill_is_not_zero():
    rows = compare_rows(
        [
            {
                "id": 1,
                "computed_list_cost_usd": 0.10,
                "provider_cost_usd": 0.12,
            },
            {
                "id": 2,
                "computed_list_cost_usd": 0.10,
                "provider_cost_usd": None,
            },
        ]
    )

    assert rows[0].difference == pytest.approx(0.02)
    assert rows[0].matches is False
    assert rows[1].difference is None
    assert rows[1].matches is None
    summary = summarize(rows)
    assert summary.status == FAIL
    assert (summary.compared, summary.matched, summary.mismatched) == (1, 0, 1)
    assert summary.without_billed == 1


def test_no_comparable_rows_is_incomplete_not_a_pass():
    rows = compare_rows([{"id": 1, "computed_list_cost_usd": 0.10}])

    summary = summarize(rows)
    assert summary.status == INCOMPLETE
    assert summary.compared == 0


def test_legacy_cost_usd_is_not_used_as_a_computed_cost():
    rows = compare_rows(
        [
            {
                "id": 1,
                "computed_list_cost_usd": None,
                "cost_usd": 0.10,
                "provider_cost_usd": 0.10,
            }
        ]
    )

    assert rows[0].computed is None
    assert rows[0].matches is None
    assert summarize(rows).status == INCOMPLETE


def test_no_rows_is_empty():
    assert summarize([]).status == EMPTY


# --- the command -------------------------------------------------------------


def test_offline_reconcile_uses_the_stored_billed_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.10)

    code, out, _err = run(capsys, db)

    assert code == 0
    assert "PASS" in out
    assert "compared: 1 of 1" in out
    assert "difference (billed - computed): $0.00" in out


def test_a_mismatch_exits_three_and_says_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.12)

    code, out, _err = run(capsys, db)

    assert code == 3
    assert "FAIL" in out
    assert "$0.020000" in out


def test_a_row_without_a_billed_cost_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None)

    code, out, err = run(capsys, db)

    assert code == 2
    assert "INCOMPLETE" in out
    assert "1 without a billed cost" in out


def test_since_and_until_select_a_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, ts="2026-01-05T00:00:00Z", computed=0.10, billed=0.10)
    log(db, ts="2026-01-06T00:00:00Z", computed=0.20, billed=0.20)

    code, out, _err = run(capsys, db, "--since", "2026-01-06", "--until", "2026-01-06")

    assert code == 0
    assert "rows: 1 selected" in out
    assert "$0.200000" in out
    assert "$0.100000" not in out


def test_an_empty_window_is_zero_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "requests.db"
    log(db, ts="2026-01-05T00:00:00Z")

    code, out, err = run(capsys, db, "--since", "2027-01-01")

    assert code == 2
    assert "ZERO_ROWS" in err
    assert out == ""


def test_passthrough_rows_are_excluded_not_counted_as_unpriced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=None, billed=None, route="passthrough")
    log(db, computed=0.10, billed=0.10)

    code, out, _err = run(capsys, db)

    assert code == 0
    assert "rows: 1 selected of 1 metered in log" in out
    assert "passthrough rows excluded: 1" in out
    assert "0 without a billed cost" in out


def test_json_summary_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.11)

    code, out, _err = run(capsys, db, "--json")

    assert code == 3
    document = json.loads(out)
    assert document["status"] == FAIL
    assert document["compared"] == 1
    assert document["mismatched"] == 1
    assert document["difference_usd"] == pytest.approx(0.01)


def test_out_artifact_carries_real_differences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    artifact = tmp_path / "receipt.json"
    log(db, computed=0.10, billed=0.12, generation="gen-abc")

    code, _out, _err = run(capsys, db, "--out", str(artifact))

    assert code == 3
    document = json.loads(artifact.read_text())
    assert document[0]["request_id"] == 1
    assert document[0]["computed_cost"] == pytest.approx(0.10)
    assert document[0]["billed_cost"] == pytest.approx(0.12)
    assert document[0]["difference"] == pytest.approx(0.02)
    assert document[0]["matches"] is False
    assert document[0]["billed_source"] == "logged"
    assert document[0]["tokens"] == {"prompt_tokens": 10, "completion_tokens": 5}


# --- the optional OpenRouter lookup -----------------------------------------


class LookupClient:
    """A stand-in for ``httpx.Client`` that records the one call it is given."""

    calls: list[dict[str, Any]] = []
    headers: dict[str, str] = {}

    def __init__(self, *, headers: dict[str, str] | None = None, **_ignored: Any) -> None:
        type(self).headers = dict(headers or {})
        type(self).calls = []

    def __enter__(self) -> LookupClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        type(self).calls.append({"url": url, "params": dict(params or {})})
        return httpx.Response(
            200,
            json={"data": {"id": "gen-abc", "total_cost": 0.10}},
            request=httpx.Request("GET", url),
        )


def test_the_lookup_reads_only_the_documented_generation_endpoint():
    client = LookupClient()
    rows = [{"id": 1, "generation_id": "gen-abc", "provider_cost_usd": None}]

    costs, errors = lookup_generation_costs(rows, client=client)

    assert costs == {"gen-abc": 0.10}
    assert errors == 0
    assert [call["url"] for call in LookupClient.calls] == [reconcile.LOOKUP_URL]
    assert LookupClient.calls[0]["params"] == {"id": "gen-abc"}


def test_non_openrouter_generation_ids_are_not_sent_to_openrouter():
    client = LookupClient()
    rows = [{"id": 1, "generation_id": "chatcmpl-123", "provider_cost_usd": None}]

    costs, errors = lookup_generation_costs(rows, client=client)

    assert costs == {}
    assert errors == 0
    assert LookupClient.calls == []


def test_online_fills_a_missing_billed_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None, generation="gen-abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setattr(reconcile.httpx, "Client", LookupClient)

    code, out, err = run(capsys, db, "--online")

    assert code == 0
    assert "PASS" in out
    assert "looked up" in err
    assert FAKE_KEY not in out
    assert FAKE_KEY not in err
    assert LookupClient.headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_online_without_a_key_exits_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None, generation="gen-abc")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code, _out, err = run(capsys, db, "--online")

    assert code == 1
    assert "OPENROUTER_API_KEY is not set" in err


def test_online_skips_a_non_openrouter_id_and_uses_the_stored_bill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.10, generation="chatcmpl-123")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setattr(reconcile.httpx, "Client", LookupClient)

    code, out, err = run(capsys, db, "--online")

    assert code == 0
    assert "PASS" in out
    assert "skipped" in err
    assert LookupClient.calls == []


class FailingClient(LookupClient):
    def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        raise httpx.ConnectError("lookup unavailable")


def test_a_failed_lookup_stays_unknown_and_never_prints_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None, generation="gen-abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setattr(reconcile.httpx, "Client", FailingClient)

    code, out, err = run(capsys, db, "--online")

    assert code == 2
    assert "INCOMPLETE" in out
    assert "1 generation lookup(s) failed" in err
    assert FAKE_KEY not in out
    assert FAKE_KEY not in err


# --- provider invoices, the provider-agnostic proof path --------------------


def invoice(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_an_invoice_line_overrides_the_stored_bill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.10)
    csv = invoice(
        tmp_path / "invoice.csv", "request_id,cost_usd\n1,0.12\n"
    )

    code, out, _err = run(capsys, db, "--invoice", str(csv))

    assert code == 3
    assert "FAIL" in out
    assert "invoice: 1 line(s) by request_id (row scope); 1 matched" in out
    assert "$0.020000" in out


def test_a_row_missing_from_the_invoice_is_incomplete_not_backfilled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=0.10)
    log(db, computed=0.20, billed=0.20)
    csv = invoice(tmp_path / "invoice.csv", "request_id,cost_usd\n1,0.10\n")

    code, out, _err = run(capsys, db, "--invoice", str(csv))

    assert code == 2
    assert "INCOMPLETE" in out
    # The stored bill on row 2 is visible in the log but is not used to fill
    # the invoice gap: a proof may not mix two billed sources.
    assert "1 without a billed cost" in out


def test_an_invoice_can_join_on_the_provider_generation_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None, generation="chatcmpl-abc")
    csv = invoice(
        tmp_path / "invoice.csv", "generation_id,cost_usd\nchatcmpl-abc,0.10\n"
    )

    code, out, _err = run(
        capsys, db, "--invoice", str(csv), "--invoice-join", "generation_id"
    )

    assert code == 0
    assert "PASS" in out


def test_invoice_duplicate_keys_are_summed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None)
    csv = invoice(
        tmp_path / "invoice.csv",
        "request_id,cost_usd\n1,0.04\n1,$0.06\n",
    )

    code, out, _err = run(capsys, db, "--invoice", str(csv))

    assert code == 0
    assert "PASS" in out
    assert "duplicate keys summed: 1" in out


def test_an_unmatched_invoice_line_blocks_pass_unless_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None)
    csv = invoice(
        tmp_path / "invoice.csv", "request_id,cost_usd\n1,0.10\n2,0.99\n"
    )

    code, out, _err = run(capsys, db, "--invoice", str(csv))
    assert code == 2
    assert "INCOMPLETE" in out
    assert "invoice lines matching no selected row: 1" in out

    code, out, _err = run(
        capsys, db, "--invoice", str(csv), "--invoice-allow-unmatched"
    )
    assert code == 0
    assert "PASS" in out


def test_a_malformed_invoice_is_a_runtime_error_not_a_smaller_bill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None)

    for body in (
        "request_id,amount\n1,0.10\n",
        "request_id,cost_usd\n1,-0.10\n",
        "request_id,cost_usd\n1,\n",
        "request_id,cost_usd\n",
    ):
        csv = invoice(tmp_path / "invoice.csv", body)
        code, out, err = run(capsys, db, "--invoice", str(csv))
        assert code == 1, body
        assert out == ""
        assert "tamias reconcile:" in err


def test_invoice_and_online_are_alternative_billed_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, computed=0.10, billed=None, generation="gen-abc")
    csv = invoice(tmp_path / "invoice.csv", "request_id,cost_usd\n1,0.10\n")

    code, _out, err = run(capsys, db, "--invoice", str(csv), "--online")

    assert code == 1
    assert "alternative billed sources" in err


def test_day_scope_compares_daily_totals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, ts="2026-01-05T01:00:00Z", computed=0.10, billed=None)
    log(db, ts="2026-01-05T02:00:00Z", computed=0.10, billed=None)
    csv = invoice(tmp_path / "invoice.csv", "date,cost_usd\n2026-01-05,0.20\n")

    code, out, _err = run(capsys, db, "--invoice", str(csv), "--invoice-scope", "day")

    assert code == 0
    assert "PASS" in out
    assert "invoice: 1 line(s) by date (day scope); 1 matched" in out


def test_day_scope_keeps_an_unpriced_row_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "requests.db"
    log(db, ts="2026-01-05T01:00:00Z", computed=0.10, billed=None)
    log(db, ts="2026-01-05T02:00:00Z", computed=None, billed=None)
    csv = invoice(tmp_path / "invoice.csv", "date,cost_usd\n2026-01-05,0.20\n")

    code, out, _err = run(capsys, db, "--invoice", str(csv), "--invoice-scope", "day")

    assert code == 2
    assert "INCOMPLETE" in out


def test_day_scope_rejects_a_non_date_key(tmp_path: Path):
    cargo = invoice(tmp_path / "invoice.csv", "date,cost_usd\nnot-a-date,0.20\n")
    loaded = load_invoice(cargo, key_column="date")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        aggregate_invoice_days([], loaded.costs)


def test_apply_invoice_reports_keys_that_matched_no_row():
    rows = [{"id": 1, "generation_id": "gen-a"}]
    enriched, matched, unmatched = apply_invoice(rows, {"1": 0.1, "2": 0.2})

    assert matched == 1
    assert unmatched == ["2"]
    assert enriched[0]["invoice_cost_usd"] == pytest.approx(0.1)


def test_day_aggregation_counts_rows_it_cannot_date():
    rows = [
        {"id": 1, "ts": "not-a-date", "computed_list_cost_usd": 0.1},
        {"id": 2, "ts": "2026-01-05T00:00:00Z", "computed_list_cost_usd": 0.1},
    ]
    aggregated, unmatched, matched, undated = aggregate_invoice_days(
        rows, {"2026-01-05": 0.1}
    )

    assert matched == 1
    assert unmatched == []
    assert undated == 1
    assert len(aggregated) == 1
