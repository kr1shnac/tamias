"""Transparent HTTP proxy in front of an OpenAI-compatible chat completions API.

Request bodies and headers are forwarded byte-for-byte (no `stream_options` are
injected unless asked for), responses are relayed chunk by chunk, upstream status
codes are passed through, and every chat completion produces one row in the
request log.  No prompt or response text is ever stored.

Shadow mode is the default and is strictly read-only with respect to the
upstream: :func:`tamias.router.decide` is called for every request, its decision
is written to the request log and to this module's logger, and the bytes handed
to the upstream are the bytes that arrived.  Only ``router_mode="active"`` may
rewrite ``model``, and only the opt-in ``inject_usage`` may add
``stream_options``.

A request's session is a salted digest, produced by
:func:`tamias.session.derive_session_id` from the request's authorization
header and its first system/developer and first user message -- or, when the
client sends ``x-tamias-session``, from that value, which is hashed the same
way rather than trusted verbatim.  Only the digest is kept, so the log records
which conversation a request belonged to without recording any of the
conversation.

:func:`create_app` also registers the Anthropic Messages route, by calling
:func:`tamias.anthropic_adapter.register_routes`, so ``POST /v1/messages`` is
logged on the same terms as ``POST /v1/chat/completions`` rather than falling
through to the catch-all relay.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from tamias import anthropic_adapter, pricing, responses_adapter, router, session
from tamias import sse as sse_module
from tamias.effort import apply_effort
from tamias.pricing import PriceSheet
from tamias.router import RouterConfig
from tamias.store import sanitize_generation_id, sanitize_project
from tamias.types import CostBreakdown, Decision, SessionState, Usage
from tamias.upstream import (
    STREAM,
    UpstreamFailure,
    classify_http_error,
)
from tamias.upstream import (
    send as upstream_send,
)

logger = logging.getLogger("tamias.proxy")

CHAT_PATH = "/v1/chat/completions"
# Backwards-compatible names for the shared session constants; the canonical
# definitions now live in tamias.session.
SESSION_HEADER = session.SESSION_HEADER
SESSION_ID_PREFIX = session.SESSION_ID_PREFIX
SESSION_ID_HEX_CHARS = session.SESSION_ID_HEX_CHARS
UNKNOWN_MODEL = "unknown"

_SESSION_CAP = 1000

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
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"accept-encoding", SESSION_HEADER}
PROJECT_HEADER = "x-tamias-project"

PASSTHROUGH_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
UPSTREAM_TIMEOUT_ENV = "TAMIAS_UPSTREAM_TIMEOUT_SECONDS"
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 600.0


class RequestLog(Protocol):
    """The slice of store.Store the proxy needs: one row per request."""

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
    ) -> int: ...


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def to_usage(raw: Any) -> Usage:
    """Build a Usage from an OpenAI usage object; anything missing stays None."""
    if not isinstance(raw, dict):
        return Usage(
            input_tokens=None,
            output_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
        )
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    # Write counts arrive either inside prompt_tokens_details (the OpenAI/
    # OpenRouter shape) or at the top level (the Anthropic-compatible shape).
    # None -- never 0 -- when genuinely absent, so a model with a nonzero
    # cache_write rate prices to UNKNOWN rather than silently undercharging.
    writes = (
        details.get("cache_creation_input_tokens")
        if isinstance(details, dict)
        else None
    )
    if writes is None:
        writes = raw.get("cache_creation_input_tokens")
    provider_cost = raw.get("cost")
    return Usage(
        input_tokens=raw.get("prompt_tokens"),
        output_tokens=raw.get("completion_tokens"),
        cached_input_tokens=cached,
        cache_write_tokens=_count(writes),
        provider_cost_usd=(
            float(provider_cost)
            if isinstance(provider_cost, int | float) and not isinstance(provider_cost, bool)
            else None
        ),
    )


def forward_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in DROPPED_REQUEST_HEADERS | {PROJECT_HEADER}
    }


def forward_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Copy end-to-end upstream response headers without proxy framing."""
    connection_values = headers.get_list("connection")
    connection_named = {
        token.strip().lower()
        for value in connection_values
        for token in value.split(",")
        if token.strip()
    }
    dropped = HOP_BY_HOP | DECODED_BODY_HEADERS | connection_named
    return {key: value for key, value in headers.items() if key.lower() not in dropped}


