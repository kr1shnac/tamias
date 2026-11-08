"""Accuracy and precision: the same arithmetic and the same decisions, three ways.

Tamias answers "what did this cost and what did the switch save" in three
places, and a user compares them side by side:

1. :mod:`tamias.pricing` -- what the proxy logs and ``tamias report`` prints.
2. the OpenCode pop-up's TypeScript (``useTamiasData.ts`` / ``priceSheet.ts``)
   -- what ``/tamias`` shows.
3. the reference model in this file -- an independent re-reading of the
   documented rules, in :class:`Decimal`, with its own choices about which arm
   pays for which case.

A disagreement is a fault whichever surface it appears on.  The checks are
adversarial rather than representative: a cached bucket with no quoted rate,
cache writes that overrun the prompt, a requested model the sheet never
quoted, missing token counts, micro rates small enough to expose float drift,
and a switch whose baseline is dearer than what ran -- where the answer must
come out negative, never clamped to zero.

Nothing here opens a socket.  The TypeScript half runs ``bun`` against the
plugin source in this repository and is skipped when Bun is unavailable; the
two surfaces' money formatting is asserted equal for the same reason.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tamias.candidates import blended_cost
from tamias.pricing import ModelPrice, PriceSheet, compute_cost, load_effective_sheet
from tamias.store import Store
from tamias.types import CostBreakdown, Decision, Usage

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "tamias-opencode-plugin"

# A sheet built to break arithmetic rather than to describe a market.
SHEET = """
date = "2026-11-06"

["paid/one"]
input = 0.5
output = 1.0
cached_input = 0.05
cache_write = 0.75

["paid/two"]
input = 0.25
output = 0.5

["free/one"]
input = 0.0
output = 0.0

["tiny/rates"]
input = 0.000001
output = 0.000002

["cache/dear"]
input = 1.0
output = 3.0
cached_input = 0.1
cache_write = 1.25

