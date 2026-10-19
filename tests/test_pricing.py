"""Tests for price sheet loading and cost arithmetic."""

from __future__ import annotations

import importlib.resources
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias.pricing import ModelPrice, PriceSheet, compute_cost, load_price_sheet  # noqa: E402
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


def _cw_sheet(tmp_path: Path) -> PriceSheet:
    """A sheet whose model charges for cache writes (P0-F)."""
    data = """date="2026-10-04"
[strong]
input=3.0
output=15.0
cached_input=0.30
cache_write=3.75
"""
    path = tmp_path / "cw.toml"
    path.write_text(data)
    return load_price_sheet(path)


def test_cache_write_counts_are_priced_when_reported(tmp_path: Path):
    """A chat row that reports write counts prices them at the cw rate."""
    sheet = _cw_sheet(tmp_path)
    c = compute_cost("strong", Usage(1_000_000, 0, 800_000, 100_000), sheet)
    assert c.usd == pytest.approx(0.915)


def test_cache_write_counts_omitted_on_a_cw_model_price_to_unknown(tmp_path: Path):
    """Omitting write counts on a cw-nonzero model is UNKNOWN, not free."""
    sheet = _cw_sheet(tmp_path)
    c = compute_cost("strong", Usage(1_000_000, 0, 800_000, None), sheet)
    assert c.usd is None
    assert "unknown: cache_write_tokens" in c.formula


def test_openai_style_usage_prices_against_the_example_sheet():
    """An unreported cache-write count must not make a cost UNKNOWN by itself.

    The OpenAI chat-completions path sets cache_write_tokens=None when the
    upstream never itemizes writes, so the shipped example sheet gives those
    models cache_write = 0 and their requests still get a number.
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


PROVENANCE_SHEET = """date = "2026-10-06"
simulated = false

[provenance]
source = "openrouter-models-api"
url = "https://openrouter.ai/api/v1/models"
fetched_at = "2026-10-06T12:00:00Z"

[gpt-4o]
input = 3.0
output = 15.0
"""


def test_loader_reads_the_provenance_block(tmp_path):
    """A fetched sheet carries where its rates came from and when."""
    path = tmp_path / "prices.toml"
    path.write_text(PROVENANCE_SHEET, encoding="utf-8")

    sheet = load_price_sheet(path)
    assert sheet.provenance == {
        "source": "openrouter-models-api",
        "url": "https://openrouter.ai/api/v1/models",
        "fetched_at": "2026-10-06T12:00:00Z",
    }
    assert sheet.simulated is False
    assert sheet.get("gpt-4o") == ModelPrice(input=3.0, output=15.0)


def test_a_sheet_without_provenance_still_loads(tmp_path):
    """Provenance is optional: every sheet written before it existed still reads."""
    path = tmp_path / "prices.toml"
    path.write_text('date = "2026-10-04"\n\n[gpt-4o]\ninput = 3.0\noutput = 15.0\n', "utf-8")
    assert load_price_sheet(path).provenance is None


def test_an_unknown_provenance_key_is_rejected(tmp_path):
    """The schema is fixed: a key nobody understands is an error, not a claim."""
    path = tmp_path / "prices.toml"
    path.write_text(
        PROVENANCE_SHEET.replace('url = "https://openrouter.ai/api/v1/models"', 'vendor = "x"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="provenance"):
        load_price_sheet(path)


def test_provenance_values_must_be_strings(tmp_path):
    """Provenance is metadata a report quotes; a number there is not a source."""
    path = tmp_path / "prices.toml"
    path.write_text(
        PROVENANCE_SHEET.replace('source = "openrouter-models-api"', "source = 1"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="provenance"):
        load_price_sheet(path)


def test_provenance_must_be_a_table(tmp_path):
    """`provenance` is a table of strings, never a bare scalar."""
    path = tmp_path / "prices.toml"
    path.write_text('date = "2026-10-06"\nprovenance = 3\n\n[gpt-4o]\ninput = 3.0\n', "utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_price_sheet(path)


# --- what a fetched sheet may and may not claim --------------------------------

RULES_SHEET = """date = "2026-10-04"

