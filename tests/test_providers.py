"""The provider catalogue: routing a model to a provider, without storing secrets.

The claim these tests defend: tamias can reach any number of providers at once,
each with its own credential, and a credential can never be printed, logged or
forwarded to the wrong provider.  A provider whose secret is missing is
*unconfigured* -- skipped, not attempted.
"""

from __future__ import annotations

import pytest

from tamias.providers import (
    Credential,
    ProviderError,
    catalogue_from_registry,
    empty_catalogue,
    load_providers,
)
from tamias.registry import ModelInfo, Registry


def write(tmp_path, body: str):
    path = tmp_path / "providers.toml"
    path.write_text(body, encoding="utf-8")
    return path


TWO_PROVIDERS = """
[[providers]]
name = "openrouter"
api_key_env = "OPENROUTER_API_KEY"
models = ["deepseek/deepseek-v4.1-flash"]

[[providers]]
name = "nvidia"
api_key_env = "NVIDIA_API_KEY"
models = ["meta/llama-3.1-8b"]
"""


def test_two_providers_resolve_from_their_own_environment_variable(tmp_path):
    catalogue = load_providers(
        write(tmp_path, TWO_PROVIDERS),
        env={"OPENROUTER_API_KEY": "or-secret", "NVIDIA_API_KEY": "nv-secret"},
    )

    assert catalogue.names() == ("nvidia", "openrouter")
    assert catalogue.provider_for("deepseek/deepseek-v4.1-flash").name == "openrouter"
    assert catalogue.provider_for("meta/llama-3.1-8b").name == "nvidia"
    assert catalogue.unconfigured() == ()


def test_a_missing_secret_marks_the_provider_unconfigured_and_skipped(tmp_path):
    catalogue = load_providers(
        write(tmp_path, TWO_PROVIDERS), env={"OPENROUTER_API_KEY": "or-secret"}
    )

    assert catalogue.unconfigured() == ("nvidia",)
    nvidia = catalogue.get("nvidia")
    assert nvidia is not None and not nvidia.configured


def test_credential_never_appears_in_repr_or_str():
    credential = Credential("super-secret-value")

    assert "super-secret-value" not in repr(credential)
    assert "super-secret-value" not in str(credential)
    assert credential.header() == "Bearer super-secret-value"


def test_a_credential_command_is_run_once_at_load_and_not_stored(tmp_path):
    calls: list[str] = []

    def runner(command: str) -> str:
        calls.append(command)
        return "from-the-keychain\n"

    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "nvidia"
api_key_command = "pass show nvidia/key"
""",
        ),
        env={},
        runner=runner,
    )

    assert calls == ["pass show nvidia/key"]
    assert catalogue.get("nvidia").configured
    assert catalogue.get("nvidia").key_source == "command"
    assert catalogue.credential("nvidia").header() == "Bearer from-the-keychain"


def test_a_failing_command_leaves_the_provider_unconfigured(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "nvidia"
api_key_command = "false"
""",
        ),
        env={},
        runner=lambda command: "",
    )

    assert catalogue.unconfigured() == ("nvidia",)


def test_the_environment_wins_over_the_command_when_both_are_given(tmp_path):
    calls: list[str] = []
    path = write(
        tmp_path,
        """
[[providers]]
name = "nvidia"
api_key_env = "NVIDIA_API_KEY"
api_key_command = "pass show nvidia/key"
""",
    )

    def runner(command: str) -> str:
        calls.append(command)
        return "cmd-key"

    from_env = load_providers(path, env={"NVIDIA_API_KEY": "env-key"}, runner=runner)

    assert from_env.credential("nvidia").header() == "Bearer env-key"
    assert from_env.get("nvidia").key_source == "env:NVIDIA_API_KEY"
    assert calls == [], "the command must not run when the variable is set"

    assert load_providers(path, env={}, runner=runner).credential("nvidia").header() == (
        "Bearer cmd-key"
    )
    assert calls == ["pass show nvidia/key"]


def test_a_bearer_provider_must_say_where_its_key_comes_from(tmp_path):
    with pytest.raises(ProviderError, match="needs 'api_key_env' or 'api_key_command'"):
        load_providers(write(tmp_path, '[[providers]]\nname = "nvidia"\n'), env={})


def test_an_unknown_provider_without_a_base_url_is_refused(tmp_path):
    with pytest.raises(ProviderError, match="needs 'base_url'"):
        load_providers(
            write(
                tmp_path,
                """
[[providers]]
name = "acme"
api_key_env = "ACME_KEY"
""",
            ),
            env={"ACME_KEY": "x"},
        )


