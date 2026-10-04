"""In-process mock of the Anthropic Messages API.

The point of this mock is the parts of the Anthropic wire format that are easy to
get wrong: the event/`data:` frame pairing, usage that is split between
``message_start`` and ``message_delta`` instead of repeated, ``input_tokens``
that excludes cache reads and cache writes, the ``cache_creation`` split by TTL,
``ping`` frames carrying no usage, and an overloaded (529) error.

No socket is ever opened: the tests drive this mock (and the proxy) through
``mock_upstream.StreamingASGITransport``, which keeps chunk-by-chunk streaming
intact.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

MESSAGES_PATH = "/v1/messages"

MODEL_CACHED = "mock-claude-cached"
MODEL_NO_USAGE = "mock-claude-no-usage"
MODEL_PARTIAL_USAGE = "mock-claude-partial-usage"
MODEL_OVERLOADED = "mock-claude-overloaded"
MODEL_ERROR = "mock-claude-error"

MESSAGE_ID = "msg_mock_01"
STREAM_TEXTS = ("alpha ", "beta ", "gamma")
CHUNK_DELAY_S = 0.05

# What message_start reports for the prompt.  input_tokens counts only the tokens
# that missed the cache, so the prompt is really 100 + 800 + 100 = 1000 tokens.
START_USAGE: dict[str, Any] = {
    "input_tokens": 100,
    "output_tokens": 1,
    "cache_read_input_tokens": 800,
    "cache_creation_input_tokens": 100,
    "cache_creation": {"ephemeral_5m_input_tokens": 60, "ephemeral_1h_input_tokens": 40},
}

# message_delta reports the final, cumulative output count and nothing else: the
# prompt counts only ever arrived in message_start.
FINAL_OUTPUT_TOKENS = 42
FINAL_USAGE: dict[str, Any] = {"output_tokens": FINAL_OUTPUT_TOKENS}

# A usage object that never says how much of the prompt was cached, so the total
# input cannot be completed.
PARTIAL_USAGE: dict[str, Any] = {
    "input_tokens": 100,
    "output_tokens": 7,
    "cache_creation_input_tokens": 100,
    "cache_creation": {"ephemeral_5m_input_tokens": 100},
}


def sse(event: str, payload: dict[str, Any]) -> bytes:
    """One SSE frame, in the `event:` + `data:` shape Anthropic sends."""
    name = event.encode("utf-8")
    data = json.dumps(payload).encode("utf-8")
    return b"event: " + name + b"\ndata: " + data + b"\n\n"


def _error(message: str, kind: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _echo(request: Request, raw: bytes) -> dict[str, Any]:
    return {
        "raw_body_b64": base64.b64encode(raw).decode("ascii"),
        "method": request.method,
        "path": request.url.path,
        "headers": {k.lower(): v for k, v in request.headers.items()},
    }


def usage_for(model: Any) -> dict[str, Any] | None:
    """The usage object this model reports, or None to report no usage at all."""
    if model == MODEL_NO_USAGE:
        return None
    if model == MODEL_PARTIAL_USAGE:
        return dict(PARTIAL_USAGE)
    return dict(START_USAGE)


async def _sse_body(
    model: Any, usage: dict[str, Any] | None, echo: dict[str, Any]
) -> AsyncIterator[bytes]:
    start_usage = dict(usage) if usage is not None else None
    if start_usage is not None:
        start_usage.setdefault("output_tokens", 1)

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": MESSAGE_ID,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": start_usage or {},
                "echo": echo,
            },
        },
    )
    await asyncio.sleep(CHUNK_DELAY_S)

    yield sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for text in STREAM_TEXTS:
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
        await asyncio.sleep(CHUNK_DELAY_S)
        # A ping carries no usage, so nothing may be lost when it goes by.
        yield sse("ping", {"type": "ping"})

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": dict(FINAL_USAGE) if usage is not None else {},
        },
    )
    await asyncio.sleep(CHUNK_DELAY_S)
    yield sse("message_stop", {"type": "message_stop"})


def create_mock_anthropic_upstream() -> FastAPI:
    """POST /v1/messages (buffered or SSE) plus a catch-all echo route."""
    app = FastAPI()

    @app.post(MESSAGES_PATH)
    async def messages(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse(_error("invalid json", "invalid_request_error"), 400)
        if not isinstance(body, dict):
            return JSONResponse(_error("body must be an object", "invalid_request_error"), 400)

        model = body.get("model")
        if model == MODEL_OVERLOADED:
            return JSONResponse(_error("Overloaded", "overloaded_error"), 529)
        if model == MODEL_ERROR:
            return JSONResponse(_error("mock upstream failure", "api_error"), 500)

        echo = _echo(request, raw)
        usage = usage_for(model)

        if not body.get("stream"):
            payload: dict[str, Any] = {
                "id": MESSAGE_ID,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "".join(STREAM_TEXTS)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "echo": echo,
            }
            if usage is not None:
                payload["usage"] = {**usage, "output_tokens": FINAL_OUTPUT_TOKENS}
            return JSONResponse(payload)

        return StreamingResponse(
            _sse_body(model, usage, echo),
            media_type="text/event-stream",
            headers={"cache-control": "no-store"},
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def catch_all(path: str, request: Request) -> Response:
        raw = await request.body()
        return JSONResponse({"echo": _echo(request, raw)}, 200)

    return app
