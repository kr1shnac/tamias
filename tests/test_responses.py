"""OpenAI Responses API metering tests, all against an in-process upstream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mock_upstream import StreamingASGITransport

from tamias import proxy, responses_adapter
from tamias.pricing import ModelPrice, PriceSheet
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
MODEL = "gpt-4.1"
FULL_USAGE = Usage(120, 45, 30, None)


class FakeStore:
    """Collects exactly the rows written by the Responses adapter."""

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
        assert self.rows, "expected a metered Responses row"
        return self.rows[-1]


@asynccontextmanager
async def closing_lifespan(application: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await application.state.upstream_client.aclose()


def sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def usage() -> dict[str, Any]:
    return {
        "input_tokens": 120,
        "output_tokens": 45,
        "input_tokens_details": {"cached_tokens": 30},
        "output_tokens_details": {"reasoning_tokens": 12},
    }


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sheet() -> PriceSheet:
    return PriceSheet(
        date="2026-10-06",
        models={MODEL: ModelPrice(input=2.0, output=8.0, cached_input=0.2)},
    )


@pytest.fixture
def upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/responses")
    async def responses(request: Request):
        raw = await request.body()
        body = json.loads(raw)
        model = body.get("model", MODEL)
        if body.get("stream") == "truncated":
            async def truncated() -> AsyncIterator[bytes]:
                yield sse("response.created", {"response": {"model": model}})
                yield b"data: {"

            return StreamingResponse(truncated(), media_type="text/event-stream")
        if body.get("stream") == "no_completed":
            async def unfinished() -> AsyncIterator[bytes]:
                yield sse(
                    "response.in_progress",
                    {"type": "response.in_progress", "response": {"usage": {"output_tokens": 999}}},
                )

            return StreamingResponse(unfinished(), media_type="text/event-stream")
        if body.get("stream"):
            async def stream() -> AsyncIterator[bytes]:
                yield sse("response.created", {"response": {"id": "resp-stream", "model": model}})
                yield sse(
                    "response.in_progress",
                    {"type": "response.in_progress", "response": {"usage": {"output_tokens": 999}}},
                )
                yield sse("response.output_text.delta", {"delta": "hello"})
                yield sse(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {"id": "resp-stream", "model": model, "usage": usage()},
                    },
                )

            return StreamingResponse(stream(), media_type="text/event-stream")
        if body.get("missing_usage"):
            return JSONResponse({"id": "resp-empty", "model": model})
        return JSONResponse({"id": "resp-complete", "model": model, "usage": usage()})

    return app


@pytest.fixture
def app(upstream: FastAPI, store: FakeStore, sheet: PriceSheet) -> FastAPI:
    return responses_adapter.register_routes(
        FastAPI(lifespan=closing_lifespan),
        UPSTREAM_URL,
        store,
        sheet,
        transport=StreamingASGITransport(upstream),
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    ) as http_client:
        yield http_client


def body(**extra: Any) -> bytes:
    return json.dumps({"model": MODEL, "input": "hi", **extra}, separators=(",", ":")).encode()


async def test_non_stream_response_records_complete_usage(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post("/v1/responses", content=body())

    assert response.status_code == 200
    assert store.last["model_requested"] == MODEL
    assert store.last["model_used"] == MODEL
    assert store.last["usage"] == FULL_USAGE
    assert store.last["status"] == "200"
    assert store.last["latency_ms"] is not None


async def test_stream_relays_bytes_in_order_and_uses_response_completed(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    expected = b"".join(
        [
            sse("response.created", {"response": {"id": "resp-stream", "model": MODEL}}),
            sse(
                "response.in_progress",
                {"type": "response.in_progress", "response": {"usage": {"output_tokens": 999}}},
            ),
            sse("response.output_text.delta", {"delta": "hello"}),
            sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"id": "resp-stream", "model": MODEL, "usage": usage()},
                },
            ),
        ]
    )

    async with client.stream("POST", "/v1/responses", content=body(stream=True)) as response:
        received = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert received == expected
    assert store.last["usage"] == FULL_USAGE


async def test_truncated_stream_logs_unknown_usage(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    async with client.stream("POST", "/v1/responses", content=body(stream="truncated")) as response:
        received = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert received.endswith(b"data: {")
    assert store.last["usage"] == Usage(None, None, None, None)


async def test_stream_without_response_completed_logs_unknown_usage(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    async with client.stream(
        "POST", "/v1/responses", content=body(stream="no_completed")
    ) as response:
        _ = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert store.last["usage"] == Usage(None, None, None, None)


async def test_missing_usage_stays_unknown_and_route_never_rewrites_model(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    requested = "gpt-no-rewrite"
    response = await client.post(
        "/v1/responses",
        content=json.dumps({"model": requested, "input": "hi", "missing_usage": True}).encode(),
    )

    assert response.status_code == 200
    assert response.json()["model"] == requested
    assert store.last["model_requested"] == requested
    assert store.last["model_used"] == requested
    assert store.last["usage"] == Usage(None, None, None, None)


async def test_proxy_app_registers_the_metered_responses_route(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = proxy.create_app(
        UPSTREAM_URL, store, sheet, transport=StreamingASGITransport(upstream)
    )
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    ) as client:
        response = await client.post("/v1/responses", content=body())

    assert response.status_code == 200
    assert store.last["usage"] == FULL_USAGE
