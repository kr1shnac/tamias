"""Shared test setup.

The config file is read from ``$TAMIAS_CONFIG`` (else ``~/.tamias/config.toml``).
Tests must not depend on the developer's real config, so point the variable at a
path that does not exist unless a test overrides it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TAMIAS_CONFIG", str(tmp_path / "no-such-config.toml"))
