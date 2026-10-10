"""Offline client matrix: chat, messages, and responses each land one metered row.

Every client speaks to a real proxy wired to an in-process upstream, and no
socket is opened.  All three routes share a single :class:`~tamias.store.Store`,
so the assertion is made against one log.  A fourth test proves that one proxy
app registers the whole matrix at once: a client does not need a different
build of tamias to be metered.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mock_anthropic_upstream import create_mock_anthropic_upstream
from mock_upstream import StreamingASGITransport, create_mock_upstream

from tamias import proxy
from tamias.pricing import ModelPrice, PriceSheet
from tamias.store import Store

UPSTREAM = "http://upstream.invalid"
CHAT_MODEL = "mock-chat"
MESSAGES_MODEL = "mock-claude-matrix"
RESPONSES_MODEL = "gpt-4.1"

CLIENT_ROUTES = ("/v1/chat/completions", "/v1/messages", "/v1/responses")


def _sheet() -> PriceSheet:
    return PriceSheet(
        date="2026-10-10",
        models={
            CHAT_MODEL: ModelPrice(input=1.0, output=2.0),
            MESSAGES_MODEL: ModelPrice(input=3.0, output=15.0),
            RESPONSES_MODEL: ModelPrice(input=2.0, output=8.0, cached_input=0.2),
        },
    )


def _responses_upstream() -> FastAPI:
    """A minimal OpenAI Responses upstream: buffered, always complete usage."""

    app = FastAPI()

    @app.post("/v1/responses")
    async def responses(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        return JSONResponse(
            {
                "id": "resp-matrix",
                "model": body.get("model", RESPONSES_MODEL),
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 2},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            }
        )

    return app


def _proxy(upstream: FastAPI, store: Store) -> FastAPI:
    return proxy.create_app(
        UPSTREAM,
        store,
        _sheet(),
        "shadow",
        transport=StreamingASGITransport(upstream),
    )


async def _post(app: FastAPI, path: str, body: dict[str, Any], headers: dict[str, str]) -> None:
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    ) as client:
        response = await client.post(path, json=body, headers=headers)
        response.raise_for_status()


async def _close(*apps: FastAPI) -> None:
    for app in apps:
        await app.state.upstream_client.aclose()


async def test_one_proxy_registers_the_whole_client_matrix() -> None:
    store = Store(":memory:")
    app = _proxy(create_mock_upstream(), store)
    try:
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        for path in CLIENT_ROUTES:
            assert path in paths, f"{path} is not metered by the default proxy app"
    finally:
        await _close(app)


async def test_every_client_lands_one_metered_row_in_one_log() -> None:
    store = Store(":memory:")
    chat_app = _proxy(create_mock_upstream(), store)
    messages_app = _proxy(create_mock_anthropic_upstream(), store)
    responses_app = _proxy(_responses_upstream(), store)

    try:
        await _post(
            chat_app,
            "/v1/chat/completions",
            {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            {"authorization": "Bearer unused"},
        )
        await _post(
            messages_app,
            "/v1/messages",
            {
                "model": MESSAGES_MODEL,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            {"anthropic-version": "2023-06-01"},
        )
        await _post(
            responses_app,
            "/v1/responses",
            {"model": RESPONSES_MODEL, "input": "hi"},
            {},
        )
    finally:
        await _close(chat_app, messages_app, responses_app)

    rows = store.rows()
    assert len(rows) == 3
    assert {row["model_used"] for row in rows} == {
        CHAT_MODEL,
        MESSAGES_MODEL,
        RESPONSES_MODEL,
    }
    assert {str(row["status"]) for row in rows} == {"200"}
