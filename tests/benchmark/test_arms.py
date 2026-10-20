"""Arm C must exist, and must be a genuine null hypothesis (F-18).

`TAMIAS-SSOT.md` §18 pairs routing off (A) with routing on (B). F-18 records that
a statically cheaper model in the agent's own config may capture the same saving
with none of the risk, so the benchmark needs a third arm: **cheap model pinned,
no proxy**. These tests pin the properties that make arm C that arm, so it
cannot be quietly dropped or allowed to drift into a copy of arm B.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import arms  # noqa: E402

STRONG = "openai/gpt-5-mini"
CHEAP = "nvidia/nemotron-3.5-lightning:free"


def test_three_arms_are_defined() -> None:
    assert arms.ARMS == (arms.ARM_OFF, arms.ARM_ON, arms.ARM_STATIC)
    assert len(arms.ARMS) == 3


def test_arm_c_uses_no_proxy() -> None:
    """The whole point: arm C is the counterfactual *without* Tamias."""
    cfg = arms.agent_config(arms.ARM_STATIC, strong_model=STRONG, cheap_model=CHEAP)
    assert cfg.arm.uses_proxy is False
    assert "tamias" not in cfg.base_url
    assert "127.0.0.1" not in cfg.base_url
    assert "tamias" not in cfg.model


def test_arm_c_pins_the_cheap_model() -> None:
    """Arm C asks for cheap by name; there is no proxy to rewrite it."""
    cfg = arms.agent_config(arms.ARM_STATIC, strong_model=STRONG, cheap_model=CHEAP)
    assert cfg.model.endswith(CHEAP)
    assert CHEAP in cfg.model


@pytest.mark.parametrize("name", [arms.ARM_OFF, arms.ARM_ON])
def test_proxy_arms_ask_for_the_strong_model_through_the_proxy(name: str) -> None:
    cfg = arms.agent_config(name, strong_model=STRONG, cheap_model=CHEAP, proxy_port=8123)
    assert cfg.arm.uses_proxy is True
    assert cfg.base_url == "http://127.0.0.1:8123/v1"
    assert cfg.model.endswith(STRONG)


def test_arms_a_and_b_differ_only_in_routing() -> None:
    """A and B share a config shape; the routing mode is a serve-time flag.

    If their configs diverged, an A-vs-B delta could be an artifact of the
    config rather than the router.
    """
    a = arms.agent_config(arms.ARM_OFF, strong_model=STRONG, cheap_model=CHEAP)
    b = arms.agent_config(arms.ARM_ON, strong_model=STRONG, cheap_model=CHEAP)
    assert a.document == b.document
    assert a.arm.routing == "off"
    assert b.arm.routing == "active"


def test_arm_c_document_is_valid_json_with_one_provider() -> None:
    import json

    cfg = arms.agent_config(arms.ARM_STATIC, strong_model=STRONG, cheap_model=CHEAP)
    text = arms.render(cfg)
    parsed = json.loads(text)
    assert len(parsed["provider"]) == 1
    assert text.endswith("\n")


def test_unknown_arm_is_rejected() -> None:
    with pytest.raises(ValueError):
        arms.arm("arm-z")
