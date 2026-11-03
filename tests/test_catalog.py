"""Tests for joining Zen's live model ids to models.dev capabilities.

Every fact here comes from a fixture.  The point of the module is that a
capability is written only when the catalogue publishes it, so these tests pin
the exclusions as tightly as the inclusions.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tamias.catalog import (
    MODELS_DEV_URL,
    build_zen_registry,
    fetch_catalog,
    render_registry,
)
from tamias.registry import load_registry

CATALOG = {
    "opencode": {
        "models": {
            "deepseek-v4.1-flash-free": {
                "tool_call": True,
                "reasoning": True,
                "limit": {"context": 200_000},
            },
            "text-only-free": {
                "tool_call": False,
                "reasoning": False,
                "limit": {"context": 100_000},
            },
            "no-limit-free": {
                "tool_call": True,
                "reasoning": False,
            },
        }
    },
    "other": {"models": {"deepseek-v4.1-flash-free": {"tool_call": True}}},
}


def test_build_keeps_only_published_tool_capable_models() -> None:
    build = build_zen_registry(
        CATALOG,
        [
            "deepseek-v4.1-flash-free",
            "text-only-free",
            "no-limit-free",
            "not-listed-free",
        ],
    )

    assert [model.id for model in build.models] == ["deepseek-v4.1-flash-free"]
    assert build.models[0].tools is True
    assert build.models[0].reasoning is True
    assert build.models[0].context_window == 200_000
    assert build.models[0].efforts == ()
    assert build.not_tool_capable == ("text-only-free",)
    assert build.missing_capability == ("no-limit-free", "not-listed-free")


def test_build_rejects_a_catalogue_without_the_provider() -> None:
    with pytest.raises(ValueError, match="no 'opencode' provider"):
        build_zen_registry({}, ["x-free"])


def test_build_rejects_a_provider_without_a_models_table() -> None:
    with pytest.raises(ValueError, match="no models table"):
        build_zen_registry({"opencode": {}}, ["x-free"])


def test_render_round_trips_through_the_registry_loader(tmp_path: Path) -> None:
    build = build_zen_registry(CATALOG, ["deepseek-v4.1-flash-free"])
    text = render_registry(
        build.models,
        "2026-10-30T12:00:00+00:00",
        "https://opencode.test/zen/v1/models",
        MODELS_DEV_URL,
    )
    path = tmp_path / "models.toml"
    path.write_text(text, encoding="utf-8")

    info = load_registry(path).get("deepseek-v4.1-flash-free")
    assert info is not None
    assert info.provider == "opencode"
    assert info.context_window == 200_000
    assert info.tools is True
    assert info.reasoning is True


class FakeOpener:
    """An opener that answers from a fixture instead of the network."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requested: list[str] = []

    def open(self, url: str) -> io.BytesIO:
        self.requested.append(url)
        return io.BytesIO(self.payload)


def test_fetch_catalog_reads_through_the_injected_opener() -> None:
    opener = FakeOpener(json.dumps(CATALOG).encode("utf-8"))

    catalog = fetch_catalog(opener)

    assert catalog["opencode"]["models"]["deepseek-v4.1-flash-free"]["tool_call"] is True
    assert opener.requested == [MODELS_DEV_URL]


def test_fetch_catalog_rejects_a_non_object_payload() -> None:
    opener = FakeOpener(b'["not", "an", "object"]')
    with pytest.raises(ValueError, match="not a JSON object"):
        fetch_catalog(opener)