["no/cache-rate"]
input = 1.0
output = 2.0
"""

TIED = 10 ** -15
TIED_TOTAL = 10 ** -12


def usage(
    input_tokens: int | None,
    output_tokens: int | None,
    cached: int | None = None,
    writes: int | None = None,
    billed: float | None = None,
) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=writes,
        provider_cost_usd=billed,
    )


# Every case names the rule it is testing, so a failure says what broke.
CASES: list[dict[str, Any]] = [
    dict(
        name="an applied switch to a free model",
        requested="paid/two",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500),
    ),
    dict(
        name="the provider's billed amount is carried, not recomputed over",
        requested="paid/one",
        used="paid/two",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500, cached=0, writes=0, billed=0.4),
    ),
    dict(
        name="a shadow switch is priced against its target, not acted on",
        requested="paid/one",
        used="paid/one",
        router_mode="shadow",
        action="SWITCH",
        target="paid/two",
        usage=usage(1000, 500),
    ),
    dict(
        name="a stay row has a zero saving rather than a missing one",
        requested="paid/one",
        used="paid/one",
        router_mode="shadow",
        action="STAY",
        usage=usage(1000, 500, cached=900, writes=0),
    ),
    dict(
        name="a warm conversation on a model with a dear cache",
        requested="cache/dear",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(100000, 1000, cached=99000, writes=1000),
    ),
    dict(
        name="micro rates that expose float drift",
        requested="tiny/rates",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(1_000_000, 1_000_000),
    ),
    dict(
        name="cached tokens against a model with no cached rate",
        requested="paid/two",
        used="paid/two",
        router_mode="active",
        action="STAY",
        usage=usage(1000, 500, cached=600),
    ),
    dict(
        name="cache writes against a model with no write rate",
        requested="paid/one",
        used="paid/two",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500, cached=900, writes=200),
    ),
    dict(
        name="cached plus writes larger than the prompt",
        requested="paid/one",
        used="paid/one",
        router_mode="active",
        action="STAY",
        usage=usage(1000, 500, cached=900, writes=200),
    ),
    dict(
        name="a requested model the sheet never quoted",
        requested="unquoted/model",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500),
    ),
    dict(
        name="no counts at all, on a free used model",
        requested="paid/two",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(None, None),
    ),
    dict(
        name="a failed request still prices",
        requested="paid/one",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500, cached=0, writes=0),
        status="500",
    ),
    dict(
        name="billed and computed agree, which is the honest case",
        requested="paid/two",
        used="free/one",
        router_mode="active",
        action="SWITCH",
        usage=usage(1000, 500, cached=0, writes=0, billed=0.0003125),
        computed_list_cost_usd=0.0003125,
    ),
]


# ------------------------------------------------------------- the reference model ---


def parse_sheet(text: str) -> dict[str, dict[str, Decimal]]:
    import tomllib

    document = tomllib.loads(text)
    return {
        model: {key: Decimal(str(value)) for key, value in table.items()}
        for model, table in document.items()
        if isinstance(table, dict)
    }


def reference_list_cost(
    model: str | None, counts: Usage, sheet: dict[str, dict[str, Decimal]]
) -> Decimal | None:
    """The documented cache arithmetic, in Decimal, written from the rules.

    ``input_tokens`` is the whole prompt, so cached and write tokens are carved
    out of it rather than added on top; a rate the sheet never quoted is unknown
    rather than zero; and a model whose quoted rates are all zero is exactly
    free, even with no counts to multiply.
    """
    if not model or model not in sheet:
        return None
    rates = sheet[model]
    if rates.get("input") is None or rates.get("output") is None:
        return None
    quoted = [
        rates.get(key)
        for key in ("input", "output", "cached_input", "cache_write", "cache_write_1h")
    ]

    cached = counts.cached_input_tokens
    writes = counts.cache_write_tokens or 0
    cached_tokens = cached or 0

    # Rule 1b, before the free shortcut: a bucket with tokens in it and no
    # quoted rate cannot be priced.
    if cached_tokens > 0 and rates.get("cached_input") is None:
        return None
    if writes > 0 and rates.get("cache_write") is None:
        return None

    # Rule 2: a bucket the sheet *does* price cannot be left uncounted.  A
    # model that quotes a nonzero cache-write rate, and a request that never
    # says how much was written, is unknown rather than zero writes.
    quoted_writes = [
        rates.get("cache_write"),
        rates.get("cache_write_1h"),
    ]
    if (
        any(value not in (None, 0) for value in quoted_writes)
        and counts.cache_write_tokens is None
    ):
        return None

    # Rule 1, before any count is required: every quoted rate is zero and no
    # unquoted bucket was used, so the model is exactly free even with no
    # usage block at all.  (Python reaches this before its count rules.)
    present = [value for value in quoted if value is not None]
    if present and all(value == 0 for value in present):
        return Decimal(0)

    if counts.input_tokens is None or counts.input_tokens < 0:
        return None
    if counts.output_tokens is None and rates["output"] > 0:
        return None
    if counts.output_tokens is not None and counts.output_tokens < 0:
        return None
    if cached is not None and cached < 0:
        return None
    if writes < 0:
        return None

    # A model that quotes a distinct cached rate but whose request never says
    # how much of the prompt was cached cannot be priced: the share is exactly
    # the unknown.  (Zero input and zero cached rates would make it moot.)
    if (
        cached is None
        and rates.get("cached_input") is not None
        and rates["cached_input"] != rates["input"]
        and rates["cached_input"] != 0
        and rates["input"] != 0
    ):
        return None

    if cached_tokens + writes > counts.input_tokens:
        return None

    # A bucket with no tokens in it costs nothing, so an unquoted rate there
    # falls back to the input rate only because it multiplies zero.
    cached_rate = rates.get("cached_input", rates["input"])
    write_rate = rates.get("cache_write", rates["input"])
    uncached = counts.input_tokens - cached_tokens - writes
    total = (
        uncached * rates["input"]
        + cached_tokens * cached_rate
        + writes * write_rate
        + (counts.output_tokens or 0) * rates["output"]
    )
    return total / Decimal(1_000_000)


def reference_warm(counts: Usage) -> Usage:
    """The same request with the prompt cache already warm."""
    if counts.input_tokens is None:
        return counts
    return Usage(
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
        cached_input_tokens=counts.input_tokens,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        provider_cost_usd=counts.provider_cost_usd,
    )


def reference_row(case: dict[str, Any], sheet: dict[str, dict[str, Decimal]]) -> dict[str, Any]:
    """What one row shows in the pop-up: actual, baseline, saved, priced.

    The pop-up's rule is the one that makes a *displayed* number right: a
    provider-billed amount is a measurement and wins, and the baseline for an
    applied switch is the requested model with a warm cache.
    """
    counts = case["usage"]
    computed = reference_list_cost(case["used"], counts, sheet)
    actual = next(
        (
            Decimal(str(value))
            for value in (
                counts.provider_cost_usd,
                case.get("computed_list_cost_usd"),
                computed,
            )
            if value is not None
        ),
        None,
    )

    switched = case["requested"] != case["used"]
    if switched:
        baseline = reference_list_cost(case["requested"], reference_warm(counts), sheet)
    else:
        baseline = actual
    saved = (
        None
        if actual is None or baseline is None
        else (baseline - actual if switched else Decimal(0))
    )
    return {
        "actual": actual,
        "baseline": baseline,
        "saved": saved,
        "priced": actual is not None and baseline is not None,
        "switched": switched,
    }


def reference_report(
    cases: list[dict[str, Any]], sheet: dict[str, dict[str, Decimal]]
) -> dict[str, Any]:
    """What ``tamias report`` prints: its two savings totals and their caveats.

    ``report`` prices the estimated saving's two arms from the sheet.  The
    realised saving also starts from a list-price counterfactual for the
    requested model, but its actual side is the provider's billed amount when
    the row carries one; only rows without a bill fall back to list-price
    arithmetic for the model that ran.  The switch arm of the estimate is the
    row's logged target for a shadow or declined decision, and the model that
    ran for an applied one.
    """
    ranked = sorted(
        ((price["input"] + price["output"]) / 2, name) for name, price in sheet.items()
    )
    cheapest = ranked[0][1] if ranked else None

    switched = 0
    unpriced = 0
    saved = Decimal(0)
    realised_count = 0
    realised_unpriced = 0
    realised_saved = Decimal(0)

    for case in cases:
        if case["action"] != "SWITCH":
            continue
        switched += 1
        applied = case["used"] != case["requested"]
        if applied:
            stay, switch = case["requested"], case["used"]
        else:
            target = case.get("target") or cheapest
            stay, switch = case["used"], target
        if stay == switch:
            saved += Decimal(0)
            continue
        staying = reference_list_cost(stay, reference_warm(case["usage"]), sheet)
        switching = reference_list_cost(switch, case["usage"], sheet)
        if staying is None or switching is None:
            unpriced += 1
        else:
            saved += staying - switching

        if case["used"] != case["requested"]:
            requested_cost = reference_list_cost(
                case["requested"], reference_warm(case["usage"]), sheet
            )
            billed = case["usage"].provider_cost_usd
            used_cost = (
                Decimal(str(billed))
                if billed is not None
                else reference_list_cost(case["used"], case["usage"], sheet)
            )
            if requested_cost is None or used_cost is None:
                realised_unpriced += 1
            else:
                realised_saved += requested_cost - used_cost
                realised_count += 1

    return {
        "switched": switched,
        "unpriced": unpriced,
        "saved": saved,
        "realised_count": realised_count,
        "realised_unpriced": realised_unpriced,
        "realised_saved": realised_saved,
        "cheapest": cheapest,
    }


# ------------------------------------------------- the arithmetic, in three languages ---


def _sheet_file(tmp_path: Path, name: str = "prices.toml") -> Path:
    path = tmp_path / name
    path.write_text(SHEET, encoding="utf-8")
    return path


def test_python_and_the_reference_agree_on_every_case(tmp_path: Path) -> None:
    """The proxy's own arithmetic against the independent Decimal model."""
    sheet = load_effective_sheet(_sheet_file(tmp_path))
    reference = parse_sheet(SHEET)
    for case in CASES:
        actual = compute_cost(case["used"], case["usage"], sheet).usd
        expected = reference_list_cost(case["used"], case["usage"], reference)
        label = case["name"]
        assert (actual is None) == (expected is None), label
        if actual is not None:
            assert abs(Decimal(str(actual)) - expected) <= Decimal(TIED), label


