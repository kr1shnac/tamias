"""Hardening tests for pricing: the arithmetic, its UNKNOWNs and its edges.

The centrepiece is a differential test.  :func:`oracle_cost` re-derives the cost
from the documented contract -- rates are per 1,000,000 tokens, a missing rate or
count that the arithmetic needs makes the cost UNKNOWN, and a model whose rates
are all zero is free -- and 1000 fixed-seed random cases must agree with
``compute_cost`` to the last cent.  Anything the implementation invents that the
contract does not, and anything it invents *not* to, shows up there.

Alongside it sit the hand-picked edges that random generation reaches rarely or
never: a model nobody priced, counts that cannot all be true, and a free model.
Nothing here fixes ``src/``; a test that exposes a real defect is marked
``xfail(strict=True)`` and carries a BUG id that also appears in
``docs/audit/hardening-findings.md``.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from tamias.pricing import ModelPrice, PriceSheet, compute_cost, load_price_sheet
from tamias.types import Usage

RATE_FIELDS = ("input", "output", "cached_input", "cache_write", "cache_write_1h")
RANDOM_CASES = 1000
RANDOM_SEED = 20261004

# Rates in this project are quoted per million tokens.  Spelled out here so the
# oracle does not import the constant it is supposed to be checking against.
TOKENS_PER_RATE_UNIT = 1_000_000


# --- an oracle written from the contract, not from the implementation ----------


def oracle_cost(model: str, usage: Usage, sheet: PriceSheet) -> float | None:
    """The cost the contract promises, derived independently of ``compute_cost``.

    Returns ``None`` for UNKNOWN.  Written as a flat sequence of questions --
    is the model priced, is it free, can the counts all be true, is anything
    needed missing, then add it up -- so that it does not inherit the shape of
    the code it is checking.
    """
    price = sheet.get(model)
    if price is None:
        return None

    input_p, output_p = price.input, price.output
    cached_p, cw_p = price.cached_input, price.cache_write
    # A sheet that says nothing about 1h writes charges them as ordinary writes.
    cw1h_p = price.cache_write_1h if price.cache_write_1h is not None else cw_p
    rates = (input_p, output_p, cached_p, cw_p, cw1h_p)

    if all(rate is not None for rate in rates) and all(rate == 0.0 for rate in rates):
        return 0.0  # every rate known and every rate zero: genuinely free

    def priced(rate: float | None) -> bool:
        return rate is not None and rate != 0.0

    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.cache_write_tokens,
        usage.cache_write_1h_tokens,
    )
    if any(count is not None and count < 0 for count in counts):
        return None  # a negative count is a lie, not a price

    cached = usage.cached_input_tokens or 0
    written = usage.cache_write_tokens or 0
    if usage.input_tokens is not None and cached + written > usage.input_tokens:
        return None  # more input accounted for than the request was given

    needed = []
    if priced(input_p) and usage.input_tokens is None:
        needed.append("input_tokens")
    if priced(cached_p) and usage.cached_input_tokens is None:
        if not (priced(input_p) and cached_p == input_p):
            needed.append("cached_input_tokens")
    if priced(output_p) and usage.output_tokens is None:
        needed.append("output_tokens")
    if (priced(cw_p) or priced(cw1h_p)) and usage.cache_write_tokens is None:
        needed.append("cache_write_tokens")
    if needed:
        return None  # a needed count never arrived: UNKNOWN, never zero

    one_hour = usage.cache_write_1h_tokens or 0
    uncached = (usage.input_tokens or 0) - cached - written
    total = (
        uncached * (input_p or 0.0)
        + cached * (cached_p or 0.0)
        + (written - one_hour) * (cw_p or 0.0)
        + one_hour * (cw1h_p or 0.0)
        + (usage.output_tokens or 0) * (output_p or 0.0)
    )
    return total / TOKENS_PER_RATE_UNIT


def make_sheet(models: dict[str, dict[str, float | None]]) -> PriceSheet:
    """A PriceSheet built the way :func:`load_price_sheet` would build one."""
    priced: dict[str, ModelPrice] = {}
    for model, table in models.items():
        rates = {field: table.get(field) for field in RATE_FIELDS}
        if rates["cache_write_1h"] is None:
            rates["cache_write_1h"] = rates["cache_write"]
        priced[model] = ModelPrice(**rates)
    return PriceSheet(date="2026-10-04", models=priced, simulated=False)


def assert_agrees(model: str, usage: Usage, sheet: PriceSheet) -> str:
    """Compare the implementation with the oracle; return which way it went."""
    real = compute_cost(model, usage, sheet)
    expected = oracle_cost(model, usage, sheet)
    if expected is None:
        assert real.usd is None, (
            f"cost should be UNKNOWN but was {real.usd!r} for {model} with {usage}\n"
            f"  formula: {real.formula}"
        )
        return "unknown"
    assert real.usd is not None, (
        f"cost should be {expected!r} but was UNKNOWN for {model} with {usage}\n"
        f"  formula: {real.formula}"
    )
    assert math.isclose(real.usd, expected, rel_tol=1e-12, abs_tol=1e-15), (
        f"cost {real.usd!r} != oracle {expected!r} for {model} with {usage}"
    )
    return "zero" if expected == 0.0 else "numeric"


# --- hand-picked cases --------------------------------------------------------


RATED = {"input": 3.0, "output": 15.0, "cached_input": 0.3, "cache_write": 3.75}


@pytest.mark.parametrize(
    "usage",
    [
        Usage(10_000, 2_000, 4_000, 0),
        Usage(10_000, 2_000, None, None),
        Usage(None, 2_000, 4_000, None),
        Usage(10_000, None, 4_000, None),
        Usage(10_000, 2_000, 4_000, 1_000),
        Usage(10_000, 2_000, 4_000, 1_000, 400),
        Usage(0, 0, 0, 0),
        Usage(1, 0, 0, 0),
    ],
    ids=[
        "plain",
        "no_cached_count",
        "no_input_count",
        "no_output_count",
        "with_writes",
        "with_1h_writes",
        "all_zero",
        "single_token",
    ],
)
def test_pricing_matches_its_oracle(usage: Usage) -> None:
    """Every ordinary shape of usage prices the way the contract says."""
    assert_agrees("gpt-4o", usage, make_sheet({"gpt-4o": RATED}))


@pytest.mark.parametrize(
    "usage",
    [
        Usage(10_000, 2_000, 4_000, -1),
        Usage(-1, 2_000, 4_000, None),
        Usage(10_000, -1, 4_000, None),
        Usage(10_000, 2_000, -4_000, None),
        Usage(1_000, 100, 999_999, None),
        Usage(1_000, 100, None, 1_000),
        Usage(10, 0, 0, 0, -5),
    ],
    ids=[
        "negative_writes",
        "negative_input",
        "negative_output",
        "negative_cached",
        "cached_exceeds_input",
        "writes_exceed_input",
        "negative_1h_writes",
    ],
)
def test_impossible_counts_price_as_unknown(usage: Usage) -> None:
    """Counts that cannot all be true mean UNKNOWN, not a smaller bill.

    Billing a contradictory usage report at anything but UNKNOWN is how a
    provider bug turns into a wrong number that looks authoritative.
    """
    assert_agrees("gpt-4o", usage, make_sheet({"gpt-4o": RATED}))


def test_unpriced_model_is_unknown() -> None:
    """A model the sheet does not know cannot be priced at all."""
    sheet = make_sheet({"gpt-4o": RATED})
    breakdown = compute_cost("some-other-model", Usage(10_000, 2_000, 4_000, None), sheet)
    assert breakdown.usd is None
    assert "some-other-model" in breakdown.formula


def test_free_model_costs_nothing() -> None:
    """A model whose every rate is zero is free, whether or not it was used."""
    free = {"input": 0.0, "output": 0.0, "cached_input": 0.0, "cache_write": 0.0}
    sheet = make_sheet({"free-model": free})
    for usage in (Usage(None, None, None, None), Usage(10_000, 2_000, 4_000, 0), Usage(0, 0, 0, 0)):
        assert compute_cost("free-model", usage, sheet).usd == 0.0


def test_cached_at_the_input_rate_needs_no_cached_count() -> None:
    """When cached input costs the same as input, an unreported count is free of charge.

    Charging full input price for tokens the provider simply did not break out
    would overstate cost; refusing to price the request at all would be
    unhelpful.  Zero is the only defensible answer, and this pins it.
    """
    sheet = make_sheet(
        {"same-rate": {"input": 3.0, "output": 15.0, "cached_input": 3.0, "cache_write": 0.0}}
    )
    breakdown = compute_cost("same-rate", Usage(10_000, 2_000, None, 0), sheet)
    # 10_000 input * 3.0 + 2_000 output * 15.0 = 60_000 / 1_000_000
    assert breakdown.usd == 0.06


def test_price_sheet_round_trips_through_toml(tmp_path: Any) -> None:
    """The oracle's sheets and the real loader must produce the same prices."""
    path = tmp_path / "prices.toml"
    path.write_text(
        'date = "2026-10-04"\n'
        "\n"
        "[gpt-4o]\n"
        "input = 3.0\n"
        "output = 15.0\n"
        "cached_input = 0.3\n"
        "cache_write = 3.75\n"
        "cache_write_1h = 6.0\n",
        encoding="utf-8",
    )
    sheet = load_price_sheet(path)
    assert sheet.date == "2026-10-04"
    assert sheet.simulated is False
    price = sheet.get("gpt-4o")
    assert price is not None
    assert (price.input, price.output, price.cached_input) == (3.0, 15.0, 0.3)
    assert (price.cache_write, price.cache_write_1h) == (3.75, 6.0)


