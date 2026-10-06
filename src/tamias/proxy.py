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

A request's session comes from the ``x-tamias-session`` header when the client
sends one, and otherwise from :func:`derive_session_id`, which hashes a
per-process salt together with the request's authorization header and its first
system/developer and first user message.  Only the digest is kept, so the log
records which conversation a request belonged to without recording any of the
conversation.

:func:`create_app` also registers the Anthropic Messages route, by calling
:func:`tamias.anthropic_adapter.register_routes`, so ``POST /v1/messages`` is
logged on the same terms as ``POST /v1/chat/completions`` rather than falling
through to the catch-all relay.
"""

from __future__ import annotations

import hashlib
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

from tamias import anthropic_adapter, pricing, responses_adapter, router
from tamias.pricing import PriceSheet
from tamias.router import RouterConfig
from tamias.store import sanitize_generation_id
from tamias.types import CostBreakdown, Decision, SessionState, Usage

logger = logging.getLogger("tamias.proxy")

CHAT_PATH = "/v1/chat/completions"
SESSION_HEADER = "x-tamias-session"
UNKNOWN_MODEL = "unknown"

# Salt for deriving session IDs.  Created once per process, so a derived ID
# identifies the same conversation for this process's lifetime but cannot be
# recomputed -- or guessed -- by anything reading the request log.
_SALT = os.urandom(16)
_SESSION_CAP = 1000
SESSION_ID_PREFIX = "auto-"
SESSION_ID_HEX_CHARS = 12
SYSTEM_ROLES = frozenset({"system", "developer"})
USER_ROLES = frozenset({"user"})

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
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"accept-encoding"}

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
    ) -> int: ...


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
    provider_cost = raw.get("cost")
    return Usage(
        input_tokens=raw.get("prompt_tokens"),
        output_tokens=raw.get("completion_tokens"),
        cached_input_tokens=cached,
        cache_write_tokens=None,
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
        if key.lower() not in DROPPED_REQUEST_HEADERS
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
    dropped = HOP_BY_HOP | connection_named
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


def _first_content(messages: list[Any], roles: frozenset[str]) -> str:
    """The content of the first message whose role is in ``roles``.

    Returns "" when no such message exists, and "" for a content shape that
    carries no flat text, so that an absent message and an unrecognised one
    hash to the same value.
    """
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in roles:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(block for block in content if isinstance(block, str))
        return ""
    return ""


def derive_session_id(headers: dict[str, str], body: dict[str, Any], salt: bytes) -> str:
    """The session a request belongs to.

    An explicit ``x-tamias-session`` header always wins.  Otherwise the id is
    derived from the salted digest of the request's identity inputs: the
    ``authorization`` header, the first system/developer message, and the first
    user message.  Every step of one conversation repeats those same three
    values, so every step of one conversation lands on one session; two
    different conversations differ in at least one of them, so they get
    different sessions.

    Only the digest is kept.  The three inputs are hashed and never stored, so
    the session id cannot be reversed back into conversation text.
    """
    explicit = headers.get(SESSION_HEADER)
    if explicit:
        return explicit

    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    identity = json.dumps(
        [
            authorization,
            _first_content(messages, SYSTEM_ROLES),
            _first_content(messages, USER_ROLES),
        ],
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(salt + identity.encode("utf-8")).hexdigest()
    return SESSION_ID_PREFIX + digest[:SESSION_ID_HEX_CHARS]


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
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    base = upstream_url.rstrip("/")
    timeout = upstream_timeout_seconds()
    client = httpx.AsyncClient(timeout=timeout, transport=transport)
    routing = router.DEFAULT_CONFIG if config is None else config
    table = SessionTable()

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
    app.state.sessions = table

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
        applied.  Only ``router_mode == "active"`` may rewrite ``model``, and only
        the opt-in ``inject_usage`` may add ``stream_options``.
        """
        if (
            router_mode == "active"
            and decision.action == "SWITCH"
            and decision.target_model
            and decision.target_model != body.get("model")
        ):
            body = {**body, "model": decision.target_model}
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

        session_id = derive_session_id(dict(request.headers.items()), body, _SALT)
        model_requested = _model_name(body.get("model"))
        streaming = bool(body.get("stream"))
        decision = plan(session_id, body, model_requested)

        outgoing = client.build_request(
            "POST",
            base + CHAT_PATH,
            content=outbound(raw, body, decision),
            headers=forward_headers(request),
        )
        started = time.perf_counter()

        if not streaming:
            try:
                upstream = await client.send(outgoing)
            except httpx.HTTPError:
                model_used = model_requested
                record(
                    session_id,
                    model_requested,
                    model_used,
                    Usage(None, None, None, None),
                    started,
                    500,
                    decision,
                )
                advance(session_id, model_used)
                return JSONResponse(
                    {"error": {"message": "upstream connection failed", "type": "upstream_error"}},
                    status_code=502,
                )
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
            )
            advance(session_id, model_used)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=forward_response_headers(upstream.headers),
            )

        upstream = await client.send(outgoing, stream=True)
        return StreamingResponse(
            _stream_chat(upstream, session_id, model_requested, decision, started),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    async def _stream_chat(
        upstream: httpx.Response,
        session_id: str,
        model_requested: str,
        decision: Decision,
        started: float,
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

        buffer = b""
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    payload = sse_payload(line)
                    if payload is not None:
                        absorb(payload)
            tail = sse_payload(buffer)
            if tail is not None:
                absorb(tail)
        finally:
            await upstream.aclose()
            model_used = _model_name(seen_model)
            record(
                session_id,
                model_requested,
                model_used,
                to_usage(seen_usage),
                started,
                upstream.status_code,
                decision,
                seen_generation_id,
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
    anthropic_adapter.register_routes(app, upstream_url, store, sheet, router_mode, config=routing)
    responses_adapter.register_routes(app, upstream_url, store, sheet)

    return app