def test_the_free_and_unknown_cases_are_distinct(tmp_path: Path) -> None:
    """Zero is a price; unknown is an absence.  They must never merge."""
    sheet = load_effective_sheet(_sheet_file(tmp_path))

    free = compute_cost(
        "free/one",
        usage(None, None),
        sheet,
    )
    cached_without_a_rate = compute_cost("no/cache-rate", usage(1000, 500, cached=900), sheet)
    writes_without_a_rate = compute_cost("paid/two", usage(1000, 500, writes=200), sheet)

    assert free.usd == 0.0 and free.source == "computed"
    assert cached_without_a_rate.usd is None
    assert "cached_input_rate" in cached_without_a_rate.formula
    assert writes_without_a_rate.usd is None
    assert "cache_write_rate" in writes_without_a_rate.formula


def test_inconsistent_counts_are_refused_not_rounded(tmp_path: Path) -> None:
    sheet = load_effective_sheet(_sheet_file(tmp_path))

    cost = compute_cost("paid/one", usage(1000, 500, cached=900, writes=200), sheet)

    assert cost.usd is None, "cached plus writes cannot exceed the prompt"


def test_blended_cost_uses_the_stated_mix_and_leaves_unpriced_last() -> None:
    sheet = PriceSheet(date="2026-11-06", models={"m": ModelPrice(input=1.0, output=3.0)})

    assert blended_cost(sheet.get("m")) == pytest.approx(0.75 * 1.0 + 0.25 * 3.0)
    assert blended_cost(sheet.get("absent")) is None


