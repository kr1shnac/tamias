"""Tests for price sheet loading and cost arithmetic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias.pricing import ModelPrice, compute_cost, load_price_sheet  # noqa: E402
from tamias.types import Usage  # noqa: E402


def test_cases():
    sheet_data = """date="2026-10-04"
[strong]
input=3.0
output=15.0
cached_input=0.30
cache_write=3.75
cache_write_1h=6.0
[oa]
input=1.0
output=4.0
cached_input=0.25
cache_write=0
[free-model]
input=0.0
output=0.0
cached_input=0.0
cache_write=0.0
"""
    import tempfile

    f = Path(tempfile.mktemp(suffix=".toml"))
    f.write_text(sheet_data)
    sheet = load_price_sheet(f)

    # 1) strong in=1_000_000 out=100_000 cached=0 write=0 -> 4.5
    c = compute_cost("strong", Usage(1_000_000, 100_000, 0, 0), sheet)
    assert c.usd == pytest.approx(4.5)

    # 2) strong in=1_000_000 out=0 cached=800_000 write=100_000 1h=None -> 0.915
    c = compute_cost("strong", Usage(1_000_000, 0, 800_000, 100_000), sheet)
    assert c.usd == pytest.approx(0.915)

    # 3) same as 2 with 1h=40_000 -> 1.005
    c = compute_cost(
        "strong", Usage(1_000_000, 0, 800_000, 100_000, cache_write_1h_tokens=40000), sheet
    )
    assert c.usd == pytest.approx(1.005)

    # 4) oa in=10_000 out=2_000 cached=4_000 write=None -> 0.015
    c = compute_cost("oa", Usage(10_000, 2_000, 4_000, None), sheet)
    assert c.usd == pytest.approx(0.015)

    # 5) oa in=10_000 out=2_000 cached=None -> usd None
    c = compute_cost("oa", Usage(10_000, 2_000, None, None), sheet)
    assert c.usd is None

    # 6) strong with out=None -> usd None
    c = compute_cost("strong", Usage(1_000_000, None, 0, 0), sheet)
    assert c.usd is None

    # 7) unknown model -> usd None
    c = compute_cost("unknown", Usage(1, 1, 1, 1), sheet)
    assert c.usd is None

    # 8) free-model with all usage None -> 0.0
    c = compute_cost("free-model", Usage(None, None, None, None), sheet)
    assert c.usd == 0.0

    # 9) strong cached=2_000_000 > in=1_000_000 -> usd None, formula contains "inconsistent"
    c = compute_cost("strong", Usage(1_000_000, 0, 2_000_000, 0), sheet)
    assert c.usd is None
    assert "inconsistent" in c.formula


def test_openai_style_usage_prices_against_the_example_sheet():
    """An unreported cache-write count must not make a cost UNKNOWN by itself.

    proxy.to_usage always sets cache_write_tokens=None for the OpenAI
    chat-completions path, so the shipped example sheet gives those models
    cache_write = 0 and their requests still get a number.
    """
    sheet = load_price_sheet(Path(__file__).resolve().parents[1] / "prices.example.toml")

    for model in ("gpt-4o", "oa"):
        assert sheet.get(model).cache_write == 0

    c = compute_cost("gpt-4o", Usage(10_000, 2_000, 4_000, None), sheet)
    assert c.usd is not None
    assert c.usd == pytest.approx(0.0492)


def test_openai_style_usage_prices_against_the_simulated_sheet():
    """The simulated sheet prices OpenAI-shaped traffic too.

    It gives its models cache_write = 0 for the same reason the example sheet
    does, and simulated = true changes no arithmetic.
    """
    sheet = load_price_sheet(Path(__file__).resolve().parents[1] / "prices.simulated.toml")
    assert sheet.simulated is True

    openai_shaped = Usage(10_000, 2_000, 4_000, None)
    for model in ("strong", "cheap"):
        assert sheet.get(model).cache_write == 0
        assert compute_cost(model, openai_shaped, sheet).usd is not None


def test_the_shipped_openrouter_sheet_prices_its_free_models_at_zero():
    """OpenRouter's free models bill at zero, so every rate is a real 0.

    The ids contain '/' and ':', so the tables are quoted; loading the sheet is
    what proves the quoted keys survive.  cache_write = 0 is load-bearing:
    OpenRouter is an OpenAI-style upstream that reports no cache-write count.
    """
    root = Path(__file__).resolve().parents[1]
    sheet = load_price_sheet(root / "prices.openrouter.toml")
    assert sheet.simulated is False
    assert set(sheet.models) == {
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "poolside/laguna-s-2.1:free",
    }

    for model in sheet.models:
        price = sheet.get(model)
        assert (price.input, price.output, price.cached_input) == (0.0, 0.0, 0.0)
        assert price.cache_write == 0
        # A free model costs exactly nothing even with no usage reported at all.
        assert compute_cost(model, Usage(None, None, None, None), sheet).usd == 0.0


def test_the_shipped_openrouter_simulated_sheet_is_simulated_with_invented_prices():
    """The simulated OpenRouter sheet must label itself, and must not be free.

    Its numbers are made up on purpose: if they ever read as 0, the file stops
    being a rehearsal and becomes a claim that the models are free.

    It is keyed by the real OpenRouter model ids, quoted because of the `/` and
    `:`.  Keying by the real ids is what makes the rehearsal usable: the ids a
    live session logs are then the ids this sheet has invented rates for, so a
    report over a real log finds prices instead of UNKNOWN.
    """
    root = Path(__file__).resolve().parents[1]
    sheet = load_price_sheet(root / "prices.openrouter-sim.toml")
    ultra = "nvidia/nemotron-3-ultra-550b-a55b:free"
    lightning = "nvidia/nemotron-3.5-lightning:free"
    assert sheet.simulated is True
    assert set(sheet.models) == {ultra, lightning}
    assert sheet.get(ultra) == ModelPrice(
        input=3.0, output=15.0, cached_input=0.30, cache_write=0.0, cache_write_1h=0.0
    )
    assert sheet.get(lightning) == ModelPrice(
        input=0.25, output=1.25, cached_input=0.03, cache_write=0.0, cache_write_1h=0.0
    )

    usage = Usage(10_000, 2_000, 0, None)
    assert compute_cost(ultra, usage, sheet).usd == pytest.approx(0.06)
    assert compute_cost(lightning, usage, sheet).usd < compute_cost(ultra, usage, sheet).usd

    # OpenAI-shaped usage reports no cache-write count.  cache_write = 0 keeps
    # that from making the rehearsal UNKNOWN.
    for model in (ultra, lightning):
        assert sheet.get(model).cache_write == 0
        assert compute_cost(model, usage, sheet).usd is not None
