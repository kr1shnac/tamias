"""In-process mock of an OpenAI-compatible upstream, plus the ASGI bridge tests use.

No socket is ever opened: the tests drive the mock (and the proxy) through
``StreamingASGITransport``.  httpx's own ``ASGITransport`` awaits the whole ASGI
app before returning, so it cannot be used to observe chunk-by-chunk streaming.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

MODEL_ERROR = "mock-error-500"
MODEL_NO_USAGE = "mock-no-usage"

STREAM_TEXTS = ("alpha ", "beta ", "gamma")
CHUNK_DELAY_S = 0.05

USAGE: dict[str, Any] = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "total_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 3},
}

_COMPLETION_ID = "chatcmpl-mock"
_CREATED = 1700000000
_END = "test.transport.end"


def sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def _error(message: str, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": kind}}


def _echo(request: Request, raw: bytes) -> dict[str, Any]:
    return {
        "raw_body_b64": base64.b64encode(raw).decode("ascii"),
        "method": request.method,
        "path": request.url.path,
        "headers": {k.lower(): v for k, v in request.headers.items()},
    }


async def _sse_body(
    model: Any, usage: dict[str, Any] | None, echo: dict[str, Any]
) -> AsyncIterator[bytes]:
    base = {
        "id": _COMPLETION_ID,
        "object": "chat.completion.chunk",
        "created": _CREATED,
        "model": model,
    }
    for index, text in enumerate(STREAM_TEXTS):
        chunk = dict(base)
        if index == 0:
            chunk["echo"] = echo
        chunk["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
        yield sse(chunk)
        await asyncio.sleep(CHUNK_DELAY_S)

    final = dict(base)
    final["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    yield sse(final)

    if usage is not None:
        usage_chunk = dict(base)
        usage_chunk["choices"] = []
        usage_chunk["usage"] = usage
        yield sse(usage_chunk)

    yield b"data: [DONE]\n\n"


def create_mock_upstream() -> FastAPI:
    """POST /v1/chat/completions (buffered or SSE) plus a catch-all echo route."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse(_error("invalid json"), 400)
        if not isinstance(body, dict):
            return JSONResponse(_error("body must be an object"), 400)

        model = body.get("model")
        if model == MODEL_ERROR:
            return JSONResponse(_error("mock upstream failure", "server_error"), 500)

        echo = _echo(request, raw)
        usage = None if model == MODEL_NO_USAGE else dict(USAGE)

        if not body.get("stream"):
            payload: dict[str, Any] = {
                "id": _COMPLETION_ID,
                "object": "chat.completion",
                "created": _CREATED,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(STREAM_TEXTS)},
                        "finish_reason": "stop",
                    }
                ],
                "echo": echo,
            }
            if usage is not None:
                payload["usage"] = usage
            return JSONResponse(payload)

        options = body.get("stream_options")
        include_usage = isinstance(options, dict) and options.get("include_usage") is True
        return StreamingResponse(
            _sse_body(model, usage if include_usage else None, echo),
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


def asgi_scope(request: httpx.Request) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": request.method,
        "headers": list(request.headers.raw),
        "scheme": request.url.scheme,
        "path": request.url.path,
        "raw_path": request.url.raw_path.split(b"?")[0],
        "query_string": request.url.query,
        "server": (request.url.host, request.url.port),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }


class _ASGIByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        task: asyncio.Task[None],
        errors: list[BaseException],
        response_complete: asyncio.Event,
    ) -> None:
        self._queue = queue
        self._task = task
        self._errors = errors
        self._response_complete = response_complete

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            message = await self._queue.get()
            kind = message["type"]
            if kind == _END:
                if self._errors:
                    raise self._errors[0]
                return
            if kind != "http.response.body":
                continue
            chunk = message.get("body", b"")
            if chunk:
                yield chunk
            if not message.get("more_body", False):
                self._response_complete.set()
                return

    async def aclose(self) -> None:
        if self._task.done():
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """httpx transport that calls an ASGI app in-process and keeps streaming intact."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        errors: list[BaseException] = []
        response_complete = asyncio.Event()
        body_stream = request.stream.__aiter__()
        request_complete = False

        async def receive() -> dict[str, Any]:
            nonlocal request_complete
            if request_complete:
                await response_complete.wait()
                return {"type": "http.disconnect"}
            try:
                chunk = await body_stream.__anext__()
            except StopAsyncIteration:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": chunk, "more_body": True}

        async def send(message: dict[str, Any]) -> None:
            await queue.put(message)

        async def run() -> None:
            try:
                await self.app(asgi_scope(request), receive, send)
            except asyncio.CancelledError:
                response_complete.set()
                raise
            except BaseException as exc:  # noqa: BLE001 - re-raised to the caller
                errors.append(exc)
            response_complete.set()
            await queue.put({"type": _END})

        task = asyncio.create_task(run())

        while True:
            message = await queue.get()
            if message["type"] == _END:
                task.result()
                raise httpx.ReadError("ASGI app returned no response")
            if message["type"] == "http.response.start":
                break

        headers = [(raw_key, raw_value) for raw_key, raw_value in message.get("headers", [])]
        return httpx.Response(
            message["status"],
            headers=headers,
            stream=_ASGIByteStream(queue, task, errors, response_complete),
        )
