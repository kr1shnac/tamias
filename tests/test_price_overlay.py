"""The hand-written price overlay: price a provider the generated sheet cannot.

The claim these tests defend: routing can compare providers that OpenRouter's
catalogue does not cover -- a free development tier, a local model, a provider
that bills differently for the same id -- without editing the generated sheet,
and a re-fetch of that sheet cannot erase the operator's rates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.cli import main
from tamias.pricing import (
    load_effective_sheet,
    load_price_sheet,
    merge_price_sheets,
    overlay_path,
)

BASE = """
date = "2026-11-02"
simulated = false

["openrouter/paid"]
input = 3.0
output = 9.0

["nvidia/shared"]
input = 0.6
output = 2.4
"""


def write_base(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(BASE, encoding="utf-8")
    return path


def test_the_overlay_sits_beside_the_generated_sheet():
    assert overlay_path("/home/k/.tamias/prices.toml") == Path("/home/k/.tamias/prices.local.toml")


def test_a_model_absent_from_the_sheet_can_be_priced(tmp_path):
    base = write_base(tmp_path)
    overlay_path(base).write_text(
        '["groq/llama-4"]\ninput = 0.0\noutput = 0.0\n', encoding="utf-8"
    )

    sheet = load_effective_sheet(base)

    assert sheet.get("groq/llama-4").input == 0.0
    assert sheet.get("openrouter/paid").input == 3.0, "the base entries are still there"


def test_the_overlay_wins_over_the_same_id_in_the_sheet(tmp_path):
    base = write_base(tmp_path)
    overlay_path(base).write_text(
        '["nvidia/shared"]\ninput = 0.0\noutput = 0.0\n', encoding="utf-8"
    )

    sheet = load_effective_sheet(base)

    assert sheet.get("nvidia/shared").input == 0.0
    assert sheet.date == "2026-11-02", "the sheet's date still describes the generated file"
    assert sheet.simulated is False


def test_a_missing_overlay_changes_nothing(tmp_path):
    base = write_base(tmp_path)

    assert load_effective_sheet(base) == load_price_sheet(base)


def test_merge_keeps_every_unrelated_entry(tmp_path):
    base = load_price_sheet(write_base(tmp_path))
    overlay_path(write_base(tmp_path)).write_text(
        '["extra/one"]\ninput = 0.1\noutput = 0.2\n', encoding="utf-8"
    )
    merged = merge_price_sheets(base, load_effective_sheet(write_base(tmp_path)))

    assert set(merged.models) == {"openrouter/paid", "nvidia/shared", "extra/one"}


def test_prices_set_writes_the_overlay_and_keeps_other_models(tmp_path):
    base = write_base(tmp_path)

    code = main(
        [
            "prices",
            "set",
            "groq/llama-4",
            "--input",
            "0",
            "--output",
            "0",
            "--sheet",
            str(base),
        ]
    )
    assert code == 0
    code = main(
        ["prices", "set", "deepseek/chat", "--input", "0.2", "--sheet", str(base)]
    )
    assert code == 0

    overlay = overlay_path(base).read_text(encoding="utf-8")
    assert '"groq/llama-4"' in overlay and '"deepseek/chat"' in overlay
    assert load_effective_sheet(base).get("groq/llama-4").output == 0.0
    assert load_effective_sheet(base).get("deepseek/chat").input == 0.2


def test_prices_set_refuses_a_negative_rate(tmp_path):
    base = write_base(tmp_path)

    assert main(["prices", "set", "m", "--input", "-1", "--sheet", str(base)]) == 2
    assert not overlay_path(base).exists()


def test_prices_set_needs_at_least_one_rate(tmp_path):
    base = write_base(tmp_path)

    assert main(["prices", "set", "m", "--sheet", str(base)]) == 2


def test_a_fetch_style_rewrite_of_the_sheet_leaves_the_overlay_alone(tmp_path):
    base = write_base(tmp_path)
    main(["prices", "set", "groq/llama-4", "--input", "0", "--output", "0", "--sheet", str(base)])

    base.write_text(BASE.replace("3.0", "4.0"), encoding="utf-8")

    sheet = load_effective_sheet(base)
    assert sheet.get("openrouter/paid").input == 4.0
    assert sheet.get("groq/llama-4").input == 0.0


@pytest.mark.parametrize("field", ["input", "output", "cached_input", "cache_write"])
def test_every_rate_field_round_trips(tmp_path, field):
    base = write_base(tmp_path)

    main(["prices", "set", "m", f"--{field.replace('_', '-')}", "0.25", "--sheet", str(base)])

    price = load_effective_sheet(base).get("m")
    assert getattr(price, field) == 0.25


def test_prices_set_creates_the_directory_for_a_new_overlay(tmp_path):
    base = tmp_path / "not-yet" / "prices.toml"

    assert main(["prices", "set", "m", "--input", "0", "--sheet", str(base)]) == 0

    assert overlay_path(base).exists()


def test_prices_fetch_creates_the_output_directory(tmp_path, monkeypatch):
    """A first run on a clean machine must not fail because ~/.tamias is absent."""
    from tamias import cli
    from tamias.pricing_fetch import ParseResult

    monkeypatch.setattr(cli, "_models_opener", lambda: object())
    monkeypatch.setattr(cli, "fetch_models", lambda opener, url: ParseResult())
    output = tmp_path / "fresh" / "prices.toml"

    assert main(["prices", "fetch", "--out", str(output)]) == 0

    assert output.exists()
