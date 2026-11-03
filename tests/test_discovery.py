"""Discovery: read each provider's model list, cache it, never override the operator.

The claim these tests defend: a model a provider starts offering becomes routable
without a code change, an unreachable provider falls back to the last good
answer instead of emptying the pool, and a hand-written registry entry keeps its
tuned facts.
"""

from __future__ import annotations

import json

import httpx

from tamias.discovery import (
    ASSUMED_CONTEXT_WINDOW,
    DiscoveredModel,
    DiscoveryReport,
    fetch_models,
    merge_into,
    parse_models,
    read_cache,
    write_cache,
)
from tamias.providers import Credential, Provider
from tamias.registry import ModelInfo, Registry

OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "deepseek/deepseek-v4.1-flash",
            "context_length": 1_048_576,
            "supported_parameters": ["tools", "reasoning", "temperature"],
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {
            "id": "nvidia/nemotron-3.5-lightning:free",
            "context_length": 1_000_000,
            "supported_parameters": ["tools"],
        },
    ]
}


def test_openrouter_shaped_entries_yield_real_capabilities():
    models = parse_models("openrouter", OPENROUTER_PAYLOAD)

    assert [model.id for model in models] == [
        "deepseek/deepseek-v4.1-flash",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    deepseek, nemotron = models
    assert deepseek.context_window == 1_048_576
    assert deepseek.tools is True and deepseek.reasoning is True
    assert deepseek.modality == "image"
    assert nemotron.reasoning is False
    assert models[0].assumed == ()


def test_an_id_only_provider_still_yields_routable_models_with_assumptions():
    models = parse_models("nvidia", {"data": [{"id": "meta/llama-3.1-8b"}]})

    info = models[0].as_model_info()

    assert info.tools is True, "a tool-less model could never carry agent traffic"
    assert info.context_window == ASSUMED_CONTEXT_WINDOW
    assert info.reasoning is False, "an unstated effort must not be pretended"
    assert set(models[0].assumed) == {"tools", "context_window", "reasoning"}


def test_a_bare_list_and_a_models_key_are_both_understood():
    assert len(parse_models("p", [{"id": "a"}])) == 1
    assert len(parse_models("p", {"models": [{"id": "a"}]})) == 1
    assert parse_models("p", {"unexpected": True}) == []


def test_duplicate_and_malformed_entries_are_dropped():
    models = parse_models("p", {"data": [{"id": "a"}, {"id": "a"}, {"nope": 1}, "string"]})

    assert [model.id for model in models] == ["a"]


def test_merge_keeps_the_hand_written_entry_and_its_quality():
    registry = Registry(
        {
            "known": ModelInfo(
                id="known",
                provider="openrouter",
                context_window=8_000,
                tools=True,
                quality=91.0,
            )
        }
    )
    report = DiscoveryReport(
        found={
            "openrouter": [
                DiscoveredModel(id="known", provider="openrouter", context_window=999_999),
                DiscoveredModel(id="brand-new", provider="openrouter", context_window=64_000),
            ]
        }
    )

    merged = merge_into(registry, report)

    assert merged.get("known").context_window == 8_000
    assert merged.get("known").quality == 91.0
    assert merged.get("brand-new").context_window == 64_000


def test_fetch_models_uses_the_provider_root_and_its_credential():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OPENROUTER_PAYLOAD)

    provider = Provider(name="openrouter", base_url="https://openrouter.ai/api", key_source="env:A")
    models = fetch_models(
        provider,
        credential=Credential("secret"),
        transport=httpx.MockTransport(handler),
    )

    assert len(models) == 2
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/models"
    assert seen[0].headers["authorization"] == "Bearer secret"


def test_fetch_models_tries_the_bare_path_when_the_versioned_one_is_missing():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    provider = Provider(name="nvidia", base_url="https://integrate.api.nvidia.com/v1")
    models = fetch_models(provider, transport=httpx.MockTransport(handler))

    assert paths == ["/v1/models", "/models"]
    assert models[0].id == "m"


def test_fetch_models_never_raises_on_a_broken_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    provider = Provider(name="p", base_url="https://p.invalid")

    assert fetch_models(provider, transport=httpx.MockTransport(handler)) == []


def test_the_cache_round_trips_through_disk(tmp_path):
    path = tmp_path / "models.cache.json"
    report = DiscoveryReport(found={"openrouter": parse_models("openrouter", OPENROUTER_PAYLOAD)})

    write_cache(path, report.found)
    restored = read_cache(path)

    assert [model.id for model in restored["openrouter"]] == [
        "deepseek/deepseek-v4.1-flash",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    assert restored["openrouter"][0].context_window == 1_048_576


def test_an_unreadable_cache_is_treated_as_empty(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_cache(path) == {}
    assert read_cache(tmp_path / "absent.json") == {}


def test_the_cache_file_is_private(tmp_path):
    path = tmp_path / "models.cache.json"

    write_cache(path, {"p": [DiscoveredModel(id="m", provider="p")]})

    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["providers"]["p"][0]["id"] == "m"