@pytest.mark.skipif(
    not shutil.which("bun") or not PLUGIN.is_dir(),
    reason="Bun or the plugin source is unavailable",
)
def test_the_popup_and_the_reference_agree_on_every_case(tmp_path: Path) -> None:
    """The pop-up, run by its own runtime, against the same reference model."""
    payload = {
        "sheet": SHEET,
        "rows": [
            {
                "id": index,
                "model_requested": case["requested"],
                "model_used": case["used"],
                "input_tokens": case["usage"].input_tokens,
                "output_tokens": case["usage"].output_tokens,
                "cached_input_tokens": case["usage"].cached_input_tokens,
                "cache_write_tokens": case["usage"].cache_write_tokens,
                "status": case.get("status", "200"),
                "router_mode": case["router_mode"],
                "provider_cost_usd": case["usage"].provider_cost_usd,
                "computed_list_cost_usd": case.get("computed_list_cost_usd"),
            }
            for index, case in enumerate(CASES, start=1)
        ],
    }
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(json.dumps(payload), encoding="utf-8")
    out_path = tmp_path / "out.json"
    script = tmp_path / "probe.ts"
    script.write_text(_PROBE.format(plugin=PLUGIN, rows=rows_path, out=out_path), encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - test-local paths only
        ["bun", "run", str(script)], capture_output=True, text=True, timeout=300, check=False
    )
    assert completed.returncode == 0, completed.stderr
    produced = json.loads(out_path.read_text(encoding="utf-8"))
    reference = parse_sheet(SHEET)

    for case, shown in zip(CASES, produced["rows"], strict=True):
        expected = reference_row(case, reference)
        label = f"{case['name']} ({case['requested']} -> {case['used']})"
        assert (expected["actual"] is None) == (shown["actual"] is None), label
        if expected["actual"] is not None:
            assert (
                abs(expected["actual"] - Decimal(str(shown["actual"]))) <= Decimal(TIED)
            ), label
        assert (expected["baseline"] is None) == (shown["baseline"] is None), label
        if expected["baseline"] is not None:
            assert (
                abs(expected["baseline"] - Decimal(str(shown["baseline"]))) <= Decimal(TIED)
            ), label
        assert (expected["saved"] is None) == (shown["saved"] is None), label
        if expected["saved"] is not None:
            assert (
                abs(expected["saved"] - Decimal(str(shown["saved"]))) <= Decimal(TIED)
            ), label
        assert expected["priced"] == shown["priced"], label
        assert expected["switched"] == shown["switched"], label

    priced = [reference_row(case, reference) for case in CASES]
    priced = [entry for entry in priced if entry["priced"]]
    assert abs(
        sum(entry["actual"] for entry in priced) - Decimal(str(produced["summary"]["actual_total"]))
    ) <= Decimal(TIED_TOTAL)
    assert abs(
        sum(entry["baseline"] for entry in priced)
        - Decimal(str(produced["summary"]["baseline_total"]))
    ) <= Decimal(TIED_TOTAL)

    # A row that reports input tokens but no cache count is UNKNOWN, not zero:
    # the share is computed only over rows where both numbers are known.
    known = [
        case
        for case in CASES
        if case["usage"].input_tokens and case["usage"].cached_input_tokens is not None
    ]
    cached = sum(case["usage"].cached_input_tokens or 0 for case in known)
    total_input = sum(case["usage"].input_tokens or 0 for case in known)
    assert produced["summary"]["cache_share"] == pytest.approx(cached / total_input, rel=1e-12)


