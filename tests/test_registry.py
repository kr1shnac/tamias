"""The model capability registry (D4).

Routing and model pickers must never guess what a model can do, so the registry
is strict: unknown keys, wrong types, unsupported efforts and inconsistent
combinations are all errors, not silent defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.registry import (
    KNOWN_EFFORTS,
    REGISTRY_ENV_VAR,
    ModelInfo,
    Registry,
    RegistryError,
    default_registry_path,
    empty_registry,
    load_registry,
    parse_registry,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """
[[models]]
id = "openai/gpt-5-mini"
provider = "openai"
context_window = 128000
tools = true
reasoning = true
efforts = ["low", "medium", "high"]

[[models]]
id = "nvidia/nemotron-3.5-lightning:free"
provider = "openrouter"
context_window = 32000
"""


# ---------- loading ----------

def test_loads_a_valid_catalogue(tmp_path: Path) -> None:
    registry = load_registry(_write(tmp_path / "models.toml", GOOD))

    assert len(registry) == 2
    assert registry.ids() == (
        "nvidia/nemotron-3.5-lightning:free",
        "openai/gpt-5-mini",
    )

    info = registry.require("openai/gpt-5-mini")
    assert info.provider == "openai"
    assert info.context_window == 128000
    assert info.tools is True
    assert info.reasoning is True
    assert info.efforts == ("low", "medium", "high")
    assert info.modality == "text"


def test_a_model_defaults_to_no_tools_no_reasoning(tmp_path: Path) -> None:
    registry = load_registry(_write(tmp_path / "models.toml", GOOD))
    info = registry.require("nvidia/nemotron-3.5-lightning:free")

    assert info.tools is False
    assert info.reasoning is False
    assert info.efforts == ()


def test_missing_default_registry_is_empty_not_an_error(tmp_path: Path) -> None:
    registry = load_registry(tmp_path / "absent.toml", missing_ok=True)
    assert len(registry) == 0


def test_explicit_missing_registry_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "absent.toml")


def test_malformed_toml_is_a_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path / "models.toml", "this is not toml ="))


def test_default_registry_path_honours_the_env_var(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(tmp_path / "custom.toml"))
    assert default_registry_path() == tmp_path / "custom.toml"


def test_default_registry_path_falls_back_to_home(monkeypatch) -> None:
    monkeypatch.delenv(REGISTRY_ENV_VAR, raising=False)
    assert default_registry_path().name == "models.toml"


def test_empty_registry_knows_nothing() -> None:
    registry = empty_registry()
    assert len(registry) == 0
    assert registry.get("anything") is None
    assert registry.supports_tools("anything") is False
    assert registry.efforts("anything") == ()


# ---------- validation ----------

def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry({"models": [], "surprise": 1})


def test_unknown_model_key_is_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry(
            {"models": [{"id": "m", "provider": "p", "context_window": 10, "cheap": True}]}
        )


@pytest.mark.parametrize("window", [0, -1, True, "1000", None])
def test_context_window_must_be_a_positive_integer(window: object) -> None:
    with pytest.raises(RegistryError):
        parse_registry(
            {"models": [{"id": "m", "provider": "p", "context_window": window}]}
        )


def test_efforts_without_reasoning_is_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry(
            {
                "models": [
                    {
                        "id": "m",
                        "provider": "p",
                        "context_window": 10,
                        "efforts": ["low"],
                    }
                ]
            }
        )


def test_unknown_effort_is_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry(
            {
                "models": [
                    {
                        "id": "m",
                        "provider": "p",
                        "context_window": 10,
                        "reasoning": True,
                        "efforts": ["ludicrous"],
                    }
                ]
            }
        )


def test_duplicate_effort_is_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry(
            {
                "models": [
                    {
                        "id": "m",
                        "provider": "p",
                        "context_window": 10,
                        "reasoning": True,
                        "efforts": ["low", "low"],
                    }
                ]
            }
        )


def test_duplicate_model_id_is_rejected() -> None:
    entry = {"id": "m", "provider": "p", "context_window": 10}
    with pytest.raises(RegistryError):
        parse_registry({"models": [entry, dict(entry)]})


def test_missing_id_and_provider_are_rejected() -> None:
    with pytest.raises(RegistryError):
        parse_registry({"models": [{"provider": "p", "context_window": 10}]})
    with pytest.raises(RegistryError):
        parse_registry({"models": [{"id": "m", "context_window": 10}]})


def test_require_names_the_unknown_model() -> None:
    registry = empty_registry()
    with pytest.raises(RegistryError, match="ghost"):
        registry.require("ghost")


# ---------- capability queries ----------

def test_supports_tools_and_reasoning() -> None:
    registry = empty_registry()
    registry = Registry(
        {
            "m": ModelInfo(
                id="m",
                provider="p",
                context_window=1000,
                tools=True,
                reasoning=True,
                efforts=("low", "high"),
            )
        }
    )

    assert registry.supports_tools("m") is True
    assert registry.supports_reasoning("m") is True
    assert registry.supports_tools("nope") is False


def test_resolve_effort_returns_the_requested_level_when_supported() -> None:
    registry = Registry({"m": ModelInfo("m", "p", 1000, reasoning=True, efforts=("low", "high"))})
    assert registry.resolve_effort("m", "high") == "high"


def test_resolve_effort_walks_down_to_the_nearest_supported_level() -> None:
    """A request never carries an effort the model rejects; downgrade, never up."""
    registry = Registry({"m": ModelInfo("m", "p", 1000, reasoning=True, efforts=("low",))})

    assert registry.resolve_effort("m", "high") == "low"
    assert registry.resolve_effort("m", "xhigh") == "low"


def test_resolve_effort_is_none_without_reasoning_support() -> None:
    registry = Registry({"m": ModelInfo("m", "p", 1000)})
    assert registry.resolve_effort("m", "high") is None
    assert registry.resolve_effort("ghost", "high") is None


def test_known_efforts_is_the_provider_vocabulary() -> None:
    assert set(KNOWN_EFFORTS) == {"none", "minimal", "low", "medium", "high", "xhigh"}
