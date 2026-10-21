"""Unit tests for the credit A/B measurement: the ledger arithmetic and its refusals.

The promises under test are the ones that keep a credit measurement honest:

* the ledger parser reads the documented field names and leaves a missing field
  as ``None`` rather than defaulting it to zero (zero means "spent nothing";
  absence means "the provider did not say");
* a saving is only claimed when the ledger actually moved and the movement is
  above the instrument floor -- a non-moving ledger is never reported as
  "$0.00 saved";
* a negative saving survives as a negative number instead of being clamped;
* two arms that did not do identical work are reported as invalid, because
  "less credit used" carries no signal unless the work was the same.

The HTTP client is stubbed, so nothing here touches the network and no key is
ever needed.  The live path is refusal-gated behind ``--allow-spend``, which is
also asserted here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

# A made-up value, not a credential: the point is that the script cannot leak
# whatever string it is given.
FAKE_KEY = "sk-live-0000000000000000-not-a-real-key"


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "credit_ab_check.py"
    spec = importlib.util.spec_from_file_location("credit_ab_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _arms(module, *, usage: tuple[float, float, float], requests: tuple[int, int] = (2, 2),
          rewritten: tuple[int, int] = (0, 1)):
    return module.Arms(
        off=module.ArmResult(name="off", mode="off", rewritten=rewritten[0],
                             requests_sent=requests[0]),
        active=module.ArmResult(name="active", mode="active", rewritten=rewritten[1],
                                requests_sent=requests[1]),
        before=module.Ledger(usage=usage[0]),
        between=module.Ledger(usage=usage[1]),
        after=module.Ledger(usage=usage[2]),
    )


def test_parse_key_payload_reads_documented_fields() -> None:
    module = _module()
    ledger = module.parse_key_payload(
        {
            "data": {
                "usage": 25.5,
                "usage_daily": 1.25,
                "usage_monthly": 25.5,
                "limit_remaining": 74.5,
                "is_free_tier": False,
                "free_model_daily_requests": {"limit": 50, "remaining": 38, "used": 12},
            }
        }
    )

    assert ledger.readable is True
    assert ledger.usage == pytest.approx(25.5)
    assert ledger.usage_daily == pytest.approx(1.25)
    assert ledger.limit_remaining == pytest.approx(74.5)
    assert ledger.free_used == 12
    assert ledger.free_remaining == 38
    assert ledger.free_limit == 50


def test_missing_usage_fields_stay_none_and_are_not_readable() -> None:
    module = _module()
    ledger = module.parse_key_payload({"data": {}})

    assert ledger.usage is None
    assert ledger.usage_daily is None
    # absence must not masquerade as "spent nothing"
    assert ledger.readable is False


def test_delta_usage_is_none_when_either_read_is_missing() -> None:
    module = _module()
    assert module.Ledger(usage=2.0).delta_usage(module.Ledger()) is None
    assert module.Ledger().delta_usage(module.Ledger(usage=1.0)) is None
    assert module.Ledger(usage=2.0).delta_usage(module.Ledger(usage=1.0)) == pytest.approx(1.0)


def test_read_key_reports_http_error_without_fabricating_numbers() -> None:
    module = _module()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"code": 401}})
    )
    with httpx.Client(transport=transport) as client:
        ledger = module.read_key(FAKE_KEY, client=client)

    assert ledger.readable is False
    assert ledger.usage is None
    assert "401" in (ledger.error or "")


def test_read_key_never_sends_anything_but_get() -> None:
    module = _module()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"data": {"usage": 0.0}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        module.read_key(FAKE_KEY, client=client)

    assert seen == ["GET"]


def test_positive_saving_is_reported_as_a_measurement() -> None:
    module = _module()
    ok, lines = module.compare_arms(_arms(module, usage=(10.00, 10.06, 10.08)))
    text = "\n".join(lines)

    assert ok is True
    assert "MEASURED SAVING: $0.040000" in text


def test_negative_saving_survives_as_negative() -> None:
    module = _module()
    _, lines = module.compare_arms(_arms(module, usage=(10.00, 10.05, 10.11)))
    text = "\n".join(lines)

    # routing that costs more must not hide behind zero
    assert "MEASURED SAVING: $-0.010000" in text


def test_no_ledger_movement_claims_no_saving() -> None:
    module = _module()
    _, lines = module.compare_arms(_arms(module, usage=(10.00, 10.00, 10.00)))
    text = "\n".join(lines)

    assert "MEASURED SAVING" not in text
    assert "below the instrument floor" in text


def test_unreadable_ledger_is_named_not_zeroed() -> None:
    module = _module()
    arms = _arms(module, usage=(10.00, 10.06, 10.08))
    arms.before = module.Ledger()
    arms.between = module.Ledger()
    arms.after = module.Ledger()

    _, lines = module.compare_arms(arms)
    text = "\n".join(lines)

    assert "MEASURED SAVING" not in text
    assert "did not yield a money delta" in text


def test_unequal_request_counts_invalidate_the_comparison() -> None:
    module = _module()
    ok, lines = module.compare_arms(
        _arms(module, usage=(10.00, 10.06, 10.08), requests=(2, 5))
    )

    assert ok is False
    assert "the work was not identical" in "\n".join(lines)


def test_free_quota_is_reported_as_a_count_not_money() -> None:
    module = _module()
    arms = module.Arms(
        off=module.ArmResult(name="off", mode="off", rewritten=0, requests_sent=2),
        active=module.ArmResult(name="active", mode="active", rewritten=1, requests_sent=2),
        before=module.Ledger(free_used=10),
        between=module.Ledger(free_used=12),
        after=module.Ledger(free_used=14),
    )

    _, lines = module.compare_arms(arms)
    text = "\n".join(lines)

    assert "MEASURED SAVING" not in text
    assert "a count, not money" in text


def test_live_path_refuses_to_spend_without_allow_spend(monkeypatch) -> None:
    module = _module()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called: list[int] = []
    monkeypatch.setattr(module, "read_key", lambda *a, **k: called.append(1))

    # no --allow-spend: prints the procedure and returns without sending anything
    assert module.main(["--strong-model", "x", "--cheap-model", "y"]) == 0
    assert called == []


def test_read_key_without_a_key_exits_two(monkeypatch) -> None:
    module = _module()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert module.main(["--read-key"]) == 2


# ------------------------- candidate sweep -------------------------


def _runs(arm: str, costs: list[float], passes: list[bool]):
    module = _module()
    return [
        module.RunResult(arm=arm, cost=cost, passed=passed)
        for cost, passed in zip(costs, passes, strict=True)
    ]


def test_sweep_picks_the_cheapest_passing_arm() -> None:
    module = _module()
    summaries = module.summarise_runs(
        _runs("off", [0.10, 0.11, 0.12], [True] * 3)
        + _runs("cheapA", [0.02, 0.021, 0.019], [True] * 3)
        + _runs("cheapB", [0.05, 0.06, 0.055], [True] * 3)
    )

    winner, lines = module.choose_cheapest(summaries, min_runs=3)

    assert winner is not None
    assert winner.arm == "cheapA"
    assert "cheapest arm that passed" in "\n".join(lines)


def test_sweep_excludes_a_cheap_model_that_fails_the_task() -> None:
    module = _module()
    summaries = module.summarise_runs(
        _runs("cheapA", [0.005, 0.006, 0.004], [True, False, True])
        + _runs("cheapB", [0.05, 0.06, 0.055], [True] * 3)
    )

    winner, lines = module.choose_cheapest(summaries, min_runs=3)

    # the cheapest arm broke the task, so it is not a saving
    assert winner is not None
    assert winner.arm == "cheapB"
    assert "did not pass" in "\n".join(lines)


def test_sweep_is_inconclusive_when_ranges_overlap() -> None:
    module = _module()
    summaries = module.summarise_runs(
        _runs("cheapA", [0.05, 0.07, 0.06], [True] * 3)
        + _runs("cheapB", [0.055, 0.065, 0.058], [True] * 3)
    )

    winner, lines = module.choose_cheapest(summaries, min_runs=3)

    assert winner is None
    assert "inconclusive" in "\n".join(lines)


def test_sweep_refuses_to_rank_too_few_runs() -> None:
    module = _module()
    summaries = module.summarise_runs(_runs("cheapA", [0.01], [True]))

    winner, lines = module.choose_cheapest(summaries, min_runs=3)

    assert winner is None
    assert "fewer than 3" in "\n".join(lines)


def test_sweep_median_is_robust_to_a_single_outlier() -> None:
    module = _module()
    summary = module.summarise_runs(_runs("cheapA", [0.10, 0.02, 0.021], [True] * 3))[0]

    assert summary.median == pytest.approx(0.021)
    assert summary.n == 3


def test_oracle_uses_the_exit_code() -> None:
    module = _module()

    assert module.run_oracle("true") == (True, "")
    # The oracle runs without a shell, so the command is an argv, not a shell
    # string: the exit code must be produced by the program itself.
    ok, reason = module.run_oracle("sh -c 'exit 3'")
    assert ok is False
    assert "exit 3" in reason


def test_oracle_reports_a_timeout_as_a_failure() -> None:
    module = _module()
    ok, reason = module.run_oracle("sleep 5", timeout=0.25)

    assert ok is False
    assert "timed out" in reason


def test_sweep_live_refuses_without_an_oracle(monkeypatch) -> None:
    module = _module()
    called: list[int] = []
    monkeypatch.setattr(module, "read_key", lambda *a, **k: called.append(1))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    # --allow-spend with candidates but no --oracle must refuse before spending
    code = module.main(["--candidates", "a,b", "--allow-spend"])

    assert code == 2
    assert called == []