def upstream_timeout_seconds() -> float:
    """Return the configured upstream timeout, falling back safely to 600 seconds."""
    value = os.getenv(UPSTREAM_TIMEOUT_ENV, str(DEFAULT_UPSTREAM_TIMEOUT_SECONDS))
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS


def with_usage_included(body: dict[str, Any]) -> bytes:
    """Re-encode a streaming body asking the upstream for a usage chunk."""
    options = body.get("stream_options")
    updated = dict(options) if isinstance(options, dict) else {}
    updated["include_usage"] = True
    return json.dumps({**body, "stream_options": updated}).encode()


def with_usage_cost_included(body: dict[str, Any]) -> bytes:
    """Re-encode a request asking OpenRouter to include usage and cost."""
    usage = body.get("usage")
    updated = dict(usage) if isinstance(usage, dict) else {}
    updated["include"] = True
    return json.dumps({**body, "usage": updated}).encode()


def sse_payload(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith(b"data:") or b"[DONE]" in line:
        return None
    try:
        payload = json.loads(line[len(b"data:") :])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _model_name(value: Any) -> str:
    return value if isinstance(value, str) and value else UNKNOWN_MODEL


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}}, status_code=400
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "authentication failed", "type": "authentication_error"}},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _payload_too_large() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "request body exceeds the configured size limit",
                "type": "invalid_request_error",
            }
        },
        status_code=413,
    )


class _BodyTooLarge(Exception):
    """Internal signal that a request body grew past the configured cap."""


def _contains_body_too_large(exc: BaseException) -> bool:
    """True when ``exc`` is (or wraps, through an anyio task group) _BodyTooLarge."""
    if isinstance(exc, _BodyTooLarge):
        return True
    sub = getattr(exc, "exceptions", None)
    return isinstance(sub, (list, tuple)) and any(
        _contains_body_too_large(item) for item in sub
    )


class _LimitedReceive:
    """ASGI receive wrapper that counts bytes and stops over the cap.

    Starlette's :class:`~starlette.requests.Request` reads its body through the
    ``_receive`` channel, so replacing it here lets :func:`create_app` enforce
    a body-size cap for chunked bodies without buffering more than the limit
    or disturbing downstream ``await request.body()`` reads.
    """

    def __init__(self, receive: Any, limit: int) -> None:
        self._receive = receive
        self._limit = limit
        self._total = 0

    async def __call__(self) -> dict[str, Any]:
        message = await self._receive()
        if message["type"] == "http.request":
            self._total += len(message.get("body", b""))
            if self._total > self._limit:
                raise _BodyTooLarge()
        return message


def _upstream_failure_response() -> JSONResponse:
    """The deliberate 502-class answer for a request the upstream never served.

    Kept beside the 400 helper so the three metered routes answer transport
    failures with one consistent envelope rather than each inventing its own.
    """
    return JSONResponse(
        {"error": {"message": "upstream connection failed", "type": "upstream_error"}},
        status_code=502,
    )


class SessionRecord:
    """One session's routing state, as this proxy tracks it.

    Wraps :class:`SessionState` with ``last_switch_index``, the value of
    ``request_index`` at the last acted-on SWITCH, so hysteresis can be measured
    from the switch rather than from the start of the session.  It holds no
    conversation content: an identity string, two counters and a model name.
    """

    __slots__ = ("session_id", "request_index", "last_switch_index", "current_model")

    def __init__(
        self,
        session_id: str,
        request_index: int = 0,
        last_switch_index: int | None = None,
        current_model: str = UNKNOWN_MODEL,
    ) -> None:
        self.session_id = session_id
        self.request_index = request_index
        self.last_switch_index = last_switch_index
        self.current_model = current_model

    def as_session_state(self) -> SessionState:
        """The projection handed to :func:`tamias.router.decide`."""
        return SessionState(
            session_id=self.session_id,
            request_index=self.request_index,
            current_model=self.current_model,
        )