def _log_cases(db: Path, prices: Path) -> Path:
    store = Store(str(db))
    for case in CASES:
        store.log_request(
            ts="2026-11-06T00:00:00Z",
            session_id="s1",
            model_requested=case["requested"],
            model_used=case["used"],
            usage=case["usage"],
            cost=CostBreakdown(
                usd=case.get("computed_list_cost_usd"),
                formula="test",
                price_sheet_date="2026-11-06",
            ),
            latency_ms=10,
            status=case.get("status", "200"),
            decision=Decision(
                action=case["action"],
                target_model=case.get("target")
                or (case["used"] if case["action"] == "SWITCH" else None),
                reason="test",
            ),
            price_sheet=str(prices),
            router_mode=case["router_mode"],
        )
    store.close()
    return db


def test_the_report_agrees_with_the_reference_model(tmp_path: Path, capsys: Any) -> None:
    """``tamias report``'s two savings totals against an independent model."""
    from tamias.cli import main

    prices = _sheet_file(tmp_path)
    db = _log_cases(tmp_path / "requests.db", prices)

    assert main(["report", "--db", str(db), "--prices", str(prices)]) == 0
    out = capsys.readouterr().out
    expected = reference_report(CASES, parse_sheet(SHEET))

    printed = re.search(r"estimated saving: \$([0-9.]+|unknown[^ ]*)", out)
    assert printed is not None, out
    if expected["unpriced"]:
        # A number is printed next to the count of rows that could not be
        # priced, so the priced subset is never quoted as the whole set.
        assert printed.group(1) != "unknown"
        assert (
            f"{expected['unpriced']} of {expected['switched']} "
            "switched requests not priced" in out
        )
    amount = re.search(r"estimated saving: \$([0-9.]+)", out)
    assert amount is not None, out
    assert float(amount.group(1)) == pytest.approx(
        float(expected["saved"]), abs=0.0005
    ), f"report {amount.group(1)} vs reference {expected['saved']}"

    line = re.search(r"realised saving[^:]*: (-)?\$([0-9.]+)", out)
    if expected["realised_count"]:
        assert line is not None, out
        printed_realised = float(line.group(2)) * (-1 if line.group(1) else 1)
        assert printed_realised == pytest.approx(
            float(expected["realised_saved"]), abs=0.0005
        ), f"realised {printed_realised} vs reference {expected['realised_saved']}"
    if expected["realised_unpriced"]:
        assert f"{expected['realised_unpriced']} of" in out


