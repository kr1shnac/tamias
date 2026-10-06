"""Tests for optional reasoning-effort switching.

All proxy tests use in-process ASGI transports; no listener is opened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tamias.effort import apply_effort
from tamias.pricing import PriceSheet
from tamias.proxy import create_app
from tamias.router import RouterConfig, decide
from tamias.store import Store
from tamias.types import SessionState


def tool_body(content: str = "ok") -> dict[str, object]:
    return {
        "model": "strong",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "shell"}}]},
            {"role": "tool", "tool_call_id": "1", "content": content},
        ],
    }


def policy() -> RouterConfig:
    return RouterConfig(cheap_model="cheap", min_gap=0, effort_policy=True)


def test_apply_effort_preserves_the_input_and_existing_reasoning() -> None:
    body = {"model": "strong", "reasoning": {"effort": "high", "keep": True}}

    result = apply_effort(body, "low")

    assert result is not body
    assert result["reasoning"] == {"effort": "low", "keep": True}
    assert body["reasoning"] == {"effort": "high", "keep": True}


def test_apply_effort_openai_style() -> None:
    assert apply_effort({"model": "strong"}, "low", style="openai")["reasoning_effort"] == "low"


def test_policy_selects_high_for_planning_and_error_and_low_for_easy_tool() -> None:
    state = SessionState(session_id="s", request_index=0, current_model="strong")
    planning = {"model": "strong", "messages": [{"role": "user"}]}
    assert decide(planning, state, policy()).target_effort == "high"
    assert decide(tool_body("Traceback: failed"), state, policy()).target_effort == "high"
    assert decide(tool_body(), state, policy()).target_effort == "low"


def test_policy_off_preserves_existing_router_decision() -> None:
    state = SessionState(session_id="s", request_index=0, current_model="strong")
    result = decide(tool_body(), state, RouterConfig(cheap_model="cheap", min_gap=0))
    assert result.action == "SWITCH"
    assert result.target_effort is None


async def test_active_proxy_forwards_and_persists_the_effective_effort(tmp_path: Path) -> None:
    received: list[dict[str, object]] = []
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        received.append(await request.json())
        return JSONResponse(
            {"model": "cheap", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )

    store = Store(tmp_path / "requests.sqlite")
    app = create_app(
        "http://upstream.invalid",
        store,
        PriceSheet(date="2026-10-07", models={}),
        "active",
        config=policy(),
        transport=httpx.ASGITransport(app=upstream),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            response = await client.post("/v1/chat/completions", content=json.dumps(tool_body()))

        assert response.status_code == 200
        assert received == [{**tool_body(), "model": "cheap", "reasoning": {"effort": "low"}}]
        (row,) = store.rows()
        assert row["effort_requested"] is None
        assert row["effort_used"] == "low"
        assert row["decision_target_effort"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_shadow_proxy_keeps_the_body_byte_identical(tmp_path: Path) -> None:
    received: list[bytes] = []
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        received.append(await request.body())
        return JSONResponse(
            {"model": "strong", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )

    store = Store(tmp_path / "requests.sqlite")
    app = create_app(
        "http://upstream.invalid",
        store,
        PriceSheet(date="2026-10-07", models={}),
        "shadow",
        config=policy(),
        transport=httpx.ASGITransport(app=upstream),
    )
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            response = await client.post("/v1/chat/completions", content=raw)

        assert response.status_code == 200
        assert received == [raw]
        (row,) = store.rows()
        assert row["effort_used"] is None
        assert row["decision_target_effort"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()