def test_base_url_variants_share_one_root_convention(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "openrouter"
api_key_env = "OPENROUTER_API_KEY"

[[providers]]
name = "nvidia"
api_key_env = "NVIDIA_API_KEY"

[[providers]]
name = "ollama"
base_url = "http://127.0.0.1:11434/v1"
""",
        ),
        env={"OPENROUTER_API_KEY": "a", "NVIDIA_API_KEY": "b"},
    )

    # Every root can carry /v1/chat/completions exactly once.
    assert catalogue.get("openrouter").root == "https://openrouter.ai/api"
    assert catalogue.get("nvidia").root == "https://integrate.api.nvidia.com"
    assert catalogue.get("ollama").root == "http://127.0.0.1:11434"
    assert catalogue.get("ollama").configured, "a local runtime needs no credential"


def test_apply_auth_replaces_the_client_token_instead_of_forwarding_it(tmp_path):
    catalogue = load_providers(
        write(tmp_path, TWO_PROVIDERS),
        env={"OPENROUTER_API_KEY": "or-secret", "NVIDIA_API_KEY": "nv-secret"},
    )

    headers = catalogue.apply_auth(
        "nvidia", {"authorization": "Bearer sk-client-placeholder", "accept": "text/event-stream"}
    )

    assert headers["authorization"] == "Bearer nv-secret"
    assert headers["accept"] == "text/event-stream"


def test_apply_auth_without_a_credential_still_drops_the_client_token(tmp_path):
    catalogue = load_providers(write(tmp_path, TWO_PROVIDERS), env={})

    headers = catalogue.apply_auth("nvidia", {"authorization": "Bearer sk-client"})

    assert "authorization" not in headers


def test_provider_for_falls_back_to_the_only_configured_provider(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "openrouter"
api_key_env = "OPENROUTER_API_KEY"
""",
        ),
        env={"OPENROUTER_API_KEY": "x"},
    )

    assert catalogue.provider_for("anything/at-all").name == "openrouter"


def test_provider_for_refuses_to_guess_between_several_providers(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "openrouter"
api_key_env = "A"

[[providers]]
name = "nvidia"
api_key_env = "B"
""",
        ),
        env={"A": "1", "B": "2"},
    )

    assert catalogue.provider_for("unknown-model") is None


def test_registry_provider_column_beats_the_id_prefix(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "openrouter"
api_key_env = "A"

[[providers]]
name = "nvidia"
api_key_env = "B"
""",
        ),
        env={"A": "1", "B": "2"},
    )
    registry = Registry(
        {
            "nvidia/nemotron": ModelInfo(
                id="nvidia/nemotron", provider="openrouter", context_window=1000
            )
        }
    )

    merged = catalogue_from_registry(catalogue, registry)

    # The registry is authoritative: this model is served through OpenRouter
    # even though its id starts with another provider's name.
    assert merged.provider_for("nvidia/nemotron").name == "openrouter"


def test_a_missing_default_file_is_an_empty_catalogue_not_a_crash(tmp_path):
    assert len(load_providers(tmp_path / "absent.toml")) == 0
    assert len(empty_catalogue()) == 0


def test_discover_listed_pins_a_provider_to_its_own_model_list(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "nvidia"
api_key_env = "NVIDIA_API_KEY"
discover = "listed"
models = ["nvidia/nemotron-3.5-lightning-30b-a3b", "poolside/laguna-xs-2.1"]
""",
        ),
        env={"NVIDIA_API_KEY": "secret"},
    )

    provider = catalogue.get("nvidia")

    assert provider is not None
    assert provider.discover == "listed"
    assert provider.models == (
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "poolside/laguna-xs-2.1",
    )


def test_discovery_defaults_to_taking_everything(tmp_path):
    catalogue = load_providers(
        write(
            tmp_path,
            """
[[providers]]
name = "openrouter"
api_key_env = "OPENROUTER_API_KEY"
""",
        ),
        env={"OPENROUTER_API_KEY": "secret"},
    )

    assert catalogue.get("openrouter").discover == "all"


def test_a_listed_provider_without_models_is_refused(tmp_path):
    with pytest.raises(ProviderError, match="non-empty 'models'"):
        load_providers(
            write(
                tmp_path,
                """
[[providers]]
name = "nvidia"
api_key_env = "X"
discover = "listed"
""",
            ),
            env={"X": "1"},
        )


def test_an_unknown_discovery_mode_is_refused(tmp_path):
    with pytest.raises(ProviderError, match="unknown discover"):
        load_providers(
            write(
                tmp_path,
                """
[[providers]]
name = "nvidia"
api_key_env = "X"
discover = "everything"
models = ["m"]
""",
            ),
            env={"X": "1"},
        )


def test_unknown_keys_are_rejected_rather_than_ignored(tmp_path):
    with pytest.raises(ProviderError, match="unknown key 'api_key'"):
        load_providers(
            write(
                tmp_path,
                """
[[providers]]
name = "nvidia"
api_key_env = "X"
api_key = "literal-secret"
""",
            ),
            env={"X": "1"},
        )
