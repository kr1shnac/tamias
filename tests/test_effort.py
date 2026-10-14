"""Tests for optional reasoning-effort switching.

All proxy tests use in-process ASGI transports; no listener is opened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tamias.effort import apply_effort
from tamias.pricing import PriceSheet
from tamias.proxy import create_app
from tamias.registry import ModelInfo, Registry, empty_registry
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


async def test_active_proxy_is_rejected_for_public_alpha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="active routing is disabled"):
        create_app(
            "http://upstream.invalid",
            Store(tmp_path / "requests.sqlite"),
            PriceSheet(date="2026-10-07", models={}),
            "active",
            config=policy(),
        )


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


# ---------------------------------------------------------------------------
# the effort axis (D3): off / meter / apply, independent of model routing
# ---------------------------------------------------------------------------


def registry_with(*entries: ModelInfo) -> Registry:
    return Registry({entry.id: entry for entry in entries})


def reasoning_registry(*efforts: str) -> Registry:
    return registry_with(
        ModelInfo(
            id="strong",
            provider="test",
            context_window=1000,
            reasoning=True,
            efforts=tuple(efforts),
        )
    )


def effort_app(
    tmp_path: Path,
    mode: str,
    upstream: FastAPI,
    *,
    registry: Registry | None = None,
    config: RouterConfig | None = None,
    effort_style: str = "openrouter",
) -> tuple[FastAPI, Store]:
    store = Store(tmp_path / "requests.sqlite")
    app = create_app(
        "http://upstream.invalid",
        store,
        PriceSheet(date="2026-10-07", models={}),
        "shadow",
        config=config or RouterConfig(cheap_model="cheap", min_gap=0),
        effort_mode=mode,
        registry=registry,
        effort_style=effort_style,
        transport=httpx.ASGITransport(app=upstream),
    )
    return app, store


async def post_one(app: FastAPI, upstream: FastAPI, raw: bytes) -> list[bytes]:
    received: list[bytes] = []

    @upstream.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        received.append(await request.body())
        return JSONResponse(
            {"model": "strong", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post("/v1/chat/completions", content=raw)

    assert response.status_code == 200
    return received


async def test_unknown_effort_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown effort mode"):
        create_app(
            "http://upstream.invalid",
            Store(tmp_path / "requests.sqlite"),
            PriceSheet(date="2026-10-07", models={}),
            "shadow",
            effort_mode="loud",
        )


async def test_off_is_the_default_and_records_no_effort(tmp_path: Path) -> None:
    upstream = FastAPI()
    store = Store(tmp_path / "requests.sqlite")
    app = create_app(
        "http://upstream.invalid",
        store,
        PriceSheet(date="2026-10-07", models={}),
        "shadow",
        config=RouterConfig(cheap_model="cheap", min_gap=0),
        transport=httpx.ASGITransport(app=upstream),
    )
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert received == [raw]
        (row,) = store.rows()
        assert row["decision_target_effort"] is None
        assert row["effort_used"] is None
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_meter_mode_forces_the_policy_but_keeps_the_body_identical(
    tmp_path: Path,
) -> None:
    """meter turns the policy on from the mode alone: the config has it off."""
    upstream = FastAPI()
    app, store = effort_app(tmp_path, "meter", upstream)
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert received == [raw]
        (row,) = store.rows()
        assert row["decision_target_effort"] == "low"
        assert row["effort_used"] is None
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_apply_mode_sends_the_effort_for_a_registry_model(tmp_path: Path) -> None:
    upstream = FastAPI()
    app, store = effort_app(
        tmp_path, "apply", upstream, registry=reasoning_registry("low", "medium", "high")
    )
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert len(received) == 1
        assert json.loads(received[0])["reasoning"]["effort"] == "low"
        assert "reasoning" not in json.loads(raw)  # the caller's body was not mutated
        (row,) = store.rows()
        assert row["effort_used"] == "low"
        assert row["decision_target_effort"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_apply_mode_sends_nothing_without_a_registry_entry(tmp_path: Path) -> None:
    """Fail closed: an empty registry means apply mode applies nothing."""
    upstream = FastAPI()
    app, store = effort_app(tmp_path, "apply", upstream, registry=empty_registry())
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert received == [raw]
        (row,) = store.rows()
        assert row["effort_used"] is None
        assert row["decision_target_effort"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_apply_mode_downgrades_to_a_supported_level(tmp_path: Path) -> None:
    """A user turn targets high effort; a low-only model gets low, not high."""
    upstream = FastAPI()
    app, store = effort_app(tmp_path, "apply", upstream, registry=reasoning_registry("low"))
    planning = {"model": "strong", "messages": [{"role": "user", "content": "plan"}]}
    raw = json.dumps(planning, separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert json.loads(received[0])["reasoning"]["effort"] == "low"
        (row,) = store.rows()
        assert row["effort_used"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()


async def test_apply_mode_honours_the_openai_effort_style(tmp_path: Path) -> None:
    upstream = FastAPI()
    app, store = effort_app(
        tmp_path,
        "apply",
        upstream,
        registry=reasoning_registry("low", "high"),
        effort_style="openai",
    )
    raw = json.dumps(tool_body(), separators=(",", ":")).encode()
    try:
        received = await post_one(app, upstream, raw)
        assert json.loads(received[0])["reasoning_effort"] == "low"
        (row,) = store.rows()
        assert row["effort_used"] == "low"
    finally:
        await app.state.upstream_client.aclose()
        store.close()


def test_serve_help_exposes_effort_mode_and_registry_not_effort_policy(capsys) -> None:
    from tamias import cli

    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])

    out = capsys.readouterr().out
    assert "--effort-mode" in out
    assert "--registry" in out
    assert "--effort-policy" not in out
