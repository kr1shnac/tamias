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
    # The cheap model must actually be cheaper: an active switch to an
    # equal-or-dearer target is refused, so a flat sheet would make every
    # rewrite test vacuous.
    return PriceSheet(
        date="2026-10-15",
        models={
            CHEAP: ModelPrice(input=0.5, output=1.0),
            STRONG: ModelPrice(input=1.0, output=2.0),
        },
    )


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


def strong_with_tools() -> ModelInfo:
    return ModelInfo(id=STRONG, provider="test", context_window=8192, tools=True)


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
    transport: httpx.AsyncBaseTransport | None = None,
    **extra: Any,
) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        mode,
        config=router_config or config(),
        registry=capabilities,
        transport=transport if transport is not None else StreamingASGITransport(upstream),
        **extra,
    )


def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


def body_of(model: str) -> bytes:
    return json.dumps({"model": model, "messages": MESSAGES}).encode()


def planning_body_of(model: str) -> bytes:
    """A user turn: the router stays put, so a refusal is the provider's only act."""
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "plan the work"}]}
    ).encode()


def refusing_upstream(sent: list[dict[str, Any]], refused: str = STRONG) -> FastAPI:
    """Rate-limit one model and serve the other, recording every attempt."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        body = json.loads(await request.body())
        sent.append(body)
        if body.get("model") == refused:
            return JSONResponse(
                {"error": {"message": "rate limited"}},
                status_code=429,
                headers={"retry-after": "60"},
            )
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


class FailOnModelTransport(httpx.AsyncBaseTransport):
    """An in-process upstream that drops the connection for one model.

    A transport failure on the *retry* is the one the proxy cannot afford to
    leak: the caller has already been refused once, so an unhandled exception
    turns a deliberate 502 into a 500 with no ledger row.
    """

    def __init__(self, app: FastAPI, fail_model: str) -> None:
        self.inner = StreamingASGITransport(app)
        self.fail_model = fail_model
        self.attempts: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        try:
            parsed = json.loads(raw) if raw else {}
            model = parsed.get("model")
        except (ValueError, AttributeError):
            model = None
        self.attempts.append(model)
        if model == self.fail_model:
            raise httpx.ConnectError("simulated connection refusal")
        replayed = httpx.Request(
            request.method, request.url, headers=request.headers, content=raw
        )
        return await self.inner.handle_async_request(replayed)


async def post(
    app: FastAPI, raw: bytes, token: str | None = None, **headers: str
) -> httpx.Response:
    all_headers = dict(JSON_HEADERS)
    if token is not None:
        all_headers["authorization"] = f"Bearer {token}"
    all_headers.update(headers)
    async with as_client(app) as client:
        return await client.post("/v1/chat/completions", content=raw, headers=all_headers)


# --------------------------------------------------------------- the pin header ---


async def test_a_pinned_request_is_not_rewritten_in_active_mode(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """The caller named the model; a hard step is the caller's to know about.

    Active mode switches this exact body, so a request that stays is the pin
    working, not the router declining to fire.
    """
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    response = await post(app, body_of(STRONG), **{proxy.PIN_HEADER: "1"})

    assert response.status_code == 200
    assert [body["model"] for body in sent] == [STRONG], "the pin must win"
    assert store.last["model_used"] == STRONG
    assert store.last["decision"].action == "STAY"
    assert "pinned by client" in store.last["decision"].reason
    assert proxy.PIN_HEADER in store.last["decision"].reason or STRONG in (
        store.last["decision"].reason
    )


async def test_the_pin_header_is_not_forwarded_upstream(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """A tamias control header must stop here, like the session header."""
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))
    seen: list[str] = []

    @upstream.middleware("http")
    async def capture(request: Request, call_next: Any) -> Response:  # noqa: ANN001
        seen.extend(
            sorted(
                name
                for name in request.headers
                if name.startswith("x-tamias-")
            )
        )
        return await call_next(request)

    await post(app, body_of(STRONG), **{proxy.PIN_HEADER: "yes"})

    assert proxy.PIN_HEADER not in seen


async def test_an_unrecognised_pin_value_is_ignored(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    await post(app, body_of(STRONG), **{proxy.PIN_HEADER: "maybe"})

    assert [body["model"] for body in sent] == [CHEAP], "a stray value must not pin"


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


# ------------------------------------ a refusal is still bound by the contract ---


@pytest.mark.parametrize("mode", ["active", "shadow"])
async def test_a_refusal_never_retries_onto_a_model_the_callers_key_forbids(
    mode: str, store: FakeStore, sheet: PriceSheet
) -> None:
    """A 429 may justify a retry, but never onto a model the key cannot use.

    The provider refusing is not the operator opting into a rewrite: a key
    restricted to ``STRONG`` must see the provider's own answer rather than be
    silently moved onto ``CHEAP``, or the allowlist stops meaning "only these
    models" the first time a provider has a bad minute.
    """
    sent: list[dict[str, Any]] = []
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[STRONG])
        app = build_app(
            refusing_upstream(sent),
            store,
            sheet,
            mode=mode,
            capabilities=registry(strong_with_tools(), cheap_with_tools()),
            key_store=keys,
        )

        response = await post(app, planning_body_of(STRONG), token)

        assert response.status_code == 429
        assert [body["model"] for body in sent] == [STRONG]
        assert store.last["decision"].action == "STAY"
        assert CHEAP not in store.last["decision"].reason
    finally:
        keys.close()


async def test_off_mode_never_retries_onto_another_model(
    store: FakeStore, sheet: PriceSheet
) -> None:
    """``off`` promises the model that was asked for, refusal or not."""
    sent: list[dict[str, Any]] = []
    app = build_app(
        refusing_upstream(sent),
        store,
        sheet,
        mode="off",
        capabilities=registry(strong_with_tools(), cheap_with_tools()),
    )

    response = await post(app, planning_body_of(STRONG))

    assert response.status_code == 429
    assert [body["model"] for body in sent] == [STRONG]
    assert store.last["decision"].action == "STAY"


async def test_a_transport_failure_on_the_retry_is_a_coded_502_with_one_row(
    store: FakeStore, sheet: PriceSheet
) -> None:
    """The retry is the likeliest place for a dropped connection: it must be
    answered and metered like any other setup failure, not escape as a 500."""
    sent: list[dict[str, Any]] = []
    upstream = refusing_upstream(sent)
    transport = FailOnModelTransport(upstream, CHEAP)
    app = build_app(
        upstream,
        store,
        sheet,
        capabilities=registry(strong_with_tools(), cheap_with_tools()),
        transport=transport,
    )

    response = await post(app, planning_body_of(STRONG))

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "upstream connection failed"
    assert transport.attempts == [STRONG, CHEAP]
    assert len(store.rows) == 1
    assert store.last["status"] == "502"
    assert store.last["failure_stage"] == "setup"
    assert store.last["failure_type"] == "connect_error"


async def test_hysteresis_is_measured_from_the_last_switch_not_the_session_start(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """A planning conversation gets one switch per ``min_gap``, not one per tool.

    The pattern is the one an agent loop actually produces: plan, run a tool,
    plan, run a tool.  Counting from the session's first request let a session
    that passed the gap once switch on *every* later tool turn, so this pins
    the clock to the last applied switch instead.
    """
    app = build_app(
        upstream,
        store,
        sheet,
        router_config=RouterConfig(cheap_model=CHEAP, min_gap=3),
        capabilities=registry(cheap_with_tools()),
    )
    session = {proxy.SESSION_HEADER: "s1"}
    bodies = [
        planning_body_of(STRONG) if index % 2 == 0 else body_of(STRONG)
        for index in range(10)
    ]

    for raw in bodies:
        assert (await post(app, raw, **session)).status_code == 200

    ran = [body["model"] for body in sent]
    assert ran == [STRONG] * 3 + [CHEAP] + [STRONG] * 3 + [CHEAP] + [STRONG] * 2
    assert ran.count(CHEAP) == 2, ran


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


def test_active_validates_every_pool_member_not_only_the_primary(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    pool = RouterConfig(cheap_pool=(CHEAP, "gpt-other"), min_gap=0)
    with pytest.raises(ValueError, match="gpt-other.*not in the capability registry"):
        build_app(
            upstream, store, sheet, router_config=pool, capabilities=registry(cheap_with_tools())
        )


def test_active_refuses_a_pool_member_that_cannot_call_tools(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    other = ModelInfo(id="gpt-other", provider="test", context_window=8192, tools=False)
    pool = RouterConfig(cheap_pool=(CHEAP, "gpt-other"), min_gap=0)
    with pytest.raises(ValueError, match="gpt-other.*does not support tool calls"):
        build_app(
            upstream,
            store,
            sheet,
            router_config=pool,
            capabilities=registry(cheap_with_tools(), other),
        )


def test_active_refuses_an_unpriced_pool_member(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    other = ModelInfo(id="gpt-other", provider="test", context_window=8192, tools=True)
    pool = RouterConfig(cheap_pool=(CHEAP, "gpt-other"), min_gap=0)
    with pytest.raises(ValueError, match="gpt-other.*not priced by the price sheet"):
        build_app(
            upstream,
            store,
            sheet,
            router_config=pool,
            capabilities=registry(cheap_with_tools(), other),
        )


def test_active_starts_when_every_pool_member_is_vouched_for(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    other = ModelInfo(id="gpt-other", provider="test", context_window=8192, tools=True)
    price = ModelPrice(input=1.0, output=2.0)
    priced = PriceSheet(
        date="2026-10-15", models={CHEAP: price, STRONG: price, "gpt-other": price}
    )
    pool = RouterConfig(cheap_pool=(CHEAP, "gpt-other"), min_gap=0)
    app = build_app(
        upstream,
        store,
        priced,
        router_config=pool,
        capabilities=registry(cheap_with_tools(), other),
    )
    assert app.state.router_mode == "active"


# ------------------------------------------------- what the response says ---


def lower_headers(response: httpx.Response) -> dict[str, str]:
    return {key.lower(): value for key, value in response.headers.items()}


def user_turn_body(model: str) -> bytes:
    return json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()


async def test_an_active_switch_is_advertised_on_the_response(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """The response says which model actually ran; the body cannot.

    The upstream only ever sees the rewritten model, so the model the client
    named and the model that ran are knowledge the gateway holds alone.  A
    client that wants to *show* the switch -- an agent UI does -- has to learn
    it here, and these headers are the only place it can.
    """
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    response = await post(app, body_of(STRONG))

    assert response.status_code == 200
    headers = lower_headers(response)
    assert headers["x-tamias-router-mode"] == "active"
    assert headers["x-tamias-decision"] == "SWITCH"
    assert headers["x-tamias-model-requested"] == STRONG
    assert headers["x-tamias-model-used"] == CHEAP
    assert "easy tool: read" in headers["x-tamias-reason"]
    # The advertised model is the one that really ran, not a restatement.
    assert [body["model"] for body in sent] == [CHEAP]
    assert response.json()["model"] == CHEAP


async def test_the_routing_headers_reach_a_streaming_response(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """A stream's headers are written before its first byte, so the decision
    must be on them: reading the body would mean waiting for the whole turn."""
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))
    body = json.dumps({"model": STRONG, "messages": MESSAGES, "stream": True}).encode()

    async with as_client(app) as client:
        response = await client.post(
            "/v1/chat/completions", content=body, headers=JSON_HEADERS
        )

    headers = lower_headers(response)
    assert response.status_code == 200
    assert headers["x-tamias-model-used"] == CHEAP
    assert headers["x-tamias-decision"] == "SWITCH"
    assert [body["model"] for body in sent] == [CHEAP]


async def test_a_stay_advertises_the_model_that_ran(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """A planning turn stays, and the response says so rather than staying
    silent: a client that shows switches must also be able to show holds."""
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    response = await post(app, user_turn_body(STRONG))

    headers = lower_headers(response)
    assert headers["x-tamias-decision"] == "STAY"
    assert headers["x-tamias-model-requested"] == STRONG
    assert headers["x-tamias-model-used"] == STRONG
    assert "user turn: planning" in headers["x-tamias-reason"]


async def test_shadow_mode_never_advertises_a_switch_it_did_not_make(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, sent: list[dict[str, Any]]
) -> None:
    """Shadow records a SWITCH and applies nothing, so the headers must name
    the mode: a shadow deployment's client would otherwise read a switch that
    never happened and bill a saving that was never earned."""
    app = build_app(upstream, store, sheet, mode="shadow")

    response = await post(app, body_of(STRONG))

    headers = lower_headers(response)
    assert headers["x-tamias-router-mode"] == "shadow"
    assert headers["x-tamias-model-used"] == STRONG
    assert headers["x-tamias-model-used"] == headers["x-tamias-model-requested"]
    assert [body["model"] for body in sent] == [STRONG]
    assert store.last["decision"].persisted_only is True


async def test_router_off_advertises_itself_as_off(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """Off is not shadow: no decision is computed at all, and the mode header
    is what tells a reader which of the two it is looking at."""
    app = build_app(upstream, store, sheet, mode="off")

    response = await post(app, body_of(STRONG))

    headers = lower_headers(response)
    assert headers["x-tamias-router-mode"] == "off"
    assert headers["x-tamias-decision"] == "STAY"
    assert "router disabled" in headers["x-tamias-reason"]


async def test_an_upstream_cannot_forge_the_routing_headers(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """The gateway's view of its own routing outranks the upstream's.

    A provider -- or anything pretending to be one -- that answers with its own
    ``x-tamias-model-used`` would rewrite the client's account of what ran, so
    the proxy merges its headers last.
    """
    app = build_app(upstream, store, sheet, capabilities=registry(cheap_with_tools()))

    @upstream.middleware("http")
    async def forge(request: Request, call_next: Any) -> Response:  # noqa: ANN001
        response = await call_next(request)
        response.headers["x-tamias-model-used"] = "gpt-lied"
        response.headers["x-tamias-decision"] = "STAY"
        return response

    response = await post(app, body_of(STRONG))

    headers = lower_headers(response)
    assert headers["x-tamias-model-used"] == CHEAP
    assert headers["x-tamias-decision"] == "SWITCH"
