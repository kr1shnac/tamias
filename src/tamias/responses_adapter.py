"""Metered relay for the OpenAI Responses API.

The Responses route deliberately never asks the router to rewrite a model.  It
only relays requests and records the usage reported by the API.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from tamias import pricing, session
from tamias import sse as sse_module
from tamias.pricing import PriceSheet
from tamias.types import CostBreakdown, Decision, Usage
from tamias.upstream import STREAM, UpstreamFailure, classify_http_error
from tamias.upstream import send as upstream_send

RESPONSES_PATH = "/v1/responses"
SESSION_HEADER = session.SESSION_HEADER
UNKNOWN_MODEL = "unknown"

HOP_BY_HOP = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Headers describing the byte stream the upstream produced. httpx has already
# decoded it, so forwarding these would tell the client to decode again -- a
# client that honours `content-encoding: gzip` then gunzips plain text and
# fails the read, which surfaces as a connection error rather than a 502.
DECODED_BODY_HEADERS = frozenset({"content-encoding"})
PROJECT_HEADER = "x-tamias-project"
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"accept-encoding", PROJECT_HEADER, SESSION_HEADER}


class RequestLog(Protocol):
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
        *,
        price_sheet: str | None = None,
        price_simulated: bool | None = None,
        generation_id: object = None,
        project: object = None,
        route: str | None = None,
    ) -> int: ...


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_from_responses(raw: Any) -> Usage:
    """Read Responses usage without counting reasoning tokens twice."""
    if not isinstance(raw, dict):
        return Usage(None, None, None, None)
    input_details = raw.get("input_tokens_details")
    cached = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
    # output_tokens already includes output_tokens_details.reasoning_tokens.
    return Usage(
        _count(raw.get("input_tokens")), _count(raw.get("output_tokens")), _count(cached), None
    )


def forward_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in DROPPED_REQUEST_HEADERS
    }


def forward_response_headers(headers: httpx.Headers) -> dict[str, str]:
    connection_named = {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
        if token.strip()
    }
    dropped = HOP_BY_HOP | DECODED_BODY_HEADERS | connection_named
    return {key: value for key, value in headers.items() if key.lower() not in dropped}


def _model_name(value: Any) -> str:
    return value if isinstance(value, str) and value else UNKNOWN_MODEL


def _event_payload(line: bytes) -> dict[str, Any] | None:
    if not line.startswith(b"data:"):
        return None
    try:
        parsed = json.loads(line[5:].strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _upstream_failure_response() -> JSONResponse:
    """The deliberate 502-class answer for a request the upstream never served."""
    return JSONResponse(
        {"error": {"message": "upstream connection failed", "type": "upstream_error"}},
        status_code=502,
    )


def _promote_route(app: FastAPI, path: str) -> None:
    for index, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == path:
            if index:
                app.router.routes.insert(0, app.router.routes.pop(index))
            return


def register_routes(
    app: FastAPI,
    upstream_url: str,
    store: RequestLog,
    sheet: PriceSheet,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    session_salt: bytes | None = None,
    authorize: Callable[[Request, object], Response | None] | None = None,
) -> FastAPI:
    """Add the meter-only ``POST /v1/responses`` route to ``app``."""
    base = upstream_url.rstrip("/")
    session_salt = session_salt or getattr(store, "session_salt", None)
    client = getattr(app.state, "upstream_client", None)
    if not isinstance(client, httpx.AsyncClient):
        timeout = getattr(app.state, "upstream_timeout_seconds", 600.0)
        client = httpx.AsyncClient(timeout=timeout, transport=transport)
        app.state.upstream_client = client

    def record(
        session_id: str,
        model_requested: str,
        model_used: str,
        usage: Usage,
        started: float,
        status: int,
        generation_id: object = None,
        project: object = None,
        failure_stage: str | None = None,
        failure_type: str | None = None,
    ) -> None:
        store.log_request(
            datetime.now(UTC).isoformat(timespec="milliseconds"),
            session_id,
            model_requested,
            model_used,
            usage,
            pricing.compute_cost(model_used, usage, sheet),
            round((time.perf_counter() - started) * 1000.0),
            str(status),
            Decision(action="STAY", target_model=None, reason="responses route is meter-only"),
            price_sheet=sheet.source,
            price_simulated=sheet.simulated,
            generation_id=generation_id,
            project=project,
            failure_stage=failure_stage,
            failure_type=failure_type,
            route="responses",
        )

    async def stream_response(
        upstream: httpx.Response,
        session_id: str,
        model_requested: str,
        started: float,
        project: object,
    ) -> AsyncIterator[bytes]:
        model_used: Any = None
        completed_usage: Any = None
        generation_id: Any = None


        def absorb(payload: dict[str, Any]) -> None:
            nonlocal model_used, completed_usage, generation_id
            response = payload.get("response")
            if not isinstance(response, dict):
                return
            if isinstance(response.get("model"), str):
                model_used = response["model"]
            if isinstance(response.get("id"), str):
                generation_id = response["id"]
            if payload.get("type") == "response.completed" and response.get("usage") is not None:
                completed_usage = response["usage"]

        assembler = sse_module.SseAssembler()
        failure: UpstreamFailure | None = None
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
                events = list(assembler.feed(chunk))
                for event in events:
                    data = event.data
                    if isinstance(data, bytes):
                        try:
                            payload = json.loads(data.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            payload = None
                        if payload is not None:
                            absorb(payload)
        except httpx.HTTPError as exc:
            # The response had already started; the meter still records the
            # cancellation as one coded row rather than a silent gap.
            failure = UpstreamFailure(STREAM, classify_http_error(exc))
            raise
        finally:
            await upstream.aclose()
            record(
                session_id,
                model_requested,
                _model_name(model_used) if model_used is not None else model_requested,
                usage_from_responses(completed_usage),
                started,
                502 if failure is not None else upstream.status_code,
                generation_id,
                project,
                failure_stage=failure.stage if failure is not None else None,
                failure_type=failure.failure_type if failure is not None else None,
            )

    @app.post(RESPONSES_PATH)
    async def responses(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": {"message": "request body is not valid JSON"}}, 400)
        if not isinstance(body, dict):
            return JSONResponse({"error": {"message": "request body must be a JSON object"}}, 400)

        session_id = session.derive_session_id(
            dict(request.headers.items()), body, session_salt, wire=session.WIRE_RESPONSES
        )
        model_requested = _model_name(body.get("model"))
        if authorize is not None:
            denied = authorize(request, body.get("model"), raw)
            if denied is not None:
                return denied
        outgoing = client.build_request(
            "POST", base + RESPONSES_PATH, content=raw, headers=forward_headers(request)
        )
        started = time.perf_counter()
        if not body.get("stream"):
            try:
                upstream = await upstream_send(client, outgoing)
            except UpstreamFailure as exc:
                record(
                    session_id,
                    model_requested,
                    model_requested,
                    Usage(None, None, None, None),
                    started,
                    502,
                    None,
                    getattr(request.state, "project", None),
                    failure_stage=exc.stage,
                    failure_type=exc.failure_type,
                )
                return _upstream_failure_response()
            try:
                payload: Any = upstream.json()
            except ValueError:
                payload = {}
            details = payload if isinstance(payload, dict) else {}
            model_used = _model_name(details.get("model"))
            record(
                session_id,
                model_requested,
                model_used,
                usage_from_responses(details.get("usage")),
                started,
                upstream.status_code,
                details.get("id"),
                getattr(request.state, "project", None),
            )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=forward_response_headers(upstream.headers),
            )

        try:
            upstream = await upstream_send(client, outgoing, stream=True)
        except UpstreamFailure as exc:
            record(
                session_id,
                model_requested,
                model_requested,
                Usage(None, None, None, None),
                started,
                502,
                None,
                getattr(request.state, "project", None),
                failure_stage=exc.stage,
                failure_type=exc.failure_type,
            )
            return _upstream_failure_response()
        return StreamingResponse(
            stream_response(
                upstream,
                session_id,
                model_requested,
                started,
                getattr(request.state, "project", None),
            ),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    _promote_route(app, RESPONSES_PATH)
    return app
