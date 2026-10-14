"""Active routing: opt-in, fail-closed, and suppressed when the caller may not use it.

The claim these tests defend: ``router_mode="active"`` may rewrite the model a
provider runs, and *only* under conditions the deployment has stated.  It
refuses to start against a target it has not vouched for (no target; a target
outside the capability registry; a target that cannot call tools; an unpriced
target), it never applies a switch the caller's own key forbids, and the model a
request is priced and capped against is the model that actually runs.

Everything runs in-process; no socket is opened.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mock_upstream import StreamingASGITransport

from tamias import proxy
from tamias.keys import KeyStore
from tamias.pricing import ModelPrice, PriceSheet
from tamias.registry import ModelInfo, Registry, parse_registry
from tamias.router import RouterConfig, applied_target
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
JSON_HEADERS = {"content-type": "application/json"}
STRONG = "gpt-strong"
CHEAP = "gpt-cheap"

# A tool result from an easy tool is what the router switches on; a trailing
# user message is a planning step and stays put.
MESSAGES: list[dict[str, Any]] = [
    {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
    {"role": "tool", "tool_call_id": "1", "content": "file body"},
]


class FakeStore:
    """Collects the rows the proxy logs, with store.Store's exact signature."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_request(
        self,
        ts: str,
        session_id: str,
        model_requested: str,
        model_used: str,
        usage: Usage,
        cost: CostBreakdown,
        latency_ms: int | None,
        status: str,
        decision: Decision,
        **provenance: Any,
    ) -> int:
        self.rows.append(
            {
                "ts": ts,
                "session_id": session_id,
                "model_requested": model_requested,
                "model_used": model_used,
                "usage": usage,
                "cost": cost,
                "latency_ms": latency_ms,
                "status": status,
                "decision": decision,
                **provenance,
            }
        )
        return len(self.rows)

    @property
    def last(self) -> dict[str, Any]:
        assert self.rows, "expected at least one logged row"
        return self.rows[-1]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sheet() -> PriceSheet:
    price = ModelPrice(input=1.0, output=2.0)
    return PriceSheet(date="2026-10-15", models={CHEAP: price, STRONG: price})


@pytest.fixture
def sent() -> list[dict[str, Any]]:
    """Every request body the upstream actually received, parsed."""
    return []