def test_a_switch_that_lost_money_is_reported_as_a_cost(tmp_path: Path, capsys: Any) -> None:
    """F-1: the instrument must be able to report a loss.

    A switch onto a model that has to re-ingest a cold prompt is dearer than
    staying on the requested model with a warm cache, so the saving is
    negative.  A report that clamped it to zero would only ever confirm
    success, which is the failure this whole arithmetic exists to prevent.
    """
    from tamias.cli import main

    prices = _sheet_file(tmp_path)
    db = tmp_path / "loss.db"
    store = Store(str(db))
    store.log_request(
        ts="2026-11-06T00:00:00Z",
        session_id="s1",
        model_requested="paid/one",
        model_used="cache/dear",
        usage=usage(100000, 1000, cached=0, writes=0),
        cost=CostBreakdown(usd=None, formula="test", price_sheet_date="2026-11-06"),
        latency_ms=10,
        status="200",
        decision=Decision(action="SWITCH", target_model="cache/dear", reason="test"),
        price_sheet=str(prices),
        router_mode="active",
    )
    store.close()

    assert main(["report", "--db", str(db), "--prices", str(prices)]) == 0
    out = capsys.readouterr().out

    assert "realised saving" in out
    match = re.search(r"realised saving[^:]*: (-)?\$([0-9.]+)", out)
    assert match is not None, out
    assert match.group(1) == "-", f"a loss must keep its sign: {out}"
    assert float(match.group(2)) > 0

    estimated = re.search(r"estimated saving: (-)?\$([0-9.]+)", out)
    assert estimated is not None and estimated.group(1) == "-", out


def test_the_two_surfaces_disagree_only_on_a_billed_row(tmp_path: Path) -> None:
    """The one place the pop-up and the report differ on purpose.

    The pop-up shows what a row **cost**, so a provider-billed amount wins.
    The report prices both arms from the sheet, so its saving stays an
    apples-to-apples comparison.  On a billed row those give different
    numbers, and a user comparing the two must be able to predict that --
    otherwise "the pop-up says I lost money, the report says I saved it"
    reads as a bug.
    """
    reference = parse_sheet(SHEET)
    case = next(
        entry
        for entry in CASES
        if entry["usage"].provider_cost_usd is not None
        and entry["usage"].provider_cost_usd > 0.3
    )

    popup = reference_row(case, reference)
    report = reference_report([case], reference)

    assert popup["actual"] == Decimal("0.4"), "the pop-up carries the billed amount"
    assert popup["saved"] < 0, "against a billed amount this switch lost money"
    assert report["saved"] > 0, "priced from the sheet the same switch saved"
    assert report["saved"] != -popup["saved"], "the two must not silently coincide"


# -------------------------------------------------------------------- the fuzz ---


def _random_cases(count: int = 120, seed: int = 20261106) -> list[dict[str, Any]]:
    """Cases a human would not write, from a fixed seed so a failure repeats.

    The interesting territory is the boundaries: zero, one, ``None``, cached
    equal to the prompt, writes that overrun it, a model the sheet never
    quoted, a rate so small it needs scientific notation, and a billed amount
    that disagrees with every computed figure.
    """
    rng = random.Random(seed)
    models = ["paid/one", "paid/two", "free/one", "tiny/rates", "cache/dear", "no/cache-rate"]
    cases: list[dict[str, Any]] = []
    for _ in range(count):
        counts = [None, 0, 1, 999, 1000, 100_000, 1_000_000, -1]
        cases.append(
            dict(
                name="fuzz",
                requested=rng.choice(models + ["unquoted/model"]),
                used=rng.choice(models),
                router_mode=rng.choice(["active", "shadow"]),
                action=rng.choice(["SWITCH", "STAY"]),
                usage=usage(
                    rng.choice(counts),
                    rng.choice(counts),
                    rng.choice(counts),
                    rng.choice(counts),
                    billed=rng.choice([None, 0.0, 0.0000001, 0.4, 12.5]),
                ),
            )
        )
    return cases