# --- 1000 fixed-seed random cases --------------------------------------------


def _random_sheet(rng: random.Random) -> tuple[str, PriceSheet]:
    """A random but fully priced model, or a free one.

    Rates are always all present: a sheet with a rate *missing* is not a random
    edge, it is a contract question, and it gets its own test.
    """
    model = f"random-model-{rng.randint(0, 10_000)}"
    if rng.random() < 0.15:
        zero = {field: 0.0 for field in RATE_FIELDS}
        return model, make_sheet({model: zero})
    input_p = rng.choice([0.1, 0.25, 0.5, 1.0, 2.5, 3.0, 15.0, 75.0])
    output_p = rng.choice([0.0, 0.4, 1.25, 10.0, 15.0, 120.0])
    # Cached input is sometimes free, sometimes the same as input, usually less.
    cached_p = rng.choice([0.0, input_p, round(input_p / 10, 6), round(input_p * 2, 6)])
    cw_p = rng.choice([0.0, 0.3, 1.0, 3.75, 6.0])
    rates: dict[str, float | None] = {
        "input": input_p,
        "output": output_p,
        "cached_input": cached_p,
        "cache_write": cw_p,
    }
    if rng.random() < 0.5:
        rates["cache_write_1h"] = round(cw_p * 1.5, 6)
    return model, make_sheet({model: rates})


