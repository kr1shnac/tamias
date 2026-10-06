from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias.router_config import load_router_config


def test_generic_profile_recognizes_error_colon_and_legacy_does_not() -> None:
    assert "Error:" in load_router_config(profile="generic").error_markers
    assert "Error:" not in load_router_config(profile="legacy").error_markers


def test_toml_config_overrides_all_documented_keys(tmp_path: Path) -> None:
    path = tmp_path / "router.toml"
    path.write_text(
        "[router]\neasy_tools = ['inspect']\nerror_markers = ['BAD']\n"
        "cheap_model = 'cheap'\nstrong_model = 'strong'\nmin_gap = 0\n",
        encoding="utf-8",
    )
    assert load_router_config(path) == load_router_config(path, profile="legacy")
    loaded = load_router_config(path)
    assert loaded.easy_tools == frozenset({"inspect"})
    assert loaded.error_markers == frozenset({"BAD"})
    assert (loaded.cheap_model, loaded.strong_model, loaded.min_gap) == ("cheap", "strong", 0)


def test_bad_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "router.toml"
    path.write_text("[router]\nmin_gap = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-negative"):
        load_router_config(path)