@pytest.mark.skipif(
    not shutil.which("bun") or not PLUGIN.is_dir(),
    reason="Bun or the plugin source is unavailable",
)
def test_fuzzed_rows_agree_across_all_three_implementations(tmp_path: Path) -> None:
    """Seeded fuzzing: Python, the pop-up and the reference must not diverge.

    A hand-written case list proves the rules I thought of; a seeded fuzz over
    the boundaries proves the ones I did not.  The seed is fixed so a failure
    is reproducible, and the divergences are reported with their inputs.
    """
    cases = _random_cases()
    payload = {
        "sheet": SHEET,
        "rows": [
            {
                "id": index,
                "model_requested": case["requested"],
                "model_used": case["used"],
                "input_tokens": case["usage"].input_tokens,
                "output_tokens": case["usage"].output_tokens,
                "cached_input_tokens": case["usage"].cached_input_tokens,
                "cache_write_tokens": case["usage"].cache_write_tokens,
                "status": case.get("status", "200"),
                "router_mode": case["router_mode"],
                "provider_cost_usd": case["usage"].provider_cost_usd,
            }
            for index, case in enumerate(cases, start=1)
        ],
    }
    rows_path = tmp_path / "fuzz.json"
    rows_path.write_text(json.dumps(payload), encoding="utf-8")
    out_path = tmp_path / "fuzz-out.json"
    script = tmp_path / "fuzz.ts"
    script.write_text(_PROBE.format(plugin=PLUGIN, rows=rows_path, out=out_path), encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - test-local paths only
        ["bun", "run", str(script)], capture_output=True, text=True, timeout=300, check=False
    )
    assert completed.returncode == 0, completed.stderr
    produced = json.loads(out_path.read_text(encoding="utf-8"))

    sheet = load_effective_sheet(_sheet_file(tmp_path))
    reference = parse_sheet(SHEET)
    divergences: list[str] = []

    for case, shown in zip(cases, produced["rows"], strict=True):
        counts = case["usage"]
        label = (
            f"{case['requested']} -> {case['used']} "
            f"in={counts.input_tokens} out={counts.output_tokens} "
            f"cached={counts.cached_input_tokens} writes={counts.cache_write_tokens} "
            f"billed={counts.provider_cost_usd} mode={case['router_mode']}"
        )
        python_actual = compute_cost(case["used"], counts, sheet).usd
        expected = reference_row(case, reference)

        # Python's ``compute_cost`` is the *list* price and ignores any billed
        # amount, exactly as ``report`` does; the pop-up shows the billed one
        # when the provider gave it.  So the two "actual" figures only have to
        # agree on a row that carries no billed amount -- the divergence on the
        # others is the one pinned by
        # :func:`test_the_two_surfaces_disagree_only_on_a_billed_row`.
        for field, python_value, ts_value, reference_value in (
            (
                "actual",
                None if counts.provider_cost_usd is not None else python_actual,
                shown["actual"],
                expected["actual"],
            ),
            ("baseline", None, shown["baseline"], expected["baseline"]),
            ("saved", None, shown["saved"], expected["saved"]),
        ):
            if python_value is not None and reference_value is not None:
                if abs(Decimal(str(python_value)) - reference_value) > Decimal(TIED):
                    divergences.append(
                        f"{label}: {field} python {python_value} vs reference {reference_value}"
                    )
            if reference_value is None or ts_value is None:
                if (reference_value is None) != (ts_value is None):
                    divergences.append(
                        f"{label}: {field} pop-up {ts_value} vs reference {reference_value}"
                    )
            elif abs(Decimal(str(ts_value)) - reference_value) > Decimal(TIED):
                divergences.append(
                    f"{label}: {field} pop-up {ts_value} vs reference {reference_value}"
                )

        # A row that carries a billed amount must show *that* number, because
        # it is the measurement and everything else is arithmetic about it.
        if counts.provider_cost_usd is not None:
            if Decimal(str(shown["actual"])) != Decimal(str(counts.provider_cost_usd)):
                divergences.append(
                    f"{label}: pop-up ignored the billed {counts.provider_cost_usd}"
                )

        if expected["priced"] != shown["priced"]:
            divergences.append(
                f"{label}: priced pop-up {shown['priced']} vs reference {expected['priced']}"
            )
        if expected["switched"] != shown["switched"]:
            divergences.append(
                f"{label}: switched pop-up {shown['switched']} vs reference {expected['switched']}"
            )

    assert not divergences, "the arithmetic diverged on:\n" + "\n".join(divergences[:10])


# ------------------------------------------------------------------- the presentation ---