class SessionTable:
    """Bounded LRU of :class:`SessionRecord`, keyed by session id.

    Held in memory only: nothing here is written to the request log, so a
    restart resets every counter.  Capped so that a long-running proxy in front
    of many conversations cannot grow without bound; the least recently used
    session is dropped, and that session simply starts again at request 0.
    """

    def __init__(self, capacity: int = _SESSION_CAP) -> None:
        self.capacity = capacity
        self._records: OrderedDict[str, SessionRecord] = OrderedDict()

    def get_or_create(self, session_id: str, model: str) -> SessionRecord:
        """The session's record, marked most recently used.

        Registering on first sight is what makes ``request_index`` count
        requests: the first request of a session is planned with 0 and leaves 1
        behind it.
        """
        record = self._records.get(session_id)
        if record is not None:
            self._records.move_to_end(session_id)
            return record
        if len(self._records) >= self.capacity:
            self._records.popitem(last=False)
        record = SessionRecord(session_id=session_id, current_model=model)
        self._records[session_id] = record
        return record

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._records


def create_app(
    upstream_url: str,
    store: RequestLog,
    sheet: PriceSheet,
    router_mode: str = "shadow",
    *,
    inject_usage: bool = False,
    request_usage_cost: bool = False,
    config: RouterConfig | None = None,
    auth_token: str | None = None,
    max_body_bytes: int | None = None,
    max_concurrency: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    effort_style: str = "openrouter",
) -> FastAPI:
    if router_mode == "active":
        raise ValueError("active routing is disabled for the public alpha; use shadow or off")
    if router_mode not in ("shadow", "off"):
        raise ValueError(f"unknown router mode: {router_mode}")
    base = upstream_url.rstrip("/")
    timeout = upstream_timeout_seconds()
    client = httpx.AsyncClient(timeout=timeout, transport=transport)
    routing = router.DEFAULT_CONFIG if config is None else config
    table = SessionTable()
    session_salt = getattr(store, "session_salt", None)

    if router_mode != "off" and not routing.cheap_model:
        logger.warning(
            "router is %s but no cheap model is configured: SWITCH decisions will be "
            "recorded with no target model. Pass config=RouterConfig(cheap_model=...) "
            "to make shadow-mode savings meaningful.",
            router_mode,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()
    app = FastAPI(lifespan=lifespan)
    app.state.upstream_client = client
    app.state.upstream_timeout_seconds = timeout
    app.state.router_mode = router_mode
    app.state.router_config = routing
    app.state.inject_usage = inject_usage
    app.state.request_usage_cost = request_usage_cost
    app.state.effort_style = effort_style
    app.state.sessions = table

    if auth_token is not None:
        @app.middleware("http")
        async def require_auth(request: Request, call_next: Any) -> Response:
            header = request.headers.get("authorization", "")
            scheme, separator, credential = header.partition(" ")
            if (
                separator
                and scheme.lower() == "bearer"
                and hmac.compare_digest(credential, auth_token)
            ):
                return await call_next(request)
            return _unauthorized()

    if max_body_bytes is not None:
        @app.exception_handler(_BodyTooLarge)
        async def _body_too_large_handler(
            request: Request, exc: _BodyTooLarge
        ) -> JSONResponse:
            return _payload_too_large()

        @app.middleware("http")
        async def enforce_body_limit(request: Request, call_next: Any) -> Response:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_body_bytes:
                        return _payload_too_large()
                except ValueError:
                    pass
            request._receive = _LimitedReceive(request._receive, max_body_bytes)
            try:
                return await call_next(request)
            except BaseException as exc:
                if _contains_body_too_large(exc):
                    return _payload_too_large()
                raise

    if max_concurrency is not None:
        semaphore = asyncio.Semaphore(max_concurrency)

        @app.middleware("http")
        async def limit_concurrency(request: Request, call_next: Any) -> Response:
            async with semaphore:
                return await call_next(request)

    @app.middleware("http")
    async def project_prefix(request: Request, call_next: Any) -> Response:
        path = request.scope["path"]
        candidate: object = request.headers.get(PROJECT_HEADER)
        if path.startswith("/p/"):
            candidate, separator, suffix = path[3:].partition("/")
            if not separator:
                return _bad_request("project prefix must be /p/<name>/...")
            request.scope["path"] = "/" + suffix
            request.scope["raw_path"] = request.scope["path"].encode()
        project = sanitize_project(candidate)
        if candidate is not None and project is None:
            return _bad_request("invalid project: use [A-Za-z0-9_.-]+ (maximum 64 characters)")
        request.state.project = project
        return await call_next(request)

    def plan(session_id: str, body: dict[str, Any], model: str) -> Decision:
        """Ask the router what it would do, and log it.

        The decision is always recorded, whatever the mode.  In shadow mode it is
        marked ``persisted_only`` so that nothing downstream can mistake a
        recorded SWITCH for one that was acted on.
        """
        record = table.get_or_create(session_id, model)
        if router_mode == "off":
            decision = Decision(action="STAY", target_model=None, reason="router disabled")
        else:
            decision = router.decide(body, record.as_session_state(), routing)
            if router_mode != "active":
                decision = replace(decision, persisted_only=True)
        if router_mode == "active" and decision.action == "SWITCH":
            record.last_switch_index = record.request_index
        logger.info(
            "session=%s prior_requests=%d model=%s mode=%s decision=%s target=%s reason=%s",
            session_id,
            record.request_index,
            model,
            router_mode,
            decision.action,
            decision.target_model or "-",
            decision.reason,
        )
        return decision

    def outbound(raw: bytes, body: dict[str, Any], decision: Decision) -> bytes:
        """The exact bytes to send upstream.

        Shadow mode returns ``raw`` unchanged: the decision is recorded, never
        applied.  Only ``router_mode == "active"`` may rewrite ``model`` or add
        a router-selected reasoning effort; only the opt-in ``inject_usage`` may
        add ``stream_options``.
        """
        if (
            router_mode == "active"
            and decision.action == "SWITCH"
            and decision.target_model
            and decision.target_model != body.get("model")
        ):
            body = {**body, "model": decision.target_model}
            raw = json.dumps(body).encode()
        if router_mode == "active" and decision.target_effort is not None:
            body = apply_effort(body, decision.target_effort, style=app.state.effort_style)
            raw = json.dumps(body).encode()
        if request_usage_cost:
            raw = with_usage_cost_included(body)
            body = json.loads(raw)
        if inject_usage and body.get("stream"):
            raw = with_usage_included(body)
        return raw

    def advance(session_id: str, model_used: str) -> None:
        """Count this request and note the model it actually ran on."""
        record = table.get_or_create(session_id, model_used)
        record.request_index += 1
        record.current_model = model_used

    def record(
        session_id: str,
        model_requested: str,
        model_used: str,
        usage: Usage,
        started: float,
        status: int,
        decision: Decision,
        generation_id: object = None,
        project: object = None,
        effort_requested: str | None = None,
        effort_used: str | None = None,
        failure_stage: str | None = None,
        failure_type: str | None = None,
    ) -> None:
        cost = pricing.compute_cost(model_used, usage, sheet)
        store.log_request(
            datetime.now(UTC).isoformat(timespec="milliseconds"),
            session_id,
            model_requested,
            model_used,
            usage,
            cost,
            round((time.perf_counter() - started) * 1000.0),
            str(status),
            decision,
            price_sheet=sheet.source,
            price_simulated=sheet.simulated,
            # The response id is response-supplied, so it is reduced to a bare
            # identifier here rather than trusted: anything that is not one
            # becomes NULL instead of text in the log.
            generation_id=sanitize_generation_id(generation_id),
            project=project,
            effort_requested=effort_requested,
            effort_used=effort_used,
            decision_target_effort=decision.target_effort,
            failure_stage=failure_stage,
            failure_type=failure_type,
            router_mode=router_mode,
        )

    async def relay(upstream: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    @app.post(CHAT_PATH)
    async def chat_completions(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _bad_request("request body is not valid JSON")
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")

        session_id = session.derive_session_id(
            dict(request.headers.items()), body, session_salt
        )
        model_requested = _model_name(body.get("model"))
        streaming = bool(body.get("stream"))
        requested_reasoning = body.get("reasoning")
        effort_requested = (
            requested_reasoning.get("effort")
            if isinstance(requested_reasoning, dict)
            and isinstance(requested_reasoning.get("effort"), str)
            else body.get("reasoning_effort")
        )
        if not isinstance(effort_requested, str):
            effort_requested = None
        decision = plan(session_id, body, model_requested)
        effort_used = (
            decision.target_effort
            if router_mode == "active" and decision.target_effort is not None
            else effort_requested
        )

        outgoing = client.build_request(
            "POST",
            base + CHAT_PATH,
            content=outbound(raw, body, decision),
            headers=forward_headers(request),
        )
        started = time.perf_counter()

        if not streaming:
            try:
                upstream = await upstream_send(client, outgoing)
            except UpstreamFailure as exc:
                model_used = model_requested
                record(
                    session_id,
                    model_requested,
                    model_used,
                    Usage(None, None, None, None),
                    started,
                    502,
                    decision,
                    project=request.state.project,
                    effort_requested=effort_requested,
                    effort_used=effort_used,
                    failure_stage=exc.stage,
                    failure_type=exc.failure_type,
                )
                advance(session_id, model_used)
                return _upstream_failure_response()
            payload: Any = None
            try:
                payload = upstream.json()
            except ValueError:
                payload = None
            details = payload if isinstance(payload, dict) else {}
            model_used = _model_name(details.get("model"))
            record(
                session_id,
                model_requested,
                model_used,
                to_usage(details.get("usage")),
                started,
                upstream.status_code,
                decision,
                details.get("id"),
                request.state.project,
                effort_requested,
                effort_used,
            )
            advance(session_id, model_used)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=forward_response_headers(upstream.headers),
            )

        try:
            upstream = await upstream_send(client, outgoing, stream=True)
        except UpstreamFailure as exc:
            model_used = model_requested
            record(
                session_id,
                model_requested,
                model_used,
                Usage(None, None, None, None),
                started,
                502,
                decision,
                project=request.state.project,
                effort_requested=effort_requested,
                effort_used=effort_used,
                failure_stage=exc.stage,
                failure_type=exc.failure_type,
            )
            advance(session_id, model_used)
            return _upstream_failure_response()
        return StreamingResponse(
            _stream_chat(
                upstream,
                session_id,
                model_requested,
                decision,
                started,
                request.state.project,
                effort_requested,
                effort_used,
            ),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    async def _stream_chat(
        upstream: httpx.Response,
        session_id: str,
        model_requested: str,
        decision: Decision,
        started: float,
        project: object,
        effort_requested: str | None,
        effort_used: str | None,
    ) -> AsyncIterator[bytes]:
        seen_model: Any = None
        seen_usage: Any = None
        seen_generation_id: Any = None

        def absorb(payload: dict[str, Any]) -> None:
            nonlocal seen_model, seen_usage, seen_generation_id
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage:
                seen_usage = usage
            if seen_model is None and isinstance(payload.get("model"), str):
                seen_model = payload["model"]
            if seen_generation_id is None and isinstance(payload.get("id"), str):
                seen_generation_id = payload["id"]

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
            # The response had already started, so the client's byte stream ends
            # here; the ledger still records the cancellation as one coded row
            # rather than letting a failed request be silently absent.
            failure = UpstreamFailure(STREAM, classify_http_error(exc))
            raise
        finally:
            await upstream.aclose()
            model_used = _model_name(seen_model)
            record(
                session_id,
                model_requested,
                model_used,
                to_usage(seen_usage),
                started,
                502 if failure is not None else upstream.status_code,
                decision,
                seen_generation_id,
                project,
                effort_requested,
                effort_used,
                failure_stage=failure.stage if failure is not None else None,
                failure_type=failure.failure_type if failure is not None else None,
            )
            advance(session_id, model_used)

    @app.api_route("/{path:path}", methods=PASSTHROUGH_METHODS)
    async def passthrough(path: str, request: Request) -> Response:
        raw = await request.body()
        outgoing = client.build_request(
            request.method,
            f"{base}/{path}",
            content=raw,
            headers=forward_headers(request),
            params=request.query_params,
        )
        upstream = await client.send(outgoing, stream=True)
        return StreamingResponse(
            relay(upstream),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    # Registered last, and promoted ahead of the catch-all above, so that
    # POST /v1/messages is logged rather than relayed unlogged.  It reuses the
    # client put on app.state above, so both routes share one connection pool
    # and one lifespan.
    anthropic_adapter.register_routes(
        app, upstream_url, store, sheet, router_mode, config=routing, session_salt=session_salt
    )
    responses_adapter.register_routes(app, upstream_url, store, sheet, session_salt=session_salt)

    return app