def _maybe(rng: random.Random, low: int, high: int) -> int | None:
    """A count in range, or nothing at all, in the proportions seen in the wild."""
    roll = rng.random()
    if roll < 0.1:
        return None
    if roll < 0.15:
        return 0
    return rng.randint(low, high)


def _random_usage(rng: random.Random) -> Usage:
    """A usage report that is often missing fields, sometimes impossible."""
    input_tokens = _maybe(rng, 1, 200_000)
    output_tokens = _maybe(rng, 1, 20_000)
    if rng.random() < 0.06:  # a provider bug: a negative count
        return Usage(-rng.randint(1, 100), output_tokens, 0, 0)
    if input_tokens is None:
        return Usage(input_tokens, output_tokens, None, None)
    if rng.random() < 0.5:
        cached = rng.randint(0, input_tokens)
    else:
        cached = _maybe(rng, 0, input_tokens)
    written = _maybe(rng, 0, max(0, input_tokens - (cached or 0)))
    one_hour = None
    if written and rng.random() < 0.4:
        one_hour = rng.randint(0, written)
    return Usage(input_tokens, output_tokens, cached, written, one_hour)


def test_oracle_and_implementation_agree_on_1000_random_cases() -> None:
    """The differential test: 1000 random cases, none of them hand-picked.

    Counts how each case resolved, so the run cannot pass by accident: a
    generator that only ever produced UNKNOWN, or only ever produced a
    fraction of a cent, would be caught here rather than read as agreement.
    """
    rng = random.Random(RANDOM_SEED)
    tally = {"numeric": 0, "zero": 0, "unknown": 0}

    for _ in range(RANDOM_CASES):
        model, sheet = _random_sheet(rng)
        usage = _random_usage(rng)
        tally[assert_agrees(model, usage, sheet)] += 1

    assert sum(tally.values()) == RANDOM_CASES
    assert tally["numeric"] >= 300, tally
    assert tally["unknown"] >= 100, tally
    assert tally["zero"] >= 1, tally