@pytest.fixture
def upstream(sent: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        body = json.loads(await request.body())
        sent.append(body)
        return JSONResponse(
            {
                "id": "chat-1",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        )

    return app


def registry(*entries: ModelInfo) -> Registry:
    """A Registry built through the real parser, so the fixture cannot drift."""
    return parse_registry(
        {
            "models": [
                {
                    "id": entry.id,
                    "provider": entry.provider,
                    "context_window": entry.context_window,
                    "tools": entry.tools,
                    "reasoning": entry.reasoning,
                    "efforts": list(entry.efforts),
                }
                for entry in entries
            ]
        }
    )


def cheap_with_tools() -> ModelInfo:
    return ModelInfo(id=CHEAP, provider="test", context_window=8192, tools=True)


def config(**extra: Any) -> RouterConfig:
    return RouterConfig(cheap_model=CHEAP, min_gap=0, **extra)


def build_app(
    upstream: FastAPI,
    store: FakeStore,
    sheet: PriceSheet,
    *,
    mode: str = "active",
    router_config: RouterConfig | None = None,
    capabilities: Registry | None = None,
    **extra: Any,
) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        mode,
        config=router_config or config(),
        registry=capabilities,
        transport=StreamingASGITransport(upstream),
        **extra,
    )


def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


def body_of(model: str) -> bytes:
    return json.dumps({"model": model, "messages": MESSAGES}).encode()


async def post(app: FastAPI, raw: bytes, token: str | None = None) -> httpx.Response:
    headers = dict(JSON_HEADERS)
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    async with as_client(app) as client:
        return await client.post("/v1/chat/completions", content=raw, headers=headers)


# ------------------------------------------------------- applied_target (pure) ---


def test_applied_target_applies_only_a_switch_to_a_different_model() -> None:
    switch = Decision(action="SWITCH", target_model=CHEAP, reason="easy")
    stay = Decision(action="STAY", target_model=None, reason="planning")

    assert applied_target(switch, requested=STRONG, enabled=True) == CHEAP
    # A switch to the model already in the request would rewrite nothing.
    assert applied_target(switch, requested=CHEAP, enabled=True) is None
    assert applied_target(stay, requested=STRONG, enabled=True) is None


def test_applied_target_never_acts_when_the_mode_is_not_active() -> None:
    switch = Decision(action="SWITCH", target_model=CHEAP, reason="easy")
    assert applied_target(switch, requested=STRONG, enabled=False) is None


def test_applied_target_is_suppressed_by_an_allowlist_that_lacks_the_target() -> None:
    switch = Decision(action="SWITCH", target_model=CHEAP, reason="easy")

    assert applied_target(switch, requested=STRONG, enabled=True, allowed=[CHEAP]) == CHEAP
    assert applied_target(switch, requested=STRONG, enabled=True, allowed=[STRONG]) is None
    # None means "no allowlist", not "nothing allowed".
    assert applied_target(switch, requested=STRONG, enabled=True, allowed=None) == CHEAP


# ------------------------------------------------------------------ the gates ---


def test_active_refuses_to_start_without_a_cheap_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    with pytest.raises(ValueError, match="needs a cheap model"):
        build_app(upstream, store, sheet, router_config=RouterConfig(min_gap=0))


def test_active_refuses_a_target_outside_the_registry(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    with pytest.raises(ValueError, match="not in the capability registry"):
        build_app(upstream, store, sheet, capabilities=registry())


def test_active_refuses_a_target_that_cannot_call_tools(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    no_tools = ModelInfo(id=CHEAP, provider="test", context_window=8192, tools=False)
    with pytest.raises(ValueError, match="does not support tool calls"):
        build_app(upstream, store, sheet, capabilities=registry(no_tools))


def test_active_refuses_an_unpriced_target(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    blind = PriceSheet(date="2026-10-15", models={STRONG: ModelPrice(input=1.0, output=2.0)})
    with pytest.raises(ValueError, match="not priced by the price sheet"):
        build_app(upstream, store, blind, capabilities=registry(cheap_with_tools()))


def test_active_starts_once_the_target_is_vouched_for(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))
    assert app.state.router_mode == "active"


def test_an_unknown_router_mode_is_still_refused(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    with pytest.raises(ValueError, match="unknown router mode"):
        build_app(upstream, store, sheet, mode="routed")


# --------------------------------------------------------------- the rewrite ---


async def test_active_rewrites_the_model_the_upstream_runs(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    response = await post(app, body_of(STRONG))

    assert response.status_code == 200
    assert [body["model"] for body in sent] == [CHEAP]
    # The log keeps both sides of the divergence, so a run can prove what ran.
    assert store.last["model_requested"] == STRONG
    assert store.last["model_used"] == CHEAP
    assert store.last["decision"].action == "SWITCH"
    assert store.last["decision"].persisted_only is False
    assert store.last["router_mode"] == "active"


async def test_shadow_mode_still_forwards_the_requested_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """The same decision, recorded but not applied: shadow stays byte-preserving."""
    app = build_app(upstream, store, sheet, mode="shadow")

    await post(app, body_of(STRONG))

    assert [body["model"] for body in sent] == [STRONG]
    assert store.last["decision"].action == "SWITCH"
    assert store.last["decision"].persisted_only is True
    assert store.last["model_used"] == STRONG


async def test_a_switch_the_callers_key_forbids_is_suppressed_not_refused(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """A restricted key stays on the model it named rather than failing on one
    it never named, and the gateway never runs it on a model it may not use."""
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[STRONG])
        app = build_app(
            upstream,
            store,
            sheet,
            capabilities=registry(cheap_with_tools()),
            key_store=keys,
        )

        response = await post(app, body_of(STRONG), token)

        assert response.status_code == 200
        assert [body["model"] for body in sent] == [STRONG]
        assert store.last["decision"].action == "SWITCH"
        assert store.last["model_used"] == STRONG
    finally:
        keys.close()


async def test_a_callers_key_that_may_use_the_target_gets_the_switch(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[STRONG, CHEAP])
        app = build_app(
            upstream,
            store,
            sheet,
            capabilities=registry(cheap_with_tools()),
            key_store=keys,
        )

        await post(app, body_of(STRONG), token)

        assert [body["model"] for body in sent] == [CHEAP]
    finally:
        keys.close()


# ------------------------------------------------ priced as it actually runs ---


async def test_a_model_sub_cap_measures_the_model_that_runs_not_the_one_named(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """The reserve follows the routed model, so a sub-cap on the target stops the
    request even though the caller named the other model."""
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(
            label="alice",
            models=[STRONG, CHEAP],
            budget_usd="50.00",
            model_caps=[(CHEAP, "0.25")],
        )
        app = build_app(
            upstream,
            store,
            sheet,
            capabilities=registry(cheap_with_tools()),
            key_store=keys,
        )

        assert (await post(app, body_of(STRONG), token)).status_code == 200
        # The first request was priced against CHEAP, so its cap is what filled.
        capped = await post(app, body_of(STRONG), token)
        assert capped.status_code == 402
        assert "sub-cap" in capped.json()["error"]["message"]
    finally:
        keys.close()


async def test_a_budgeted_key_is_priced_against_the_model_that_runs(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """An unpriced *requested* model is fine when the target is priced: the
    gateway bills what it runs, not what the caller typed."""
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[STRONG, CHEAP], budget_usd="5.00")
        priced = PriceSheet(date="2026-10-15", models={CHEAP: ModelPrice(input=1.0, output=2.0)})
        app = build_app(
            upstream,
            store,
            priced,
            capabilities=registry(cheap_with_tools()),
            key_store=keys,
        )

        response = await post(app, body_of(STRONG), token)

        assert response.status_code == 200
    finally:
        keys.close()


# ---------------------------------------------------- effort on what will run ---


async def test_effort_is_applied_to_the_routed_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """Both axes on one request: the model is rewritten *and* the effort the
    policy chose for that step is sent, resolved against the model that runs."""
    capabilities = registry(
        ModelInfo(
            id=CHEAP,
            provider="test",
            context_window=8192,
            tools=True,
            reasoning=True,
            efforts=("low",),
        )
    )
    app = build_app(
        upstream,
        store,
        sheet,
        router_config=config(effort_policy=True),
        capabilities=capabilities,
        effort_mode="apply",
    )

    await post(app, body_of(STRONG))

    (body,) = sent
    assert body["model"] == CHEAP
    assert body["reasoning"] == {"effort": "low"}
    assert store.last["effort_used"] == "low"
