"""The three benchmark arms, defined so the null hypothesis (F-18) cannot be dropped.

`TAMIAS-SSOT.md` §18 specifies a **paired** benchmark: routing off (arm A) vs on
(arm B). The council of 2026-10-20 found that pairing insufficient: F-18 records
that a **statically cheaper model in the agent's own config** "may capture the
same savings with none of the quality/cache/MITM risk". A positive A-vs-B delta
therefore proves only that *swapping models is cheaper* — something the user can
do by editing one config line, with no proxy, no prefix-cache destruction and no
interception surface.

So the benchmark needs a third arm:

| Arm | Proxy | Model the agent asks for | What it isolates |
|-----|-------|--------------------------|------------------|
| A   | yes, routing **off** | strong | the unmodified baseline |
| B   | yes, routing **on**  | strong (proxy rewrites to cheap) | the proxy's lever |
| C   | **no proxy** | cheap, pinned in the agent config | the null hypothesis |

A result that does not beat arm C does not justify Tamias. This module defines
the arms and builds each arm's agent configuration; it runs nothing and makes no
savings claim. The runner that executes the arms is separate, so this contract
can be tested offline and cheaply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# The three arms, by name.  Order here is presentation order, not ranking.
ARM_OFF = "off"
ARM_ON = "on"
ARM_STATIC = "static_cheap"
ARMS = (ARM_OFF, ARM_ON, ARM_STATIC)


@dataclass(frozen=True)
class Arm:
    """One benchmark arm: how the agent is pointed, and whether a proxy is used."""

    name: str
    uses_proxy: bool
    routing: str  # "off" | "active" | "n/a"
    model_in_config: str  # the model the agent asks for by name
    isolates: str

    def describe(self) -> str:
        via = "through tamias" if self.uses_proxy else "directly, no proxy"
        return f"{self.name}: {self.model_in_config} {via} ({self.isolates})"


def arm(name: str) -> Arm:
    """Look up an arm by name, failing loudly on a typo."""
    if name == ARM_OFF:
        return Arm(
            ARM_OFF,
            uses_proxy=True,
            routing="off",
            model_in_config="strong",
            isolates="unmodified baseline",
        )
    if name == ARM_ON:
        return Arm(
            ARM_ON,
            uses_proxy=True,
            routing="active",
            model_in_config="strong",
            isolates="the proxy's routing lever",
        )
    if name == ARM_STATIC:
        return Arm(
            ARM_STATIC,
            uses_proxy=False,
            routing="n/a",
            model_in_config="cheap",
            isolates="null hypothesis (F-18): cheap model pinned, no proxy",
        )
    raise ValueError(f"unknown arm {name!r}; expected one of {ARMS}")


@dataclass(frozen=True)
class AgentConfig:
    """The opencode.json an arm uses, plus the endpoint it is pointed at."""

    arm: Arm
    document: dict = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        provider = self.document["provider"]
        # The only provider key is the one this config injects.
        only = next(iter(provider.values()))
        return only["options"]["baseURL"]

    @property
    def model(self) -> str:
        return self.document["model"]


def agent_config(
    arm_name: str,
    *,
    strong_model: str,
    cheap_model: str,
    proxy_port: int = 8000,
) -> AgentConfig:
    """Build the opencode.json for one arm.

    Arms A and B point the same OpenAI-compatible provider at the local proxy;
    they differ only in the proxy's routing mode, which is set at serve time,
    not here. Arm C removes the proxy entirely and pins the cheap model, so it
    is the honest counterfactual: "just use a cheaper model".
    """
    selected = arm(arm_name)

    if selected.uses_proxy:
        base_url = f"http://127.0.0.1:{proxy_port}/v1"
        model = strong_model
    else:
        # No proxy: the agent talks to the provider directly.  The endpoint is
        # whatever the operator's provider needs; this contract only fixes the
        # *model*, because that is the variable arm C isolates.
        base_url = "https://openrouter.ai/api/v1"
        model = cheap_model

    document = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "tamias" if selected.uses_proxy else "openrouter": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "tamias (local proxy)" if selected.uses_proxy else "openrouter (no proxy)",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:OPENROUTER_API_KEY}",
                },
                "models": {
                    strong_model: {"name": "strong"},
                    cheap_model: {"name": "cheap"},
                },
            }
        },
        "model": f"{'tamias' if selected.uses_proxy else 'openrouter'}/{model}",
    }
    return AgentConfig(arm=selected, document=document)


def render(config: AgentConfig) -> str:
    """The opencode.json text, with a trailing newline."""
    return json.dumps(config.document, indent=2) + "\n"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print one benchmark arm's agent config.")
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument("--strong-model", required=True)
    parser.add_argument("--cheap-model", required=True)
    parser.add_argument("--proxy-port", type=int, default=8000)
    args = parser.parse_args()
    cfg = agent_config(
        args.arm,
        strong_model=args.strong_model,
        cheap_model=args.cheap_model,
        proxy_port=args.proxy_port,
    )
    print(f"# {cfg.arm.describe()}", flush=True)
    print(render(cfg), end="")