def test_cost_is_never_negative_where_it_is_known() -> None:
    """A known cost is never below zero, whatever the counts say."""
    rng = random.Random(123)
    for _ in range(500):
        model, sheet = _random_sheet(rng)
        breakdown = compute_cost(model, _random_usage(rng), sheet)
        if breakdown.usd is not None:
            assert breakdown.usd >= 0.0, f"negative cost {breakdown.usd} for {model}"


def test_cost_does_not_fall_when_output_grows() -> None:
    """More output tokens can never make a request cheaper."""
    rng = random.Random(456)
    for _ in range(200):
        model, sheet = _random_sheet(rng)
        input_tokens = rng.randint(1_000, 50_000)
        cached = rng.randint(0, input_tokens)
        first = compute_cost(model, Usage(input_tokens, 100, cached, 0), sheet)
        second = compute_cost(
            model, Usage(input_tokens, 100 + rng.randint(1, 5_000), cached, 0), sheet
        )
        if first.usd is not None and second.usd is not None:
            assert second.usd >= first.usd, f"{second.usd} < {first.usd} for {model}"


# --- rates that were never quoted --------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG-3: a model with an unquoted rate is billed as if that rate were zero",
)
@pytest.mark.parametrize(
    "rates,usage",
    [
        pytest.param({}, Usage(10_000, 2_000, 4_000, None), id="no_rates_at_all"),
        pytest.param(
            {"input": 0.0}, Usage(10_000, 2_000, None, None), id="zero_input_unknown_output"
        ),
        pytest.param({"input": 3.0}, Usage(10_000, 2_000, None, None), id="unknown_output_rate"),
    ],
)
def test_unquoted_rate_is_unknown_not_free(rates: dict[str, float | None], usage: Usage) -> None:
    """A rate the sheet never gave must not be quietly read as zero.

    ``ModelPrice`` documents a ``None`` rate as UNKNOWN, and the report's rule is
    that a missing input to the arithmetic is never guessed at zero.  Both are
    broken here: a sheet entry with no rates at all is called a free model, an
    unquoted output rate prices 2,000 output tokens at nothing, and a zero input
    rate drags the whole model into the free-model shortcut.  The cost of a real
    request is then under-reported as $0.00 with nothing in the output to say why.
    """
    sheet = make_sheet({"gpt-4o": rates})
    breakdown = compute_cost("gpt-4o", usage, sheet)
    assert breakdown.usd is None, (
        f"unquoted rates {sorted(rates)} should leave the cost UNKNOWN, got {breakdown.usd!r}\n"
        f"  formula: {breakdown.formula}"
    )