[strong]
input = 3.0
output = 15.0
cached_input = 0.30
cache_write = 3.75
cache_write_1h = 6.0

[no-cache-rates]
input = 3.0
output = 15.0
cache_write = 3.75

[no-write-rate]
input = 3.0
output = 15.0
cached_input = 0.30

[tiered]
input = 3.0
output = 15.0
tier_200k_input = 6.0
tier_200k_output = 30.0

[tiered-free]
input = 0.0
output = 0.0
cached_input = 0.0
cache_write = 0.0
tier_200k_input = 0.0

[free-no-cache]
input = 0.0
output = 0.0
"""


def rules_sheet(tmp_path) -> PriceSheet:
    path = tmp_path / "prices.toml"
    path.write_text(RULES_SHEET, encoding="utf-8")
    return load_price_sheet(path)


def test_provider_billed_cost_is_never_folded_into_the_sheet_arithmetic(tmp_path):
    """Provider money is a measurement, not a substitute for list-price math.

    ``usd`` is always the computed list price; the provider's billed amount is
    carried separately in ``Usage.provider_cost_usd`` and stored in its own
    column.  The sheet sum stands on its own and nothing rescues or replaces it.
    """
    sheet = rules_sheet(tmp_path)
    usage = Usage(1_000_000, 100_000, 0, 0, provider_cost_usd=0.42)
    breakdown = compute_cost("strong", usage, sheet)
    assert breakdown.usd == pytest.approx(4.5)
    assert breakdown.source == "computed"
    assert "billed" not in breakdown.formula
    assert usage.provider_cost_usd == 0.42
    assert compute_cost("strong", Usage(1_000_000, 100_000, 0, 0), sheet).usd == pytest.approx(4.5)


def test_provider_money_does_not_fill_an_unknown_model(tmp_path):
    """A bill for an unpriced model does not make its list price computable."""
    sheet = rules_sheet(tmp_path)
    usage = Usage(1_000_000, 100_000, None, None, provider_cost_usd=0.001)
    breakdown = compute_cost("a-model-nobody-priced", usage, sheet)
    assert breakdown.usd is None
    assert breakdown.source == "unknown"
    assert "a-model-nobody-priced" in breakdown.formula


def test_provider_billed_zero_cannot_mask_an_uncomputable_cost(tmp_path):
    """A real zero-dollar bill and a missing list price stay distinguishable."""
    sheet = rules_sheet(tmp_path)
    priced = compute_cost(
        "strong", Usage(1_000_000, 100_000, 0, 0, provider_cost_usd=0.0), sheet
    )
    assert priced.usd == pytest.approx(4.5)
    assert priced.source == "computed"
    unknown = compute_cost(
        "a-model-nobody-priced",
        Usage(10_000, 2_000, None, None, provider_cost_usd=0.0),
        sheet,
    )
    assert unknown.usd is None
    assert unknown.source == "unknown"


def test_a_tiered_model_stays_unknown_even_with_provider_money(tmp_path):
    """Sheet arithmetic does not quote unsupported tiers, bill or no bill."""
    sheet = rules_sheet(tmp_path)
    usage = Usage(10_000, 2_000, 0, 0, provider_cost_usd=0.5)
    breakdown = compute_cost("tiered", usage, sheet)
    assert breakdown.usd is None
    assert breakdown.source == "unknown"
    assert usage.provider_cost_usd == 0.5


def test_without_a_provider_price_the_exact_sheet_sum_is_used(tmp_path):
    """The fallback arithmetic is the documented sum, to the cent."""
    sheet = rules_sheet(tmp_path)
    usage = Usage(1_000_000, 100_000, 0, 0)
    breakdown = compute_cost("strong", usage, sheet)
    assert breakdown.usd == pytest.approx(4.5)
    assert "usd = (" in breakdown.formula


def test_a_missing_cached_rate_for_nonzero_cached_tokens_is_unknown(tmp_path):
    """Cache reads are never guessed at: no rate, no number."""
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("no-cache-rates", Usage(10_000, 2_000, 4_000, 0), sheet)
    assert breakdown.usd is None
    assert "cached_input_rate" in breakdown.formula


def test_a_missing_write_rate_for_nonzero_write_tokens_is_unknown(tmp_path):
    """Cache writes are the same bargain: the field is named, the cost is not."""
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("no-write-rate", Usage(10_000, 2_000, 0, 1_000), sheet)
    assert breakdown.usd is None
    assert "cache_write_rate" in breakdown.formula


def test_a_missing_cache_rate_costs_nothing_when_the_count_is_zero(tmp_path):
    """A rate nobody quoted only matters if tokens actually reached it."""
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("no-cache-rates", Usage(10_000, 2_000, 0, 0), sheet)
    # 10_000 input * 3.0 + 2_000 output * 15.0 = 60_000 / 1_000_000
    assert breakdown.usd == pytest.approx(0.06)


def test_cache_reads_are_priced_at_the_cache_rate_not_the_input_rate(tmp_path):
    """A discounted cache read must never be billed at the plain input price."""
    sheet = rules_sheet(tmp_path)
    usage = Usage(1_000_000, 0, 1_000_000, 0)
    breakdown = compute_cost("strong", usage, sheet)
    assert breakdown.usd == pytest.approx(0.3)  # 1M * 0.30, not 1M * 3.0


def test_cache_writes_are_priced_at_the_write_rate_not_the_input_rate(tmp_path):
    """Writes carry their own rate: 100k * 3.75 on top of 900k * 3.0."""
    sheet = rules_sheet(tmp_path)
    usage = Usage(1_000_000, 0, 0, 100_000)
    breakdown = compute_cost("strong", usage, sheet)
    assert breakdown.usd == pytest.approx(3.075)  # 2.70 + 0.375, never 3.00


def test_reasoning_tokens_are_counted_once_inside_completion_tokens(tmp_path):
    """Reasoning is part of the output count, so the formula adds it once."""
    sheet = rules_sheet(tmp_path)
    # output_tokens = 1_000_000 already includes any reasoning tokens the
    # upstream reported inside the completion.
    usage = Usage(1_000_000, 1_000_000, 0, 0)
    breakdown = compute_cost("strong", usage, sheet)
    assert breakdown.usd == pytest.approx(18.0)  # 3.0 input + 15.0 output
    assert "reasoning" not in breakdown.formula
    assert breakdown.formula.count("output") == 1


def test_a_tier_field_makes_the_cost_unknown_and_names_it(tmp_path):
    """Tiered long-context pricing is unsupported: UNKNOWN, never a guess."""
    sheet = rules_sheet(tmp_path)
    assert sheet.get("tiered").tiers == ("tier_200k_input", "tier_200k_output")
    breakdown = compute_cost("tiered", Usage(10_000, 2_000, 0, 0), sheet)
    assert breakdown.usd is None
    assert "tier_200k_input" in breakdown.formula
    assert "tier" in breakdown.formula


def test_a_tiered_model_is_unknown_even_when_every_rate_is_zero(tmp_path):
    """The free-model shortcut does not rescue an entry with tier fields."""
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("tiered-free", Usage(None, None, None, None), sheet)
    assert breakdown.usd is None
    assert "tier_200k_input" in breakdown.formula


def test_a_free_model_with_unquoted_cache_rate_is_unknown_on_cache_use(tmp_path):
    """Rule 1b outranks the free-model shortcut: an unquoted bucket in use is not $0.

    This is B3: a model that quotes zero base rates but never quotes a cache
    rate costs US$0.00 today even when the upstream reports hundreds of
    thousands of cached-input tokens.  Zero is a claim those hanging tokens are
    free, so when the bucket is actually touched the cost is UNKNOWN instead.
    """
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("free-no-cache", Usage(10_000, 2_000, 5_000, 0), sheet)
    assert breakdown.usd is None
    assert breakdown.source == "unknown"
    assert "cached_input_rate" in breakdown.formula


def test_a_free_model_with_unquoted_cache_rate_is_zero_when_the_bucket_is_empty(tmp_path):
    """No tokens reached the unquoted bucket, so a free model costs exactly 0."""
    sheet = rules_sheet(tmp_path)
    breakdown = compute_cost("free-no-cache", Usage(10_000, 2_000, 0, 0), sheet)
    assert breakdown.usd == 0.0
    assert breakdown.source == "computed"
    # An unreported count also touches the bucket with nothing: still free.
    untouched = compute_cost("free-no-cache", Usage(10_000, 2_000, None, None), sheet)
    assert untouched.usd == 0.0
    assert untouched.source == "computed"


def test_a_rate_key_that_is_not_a_tier_field_is_still_rejected(tmp_path):
    """Only names beginning with `tier` are tolerated; typos stay errors."""
    path = tmp_path / "prices.toml"
    path.write_text(
        'date = "2026-10-04"\n\n[m]\ninput = 3.0\noutput = 15.0\ninput_200k = 6.0\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown rate"):
        load_price_sheet(path)


@pytest.mark.parametrize(
    "table",
    [
        "[m]\ninput = -1.0\noutput = 15.0\n",
        "[m]\ninput = 3.0\noutput = inf\n",
        "[m]\ninput = 3.0\noutput = -inf\n",
        "[m]\ninput = nan\noutput = 15.0\n",
        "[m]\ninput = 3.0\noutput = 15.0\ncached_input = -0.5\n",
        "[m]\ninput = 3.0\noutput = 15.0\ncache_write = nan\n",
    ],
    ids=[
        "negative-input",
        "positive-inf",
        "negative-inf",
        "nan-input",
        "negative-cache",
        "nan-write",
    ],
)
def test_rates_must_be_finite_and_non_negative(tmp_path, table) -> None:
    """A rate the arithmetic cannot trust is refused at load time, never later.

    Negative would fabricate a credit, and NaN or +/-inf would poison every sum
    it touched.  The loader rejects them up front so no price sitting in the
    sheet can silently corrupt the audit trail.
    """
    path = tmp_path / "prices.toml"
    path.write_text(f'date = "2026-10-04"\n\n{table}', encoding="utf-8")
    with pytest.raises(ValueError, match="finite non-negative"):
        load_price_sheet(path)


def test_a_fully_specified_all_zero_model_still_returns_zero(tmp_path):
    """Every bucket quoted at 0 is genuinely free: $0.00, exactly, not UNKNOWN."""
    path = tmp_path / "prices.toml"
    path.write_text(
        'date = "2026-10-04"\n\n[free]\ninput = 0.0\noutput = 0.0\n'
        "cached_input = 0.0\ncache_write = 0.0\n",
        encoding="utf-8",
    )
    sheet = load_price_sheet(path)
    breakdown = compute_cost("free", Usage(10_000, 2_000, 3_000, 500), sheet)
    assert breakdown.usd == 0.0
    assert breakdown.source == "computed"


def test_the_bundled_default_sheet_is_present_and_prices_above_zero() -> None:
    """F-20: a fresh install must be able to price a request above $0.00.

    The sheet shipped inside the package is a real OpenRouter snapshot, not the
    free-model demo sheet, so it is what stops a first run from printing $0.00
    and looking like a broken proxy.
    """
    resource = importlib.resources.files("tamias").joinpath("data", "prices.default.toml")
    sheet = load_price_sheet(str(resource))
    assert sheet.simulated is False
    assert sheet.provenance is not None
    assert sheet.provenance["source"] == "openrouter-models-api"
    assert sheet.models, "the bundled sheet must contain models"
    assert any(
        price.input is not None and price.input > 0 for price in sheet.models.values()
    ), "the bundled sheet must price at least one model above zero (F-20)"