def test_money_follows_the_shared_rules() -> None:
    from tamias.cli import _money

    assert _money(0.0) == "$0.00"
    assert _money(0.00001) == "<$0.0001", "below the floor it is named, not invented"
    assert _money(-0.00001) == "-<$0.0001", "the sign survives the floor"
    assert _money(0.00012345) == "$0.000123"
    assert _money(0.5001) == "$0.500100"
    assert _money(1.0) == "$1.0000"
    assert _money(1234.5) == "$1234.5000"
    assert _money(-1234.5) == "-$1234.5000"


@pytest.mark.skipif(not shutil.which("bun"), reason="Bun is unavailable")
def test_the_two_surfaces_render_every_amount_identically(tmp_path: Path) -> None:
    """`tamias report` and `/tamias` are read side by side.

    Two renderings of one number read as two numbers, so both formatters are
    asserted equal over every branch of the rules.
    """
    from tamias.cli import _money

    values = [
        0.0,
        -0.0,
        0.00005,
        -0.00005,
        0.0001,
        0.00012345,
        -0.00012345,
        0.005,
        0.5001,
        0.9999,
        1.0,
        1.00005,
        -1.00005,
        1234.5,
        -1234.5,
    ]
    payload = tmp_path / "money.json"
    payload.write_text(json.dumps(values), encoding="utf-8")
    out = tmp_path / "money-out.json"
    script = tmp_path / "money.ts"
    script.write_text(
        _MONEY_PROBE.format(plugin=PLUGIN, values=payload, out=out), encoding="utf-8"
    )
    completed = subprocess.run(  # noqa: S603 - test-local paths only
        ["bun", "run", str(script)], capture_output=True, text=True, timeout=120, check=False
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(out.read_text(encoding="utf-8"))

    for value, shown in zip(values, rendered, strict=True):
        assert shown == _money(value), f"{value!r}: pop-up {shown!r}, report {_money(value)!r}"


# ------------------------------------------------------------------------ the overlay ---


def test_the_operator_overlay_wins_over_the_generated_sheet(tmp_path: Path) -> None:
    from tamias.cli import main

    generated = tmp_path / "prices.toml"
    generated.write_text(
        'date = "2026-11-06"\n\n["paid/one"]\ninput = 0.5\noutput = 1.0\n', encoding="utf-8"
    )
    assert (
        main(
            [
                "prices",
                "set",
                "paid/one",
                "--input",
                "0",
                "--output",
                "0",
                "--sheet",
                str(generated),
            ]
        )
        == 0
    )

    effective = load_effective_sheet(generated)

    assert effective.get("paid/one").input == 0.0
    assert effective.get("paid/one").output == 0.0


_PROBE = """
import {{ readFileSync, writeFileSync }} from "node:fs";
import path from "node:path";

const plugin = path.resolve("{plugin}");
const {{ priceRow, summarizeRows }} = await import(path.join(plugin, "src/hooks/useTamiasData.ts"));
const {{ parsePriceSheet }} = await import(path.join(plugin, "src/utils/priceSheet.ts"));

const fixture = JSON.parse(readFileSync("{rows}", "utf-8"));
const sheet = parsePriceSheet(fixture.sheet);
const rows = fixture.rows.map((raw) => priceRow(raw, sheet));
const summary = summarizeRows(rows);

writeFileSync(
  "{out}",
  JSON.stringify({{
    rows: rows.map((row) => ({{
      actual: row.actual,
      baseline: row.baseline,
      saved: row.saved,
      priced: row.priced,
      switched: row.switched,
    }})),
    summary: {{
      actual_total: summary.actual_total,
      baseline_total: summary.baseline_total,
      cache_share: summary.cache_share,
    }},
  }}),
);
"""

_MONEY_PROBE = """
import {{ readFileSync, writeFileSync }} from "node:fs";
import path from "node:path";

const {{ money }} = await import(path.join(path.resolve("{plugin}"), "src/utils/format.ts"));
const values = JSON.parse(readFileSync("{values}", "utf-8"));
writeFileSync("{out}", JSON.stringify(values.map((value) => money(value))));
"""
