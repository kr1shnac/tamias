"""The misroute policy: a conversation that stumbles stops shopping.

The claim these tests defend: when a session has been moved off the model the
caller named and the *next* turn fails a tool, that session keeps the caller's
model for a while. The scope is one conversation, because tamias cannot see the
counterfactual -- it did not also run the strong model -- so one bad turn must
never suppress a model for everybody. Off by default, and every escalation is
logged with a reason that names it, so the report shows the policy's effect.

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
from tamias.misroute import MisroutePolicy, failure_follows_downgrade
from tamias.pricing import ModelPrice, PriceSheet
from tamias.registry import ModelInfo, Registry
from tamias.router import RouterConfig
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
JSON_HEADERS = {"content-type": "application/json"}
STRONG = "gpt-strong"
CHEAP = "gpt-cheap"

EASY_MESSAGES: list[dict[str, Any]] = [
    {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
    {"role": "tool", "tool_call_id": "1", "content": "file body"},
]
ERROR_MESSAGES: list[dict[str, Any]] = [
    {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
    {"role": "tool", "tool_call_id": "1", "content": "Traceback: ValueError: bad"},
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
    return PriceSheet(
        date="2026-11-09",
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


@pytest.fixture
def capabilities() -> Registry:
    return Registry(
        {
            STRONG: ModelInfo(id=STRONG, provider="test", context_window=8192, tools=True),
            CHEAP: ModelInfo(id=CHEAP, provider="test", context_window=8192, tools=True),
        }
    )


def build(
    upstream: FastAPI,
    store: FakeStore,
    sheet: PriceSheet,
    capabilities: Registry,
    *,
    mode: str = "active",
    misroute: MisroutePolicy | None = None,
    min_gap: int = 0,
) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        mode,
        config=RouterConfig(cheap_model=CHEAP, min_gap=min_gap),
        registry=capabilities,
        transport=StreamingASGITransport(upstream),
        misroute=misroute,
    )


async def post(app: FastAPI, messages: list[dict[str, Any]]) -> httpx.Response:
    transport = StreamingASGITransport(app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://proxy.test", timeout=None
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": STRONG, "messages": messages}).encode(),
            headers=JSON_HEADERS,
        )


# --- the pure predicate ------------------------------------------------------


def test_only_a_failure_after_a_downgrade_is_a_strike() -> None:
    assert failure_follows_downgrade(True, "tool error: needs strong model")
    assert not failure_follows_downgrade(True, "user turn: planning")
    assert not failure_follows_downgrade(False, "tool error: needs strong model")
    assert not failure_follows_downgrade(False, "user turn: planning")


def test_the_policy_is_off_unless_it_is_asked_for() -> None:
    assert MisroutePolicy().enabled is False


def test_a_negative_window_is_refused_at_startup() -> None:
    with pytest.raises(ValueError):
        proxy.create_app(
            UPSTREAM_URL,
            FakeStore(),
            PriceSheet(date="2026-11-09", models={}),
            "shadow",
            misroute=MisroutePolicy(enabled=True, escalate_turns=-1),
        )


# --- the policy in the proxy ------------------------------------------------


async def test_a_stumble_holds_the_session_on_the_strong_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, capabilities: Registry, sent
) -> None:
    app = build(upstream, store, sheet, capabilities,
                misroute=MisroutePolicy(enabled=True, escalate_turns=3))

    assert (await post(app, EASY_MESSAGES)).status_code == 200
    assert sent[-1]["model"] == CHEAP, "the easy turn should have been downgraded"

    assert (await post(app, ERROR_MESSAGES)).status_code == 200
    assert sent[-1]["model"] == STRONG, "a failed tool is what the strong model is for"

    for turn in range(3):
        assert (await post(app, EASY_MESSAGES)).status_code == 200
        # Without the policy these three turns would all have been downgraded.
        assert sent[-1]["model"] == STRONG, f"turn {turn} escaped the escalation"
        assert "misroute escalation" in store.last["decision"].reason

    assert (await post(app, EASY_MESSAGES)).status_code == 200
    assert sent[-1]["model"] == CHEAP, "the window must expire"


async def test_the_policy_is_off_by_default(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, capabilities: Registry, sent
) -> None:
    app = build(upstream, store, sheet, capabilities)

    for _ in range(5):
        assert (await post(app, EASY_MESSAGES)).status_code == 200
        await post(app, ERROR_MESSAGES)
    # Every easy turn still downgrades: no adaptation was configured.
    assert all(body["model"] == CHEAP for body in sent[0::2])


async def test_one_session_is_not_penalised_for_another_sessions_stumble(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, capabilities: Registry, sent
) -> None:
    """The whole point of the session scope: bad luck is not a model verdict."""
    app = build(upstream, store, sheet, capabilities,
                misroute=MisroutePolicy(enabled=True, escalate_turns=5))
    transport = StreamingASGITransport(app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://proxy.test", timeout=None
    ) as client:

        async def send(session: str, messages: list[dict[str, Any]]) -> None:
            await client.post(
                "/v1/chat/completions",
                content=json.dumps({"model": STRONG, "messages": messages}).encode(),
                headers={**JSON_HEADERS, "x-tamias-session": session},
            )

        # s1 stumbles and escalates.
        await send("s1", EASY_MESSAGES)
        await send("s1", ERROR_MESSAGES)
        # s2 has done nothing wrong, so it keeps taking the cheap model.
        await send("s2", EASY_MESSAGES)
        await send("s2", EASY_MESSAGES)

        assert sent[2]["model"] == CHEAP
        assert sent[3]["model"] == CHEAP


async def test_shadow_mode_simulates_the_policy_without_changing_anything(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, capabilities: Registry, sent
) -> None:
    app = build(upstream, store, sheet, capabilities, mode="shadow",
                misroute=MisroutePolicy(enabled=True, escalate_turns=5))

    await post(app, EASY_MESSAGES)
    await post(app, ERROR_MESSAGES)
    third = await post(app, EASY_MESSAGES)

    # Nothing is rewritten in shadow, and the simulation is visible in the log.
    assert third.status_code == 200
    assert sent[2]["model"] == STRONG
    assert "misroute escalation" in store.last["decision"].reason
    assert store.last["decision"].persisted_only is True
