"""Tests for turning OpenRouter's model catalogue into tamias price-sheet entries.

Every number here comes from a fixture, never from the network: the fetcher
takes its opener as an argument precisely so that "it fetched the price" can be
tested without fetching anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tamias.pricing_fetch import ParseResult, parse_models

PER_MILLION = Decimal(1_000_000)


def ids(result: ParseResult) -> list[str]:
    return list(result.models)


def reasons(result: ParseResult) -> list[str]:
    return [skipped.reason for skipped in result.skipped]


def skipped_ids(result: ParseResult) -> list[str]:
    return [skipped.model for skipped in result.skipped]


def test_free_model_is_priced_at_zero_everywhere() -> None:
    """A model whose published rates are all zero gets explicit 0 rates.

    Explicit zeros -- rather than omitted fields -- are what let the sheet
    answer 0 instead of UNKNOWN for a free model.
    """
    result = parse_models(
        {"data": [{"id": "free/no-cache", "pricing": {"prompt": "0", "completion": "0"}}]}
    )
    assert ids(result) == ["free/no-cache"]
    price = result.models["free/no-cache"]
    assert price.input == 0.0
    assert price.output == 0.0
    assert result.skipped == []


def test_free_model_with_zero_cache_prices_also_gets_zero_cache_rates() -> None:
    """Free stays free even when the catalogue quotes cache prices of zero."""
    result = parse_models(
        {
            "data": [
                {
                    "id": "free/with-cache",
                    "pricing": {
                        "prompt": "0",
                        "completion": "0",
                        "input_cache_read": "0",
                        "input_cache_write": "0",
                    },
                }
            ]
        }
    )
    price = result.models["free/with-cache"]
    assert (price.input, price.output) == (0.0, 0.0)
    assert (price.cached_input, price.cache_write) == (0.0, 0.0)


def test_paid_model_without_cache_fields_omits_them_rather_than_zeroing_them() -> None:
    """A missing cache price is UNKNOWN, never a free cache read."""
    result = parse_models(
        {
            "data": [
                {"id": "paid/plain", "pricing": {"prompt": "0.000003", "completion": "0.000015"}}
            ]
        }
    )
    price = result.models["paid/plain"]
    assert price.input == 3.0
    assert price.output == 15.0
    assert price.cached_input is None
    assert price.cache_write is None


def test_paid_model_with_cache_fields_prices_them() -> None:
    """`input_cache_read` and `input_cache_write` feed their own sheet fields."""
    result = parse_models(
        {
            "data": [
                {
                    "id": "paid/with-cache",
                    "pricing": {
                        "prompt": "0.000001",
                        "completion": "0.000002",
                        "input_cache_read": "0.0000001",
                        "input_cache_write": "0.00000125",
                    },
                }
            ]
        }
    )
    price = result.models["paid/with-cache"]
    assert (price.input, price.output) == (1.0, 2.0)
    assert price.cached_input == 0.1
    assert price.cache_write == 1.25
    # OpenRouter publishes no 1h cache-write rate, so the sheet quotes none.
    assert price.cache_write_1h is None


@pytest.mark.parametrize(
    ("per_token", "per_million"),
    [
        ("0.00000055", 0.55),
        ("0.000000000000000001", 1e-12),
        ("1e-6", 1.0),
        ("0.000003", 3.0),
        ("0.000000025", 0.025),
        ("0", 0.0),
    ],
    ids=["decimals", "tiny", "exponent", "three", "small", "zero"],
)
def test_per_token_prices_are_converted_with_decimal_arithmetic(
    per_token: str, per_million: float
) -> None:
    """The conversion happens in Decimal, so no float drift enters the sheet."""
    expected = float(Decimal(per_token) * PER_MILLION)
    assert expected == per_million
    result = parse_models(
        {"data": [{"id": "paid/plain", "pricing": {"prompt": per_token, "completion": per_token}}]}
    )
    price = result.models["paid/plain"]
    assert price.input == per_million
    assert price.output == per_million


def test_numeric_json_values_are_accepted_too() -> None:
    """JSON numbers arrive as floats as readily as strings."""
    result = parse_models(
        {"data": [{"id": "paid/numeric", "pricing": {"prompt": 0.000003, "completion": 1.5e-5}}]}
    )
    price = result.models["paid/numeric"]
    assert price.input == 3.0
    assert price.output == 15.0


def test_junk_entries_are_skipped_with_a_reason() -> None:
    """A model we cannot price is dropped, and the report says why."""
    result = parse_models(
        {
            "data": [
                {
                    "id": "bad/negative",
                    "pricing": {"prompt": "-0.000003", "completion": "0.000015"},
                },
                {"id": "bad/non-numeric", "pricing": {"prompt": "free", "completion": "0.000015"}},
                {
                    "id": "bad/negative-cache",
                    "pricing": {
                        "prompt": "0.000003",
                        "completion": "0.000015",
                        "input_cache_read": "-1",
                    },
                },
                {"id": "bad/no-prompt", "pricing": {"completion": "0.000015"}},
                {"id": "bad/no-pricing"},
                {"pricing": {"prompt": "0.000003", "completion": "0.000015"}},
                "not-an-object",
                {"id": "", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
            ]
        }
    )
    assert ids(result) == []
    assert skipped_ids(result) == [
        "bad/negative",
        "bad/non-numeric",
        "bad/negative-cache",
        "bad/no-prompt",
        "bad/no-pricing",
        "<missing id>",
        "not-an-object",
        "",
    ]
    joined = " ".join(reasons(result))
    assert "negative" in joined
    assert "not a number" in joined
    assert "prompt" in joined
    assert "pricing" in joined


def test_bad_cache_price_skips_the_whole_model() -> None:
    """One unusable price poisons the entry: a partial sheet entry is a lie."""
    result = parse_models(
        {
            "data": [
                {
                    "id": "bad/cache",
                    "pricing": {
                        "prompt": "0.000003",
                        "completion": "0.000015",
                        "input_cache_write": "n/a",
                    },
                }
            ]
        }
    )
    assert result.models == {}
    assert len(result.skipped) == 1
    assert "input_cache_write" in result.skipped[0].reason


def test_duplicate_ids_keep_the_first_entry() -> None:
    """The catalogue's first listing of an id is the one the sheet records."""
    result = parse_models(
        {
            "data": [
                {"id": "paid/plain", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
                {"id": "paid/plain", "pricing": {"prompt": "0.000009", "completion": "0.000045"}},
            ]
        }
    )
    assert ids(result) == ["paid/plain"]
    assert result.models["paid/plain"].input == 3.0
    assert len(result.skipped) == 1
    assert result.skipped[0].model == "paid/plain"
    assert "duplicate" in result.skipped[0].reason


def test_boolean_prices_are_not_numbers() -> None:
    """`true` is not a price; JSON booleans must not sneak into the sheet."""
    result = parse_models(
        {"data": [{"id": "paid/bool", "pricing": {"prompt": True, "completion": "0.000015"}}]}
    )
    assert result.models == {}
    assert "not a number" in result.skipped[0].reason


def test_a_payload_without_a_data_list_is_rejected_outright() -> None:
    """A broken envelope is an error, not an empty sheet that looks valid."""
    with pytest.raises(ValueError, match="data"):
        parse_models({"object": "model"})
    with pytest.raises(ValueError, match="data"):
        parse_models({})
    with pytest.raises(ValueError):
        parse_models([{"id": "x"}])
