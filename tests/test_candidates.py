"""The three routing gates: capability, supply, health -- then cheapest wins.

The claim these tests defend: a model can only be chosen after it has separately
proved it can do the work, that tamias can reach and price it, and that no
provider has locked it out.  Among the survivors the cheapest wins, so spend
falls; the quality floor is what stops that from buying a worse answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from tamias.candidates import (
    CAPABILITY,
    HEALTH,
    SUPPLY,
    CandidatePool,
    Needs,
    blended_cost,
    build_pool,
    needs_from_body,
    quality_of,
)
from tamias.limits import LimitLedger
from tamias.pricing import ModelPrice, PriceSheet
from tamias.providers import Credential, Provider, ProviderCatalogue
from tamias.registry import ModelInfo, Registry


def model(
    model_id: str,
    *,
    provider: str = "openrouter",
    tools: bool = True,
    reasoning: bool = False,
    context_window: int = 128_000,
    modality: str = "text",
    quality: float | None = None,
) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider=provider,
        context_window=context_window,
        tools=tools,
        reasoning=reasoning,
        efforts=("low", "high") if reasoning else (),
        modality=modality,
        quality=quality,
    )


def registry(*models: ModelInfo) -> Registry:
    return Registry({info.id: info for info in models})


def sheet(**prices: tuple[float, float]) -> PriceSheet:
    return PriceSheet(
        date="2026-11-01",
        models={
            name: ModelPrice(input=rates[0], output=rates[1]) for name, rates in prices.items()
        },
    )


def catalogue(*providers: Provider, names: dict[str, str] | None = None) -> ProviderCatalogue:
    return ProviderCatalogue(
        providers={provider.name: provider for provider in providers},
        credentials={
            provider.name: Credential("key") for provider in providers if provider.configured
        },
        model_providers=names or {},
    )


OPENROUTER = Provider(name="openrouter", base_url="https://openrouter.ai/api", key_source="env:A")


def test_capability_gate_refuses_a_model_that_cannot_take_tools():
    pool = build_pool(
        registry=registry(model("no-tools", tools=False), model("tools")),
        needs=Needs(tools=True),
    )

    assert pool.ids() == ("tools",)
    assert pool.rejected == (pool.rejected[0],)
    assert pool.rejected[0].gate == CAPABILITY
    assert pool.rejected[0].reason == "no tool calls"


def test_capability_gate_refuses_a_window_too_small_for_the_messages():
    pool = build_pool(
        registry=registry(model("small", context_window=1_000), model("big")),
        needs=Needs(tokens=50_000),
    )

    assert pool.ids() == ("big",)
    assert "window 1000 < 50000" in pool.rejected[0].reason


def test_capability_gate_refuses_a_model_without_the_requested_modality():
    pool = build_pool(
        registry=registry(model("text-only"), model("vision", modality="image")),
        needs=Needs(modality="image"),
    )

    assert pool.ids() == ("vision",)


def test_supply_gate_refuses_a_provider_without_a_credential():
    unconfigured = Provider(
        name="nvidia", base_url="https://integrate.api.nvidia.com", key_source=""
    )
    pool = build_pool(
        registry=registry(model("a/model", provider="nvidia")),
        catalogue=catalogue(unconfigured),
    )

    assert pool.ids() == ()
    assert pool.rejected[0].gate == SUPPLY
    assert "no credential" in pool.rejected[0].reason


def test_supply_gate_refuses_an_unpriced_model_only_when_accounting_requires_it():
    unpriceable = model("mystery")
    prices = sheet()

    lenient = build_pool(registry=registry(unpriceable), sheet=prices)
    strict = build_pool(registry=registry(unpriceable), sheet=prices, require_priced=True)

    assert lenient.ids() == ("mystery",)
    assert strict.ids() == ()
    assert strict.rejected[0].reason == "unpriced"


def test_health_gate_refuses_a_model_the_ledger_locked_out():
    ledger = LimitLedger(":memory:")
    ledger.record("openrouter", "cheap", 429)

    pool = build_pool(
        registry=registry(model("cheap"), model("other")),
        limits=ledger,
    )

    assert pool.ids() == ("other",)
    assert pool.rejected[0].gate == HEALTH


def test_health_gate_refuses_every_model_of_a_rejected_provider():
    ledger = LimitLedger(":memory:")
    ledger.record("openrouter", "cheap", 401)

    pool = build_pool(registry=registry(model("cheap"), model("other")), limits=ledger)

    assert pool.ids() == ()
    assert {rejection.gate for rejection in pool.rejected} == {HEALTH}


def test_the_cheapest_capable_model_wins_even_when_it_is_the_weakest():
    pool = build_pool(
        registry=registry(model("free", quality=40), model("paid", quality=95)),
        sheet=sheet(free=(0.0, 0.0), paid=(10.0, 30.0)),
    )

    assert pool.best().model == "free"


def test_the_quality_floor_keeps_a_hard_step_off_the_cheapest_model():
    pool = build_pool(
        registry=registry(model("free", quality=40), model("paid", quality=95)),
        sheet=sheet(free=(0.0, 0.0), paid=(10.0, 30.0)),
    )

    assert pool.best(min_quality=90).model == "paid"


def test_no_candidate_meets_a_floor_that_nothing_satisfies():
    pool = build_pool(registry=registry(model("free", quality=40)))

    assert pool.best(min_quality=90) is None


def test_an_unpriced_model_ranks_behind_every_priced_one():
    pool = build_pool(
        registry=registry(model("mystery"), model("dear")),
        sheet=sheet(dear=(100.0, 100.0)),
    )

    assert pool.ids() == ("dear", "mystery")


def test_equal_cost_is_broken_by_quality_then_by_id():
    pool = build_pool(
        registry=registry(
            model("b", quality=50), model("a", quality=50), model("strong", quality=90)
        ),
        sheet=sheet(a=(1.0, 1.0), b=(1.0, 1.0), strong=(1.0, 1.0)),
    )

    assert pool.ids() == ("strong", "a", "b")


def test_excluding_the_model_that_just_failed_yields_the_next_one():
    pool = build_pool(
        registry=registry(model("first"), model("second")),
        sheet=sheet(first=(1.0, 1.0), second=(2.0, 2.0)),
    )

    assert pool.best(exclude={"first"}).model == "second"
    assert pool.best(exclude={"first", "second"}) is None


def test_the_pool_explains_itself_by_gate():
    pool = build_pool(
        registry=registry(
            model("good"),
            model("no-tools", tools=False),
            model("no-window", context_window=100),
        ),
        needs=Needs(tools=True, tokens=10_000),
    )

    assert pool.reason() == "1 capable; refused 2 capability"
    assert pool.considered == 3


def test_blended_cost_weights_output_higher_than_input():
    assert blended_cost(ModelPrice(input=1.0, output=5.0)) == pytest.approx(2.0)
    assert blended_cost(None) is None
    assert blended_cost(ModelPrice(input=None, output=5.0)) is None


def test_declared_quality_wins_over_the_derived_score():
    declared = model("m", quality=88)
    derived = model("m2", reasoning=True, tools=True, context_window=400_000)

    assert quality_of(declared) == 88
    assert quality_of(derived) > 50


def body(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "m",
        "messages": [{"role": "user", "content": "x" * 400}],
    }
    base.update(extra)
    return base


def test_needs_read_tools_effort_and_size_from_the_request():
    needs = needs_from_body(
        body(
            tools=[{"type": "function", "function": {"name": "read"}}],
            reasoning={"effort": "high"},
        )
    )

    assert needs.tools is True
    assert needs.effort == "high"
    assert needs.tokens == 100


def test_needs_detect_an_image_part():
    needs = needs_from_body(
        body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    ],
                }
            ]
        )
    )

    assert needs.modality == "image"
    assert needs.tokens == 3


def test_an_empty_registry_yields_an_empty_pool_not_an_error():
    pool = build_pool(registry=registry())

    assert pool == CandidatePool(candidates=())
    assert not pool
