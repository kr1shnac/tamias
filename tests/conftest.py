"""Shared test setup.

The config file is read from ``$TAMIAS_CONFIG`` (else ``~/.tamias/config.toml``).
Tests must not depend on the developer's real config, so point the variable at a
path that does not exist unless a test overrides it.  The provider catalogue,
the discovery cache and the limit ledger follow the same rule: a test that
implicitly read the developer's real ``~/.tamias/providers.toml`` would route
somewhere the test never configured.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TAMIAS_CONFIG", str(tmp_path / "no-such-config.toml"))
    monkeypatch.setenv("TAMIAS_PROVIDERS", str(tmp_path / "no-such-providers.toml"))
    monkeypatch.setenv("TAMIAS_MODELS_CACHE", str(tmp_path / "no-such-cache.json"))
    monkeypatch.setenv("TAMIAS_REGISTRY", str(tmp_path / "no-such-registry.toml"))
