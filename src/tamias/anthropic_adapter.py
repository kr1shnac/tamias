"""Anthropic Messages API support for the tamias proxy.

``POST /v1/messages`` is proxied exactly the way ``POST /v1/chat/completions``
is: the request body is forwarded byte for byte, every header is forwarded
unchanged apart from hop-by-hop headers and ``accept-encoding``, SSE responses
are relayed chunk by chunk without buffering, upstream status codes pass
straight through, and every request produces one row in the request log.  No
prompt or response text is ever stored.

Three things are Anthropic-specific:

* the credential arrives in ``x-api-key``, alongside ``anthropic-version`` and
  ``anthropic-beta``.  They are ordinary headers here, so all of them reach the
  upstream untouched;
* ``input_tokens`` EXCLUDES cache reads and cache writes, so
  :func:`usage_from_anthropic` adds the three counts together to produce the
  total that ``Usage.input_tokens`` means everywhere else in tamias.  A missing
  component leaves that total UNKNOWN (None): it is never read as zero, because
  zero would understate the prompt and the bill;
* a stream reports usage in two places.  ``message_start`` carries
  ``message.usage`` (the prompt, cache reads and cache writes) and later
  ``message_delta`` events carry ``usage`` with the cumulative
  ``output_tokens``, typically without repeating the prompt fields.
  :class:`AnthropicStreamUsage` merges them, keeping the last non-null value of
  every field.

This module does not import :mod:`tamias.proxy`, so that ``proxy`` can import
and call :func:`register_routes` without an import cycle.  The handful of
constants and the request-log Protocol it shares with ``proxy`` are restated
here for that reason.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from tamias import pricing, router
from tamias.pricing import PriceSheet
from tamias.router import RouterConfig
from tamias.types import CostBreakdown, Decision, SessionState, Usage

logger = logging.getLogger("tamias.anthropic")

MESSAGES_PATH = "/v1/messages"
SESSION_HEADER = "x-tamias-session"
DEFAULT_SESSION = "default"
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
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"accept-encoding"}

DATA_PREFIX = b"data:"

__all__ = [
    "AnthropicStreamUsage",
    "MESSAGES_PATH",
    "cost_for_anthropic",
    "forward_headers",
    "register_routes",
    "usage_from_anthropic",
]


class RequestLog(Protocol):
    """The slice of store.Store this route needs: one row per request."""

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
    ) -> int: ...


def _count(value: Any) -> int | None:
    """A token count, or None when the upstream did not report a usable one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def usage_from_anthropic(raw: Any) -> Usage:
    """Build a Usage whose ``input_tokens`` is the whole prompt.

    Anthropic's ``input_tokens`` counts only the prompt tokens that missed the
    cache, so the prompt size tamias logs is
    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``.
    If any of those three is missing the total is UNKNOWN: the sum cannot be
    finished, and reading the gap as zero would quietly understate the request.
    """
    if not isinstance(raw, dict):
        return Usage(
            input_tokens=None,
            output_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
        )

    uncached = _count(raw.get("input_tokens"))
    reads = _count(raw.get("cache_read_input_tokens"))
    writes = _count(raw.get("cache_creation_input_tokens"))
    total: int | None = None
    if uncached is not None and reads is not None and writes is not None:
        total = uncached + reads + writes

    # `cache_creation` splits the writes by TTL; 1-hour writes are billed
    # differently from 5-minute ones, so the split is read separately.
    creation = raw.get("cache_creation")
    split = creation if isinstance(creation, dict) else {}
    one_hour = _count(split.get("ephemeral_1h_input_tokens"))

    return Usage(
        input_tokens=total,
        output_tokens=_count(raw.get("output_tokens")),
        cached_input_tokens=reads,
        cache_write_tokens=writes,
        cache_write_1h_tokens=one_hour,
    )


def cost_for_anthropic(model: str, usage: Usage, sheet: PriceSheet) -> CostBreakdown:
    """Price Anthropic cache usage only when its separate rates are quoted."""
    cost = pricing.compute_cost(model, usage, sheet)
    price = sheet.get(model)
    if price is None:
        return cost

    missing_rates: list[str] = []
    if usage.cached_input_tokens not in (None, 0) and price.cached_input is None:
        missing_rates.append("cached_input_rate")
    if usage.cache_write_tokens not in (None, 0) and price.cache_write is None:
        missing_rates.append("cache_write_rate")
    if not missing_rates:
        return cost
    return CostBreakdown(
        usd=None,
        formula=f"{cost.formula}; unknown: {', '.join(missing_rates)}",
        price_sheet_date=sheet.date,
    )


class AnthropicStreamUsage:
    """Collects the token counts carried by an Anthropic SSE stream.

    Usage is spread across the stream rather than repeated in every frame:
    ``message_start`` reports the prompt, each later ``message_delta`` reports the
    output count so far, and frames such as ``content_block_delta`` and ``ping``
    report nothing at all.  Every field therefore keeps its last non-null value,
    which yields the most complete usage the stream ever offered.
    """

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}
        self.model: str | None = None
        self._buffer = b""

    def feed(self, data: bytes) -> None:
        """Absorb SSE bytes, tolerating chunk boundaries mid-line."""
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            self.feed_line(line)

    def flush(self) -> None:
        """Absorb a final line that arrived without its trailing newline."""
        line, self._buffer = self._buffer, b""
        if line.strip():
            self.feed_line(line)

    def feed_line(self, line: bytes) -> None:
        """Merge the usage of one SSE line; anything else is ignored.

        Anthropic sends an ``event:`` line before every ``data:`` line, so
        anything that is not a data frame (``event:``, comments, blank lines)
        is dropped without being parsed.
        """
        line = line.strip()
        if not line.startswith(DATA_PREFIX):
            return
        try:
            event = json.loads(line[len(DATA_PREFIX) :])
        except ValueError:
            return
        if not isinstance(event, dict):
            return

        self._remember(event.get("model"))
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self._remember(message.get("model"))
                self._merge(message.get("usage"))
        elif kind == "message_delta":
            self._merge(event.get("usage"))

    def usage(self) -> Usage:
        """The merged usage, after absorbing any unterminated final line."""
        self.flush()
        return usage_from_anthropic(self.fields)

    def _merge(self, usage: Any) -> None:
        """Keep the latest non-null value of every field the upstream reported."""
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if value is not None:
                self.fields[key] = value

    def _remember(self, model: Any) -> None:
        """Note the model the upstream said it used, the first time it says."""
        if self.model is None and isinstance(model, str) and model:
            self.model = model


def forward_headers(request: Request) -> dict[str, str]:
    """Every request header except the hop-by-hop ones and ``accept-encoding``.

    ``x-api-key``, ``anthropic-version`` and ``anthropic-beta`` pass through
    here like any other header, which is what makes the credential and the API
    version reach Anthropic exactly as the client sent them.
    """
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in DROPPED_REQUEST_HEADERS
    }


def forward_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Copy end-to-end upstream response headers without proxy framing."""
    connection_named = {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
        if token.strip()
    }
    dropped = HOP_BY_HOP | connection_named
    return {key: value for key, value in headers.items() if key.lower() not in dropped}


def _model_name(value: Any) -> str:
    return value if isinstance(value, str) and value else UNKNOWN_MODEL


def _bad_request(message: str) -> JSONResponse:
    """A 400 in Anthropic's error envelope, so clients parse it as they expect."""
    return JSONResponse(
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        status_code=400,
    )


def _promote_route(app: FastAPI, path: str) -> None:
    """Make sure ``path`` is matched before any catch-all route.

    ``proxy.create_app`` registers ``/{path:path}`` as a plain relay, so an app
    that calls :func:`register_routes` afterwards would otherwise have
    ``/v1/messages`` answered by that relay: relayed, but never logged.
    """

    routes = app.router.routes
    for index, route in enumerate(routes):
        if getattr(route, "path", None) != path:
            continue
        if index:
            routes.insert(0, routes.pop(index))
        return


def register_routes(
    app: FastAPI,
    upstream_url: str,
    store: RequestLog,
    sheet: PriceSheet,
    router_mode: str = "shadow",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    config: RouterConfig | None = None,
) -> FastAPI:
    """Add the Anthropic Messages route to ``app`` and return it.

    The upstream is ``upstream_url`` + :data:`MESSAGES_PATH`, so
    ``upstream_url`` is the provider root, exactly as for chat completions.

    An ``httpx.AsyncClient`` already on ``app.state.upstream_client`` is reused,
    so an app built by ``proxy.create_app`` shares one client and one lifespan
    across both routes.  Otherwise a client is created here (with ``transport``,
    when given) and put on ``app.state.upstream_client`` for the app's own
    lifespan to close.

    The session counter feeding ``router.decide`` belongs to this route: it
    counts the Anthropic requests of a session, and the decision is recorded but
    only applied when ``router_mode`` is ``active``.

    Nothing is injected into the request.  Anthropic reports usage in every
    stream, so there is no equivalent of the OpenAI ``stream_options`` opt-in.
    """

    base = upstream_url.rstrip("/")
    sessions: dict[str, SessionState] = {}
    routing = router.DEFAULT_CONFIG if config is None else config

    client = getattr(app.state, "upstream_client", None)
    if not isinstance(client, httpx.AsyncClient):
        timeout = getattr(app.state, "upstream_timeout_seconds", 600.0)
        client = httpx.AsyncClient(timeout=timeout, transport=transport)
        app.state.upstream_client = client

    def current(session_id: str, model: str) -> SessionState:
        """The session's state, registering it on the session's first request.

        Registering here (rather than only reading) is what makes
        ``request_index`` count requests correctly: the first request of a
        session must leave the counter at 0 while it runs and at 1 afterwards,
        so ``advance`` always has a state to increment.
        """
        state = sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id, request_index=0, current_model=model)
            sessions[session_id] = state
        return state

    def plan(session_id: str, body: dict[str, Any], model: str) -> Decision:
        """Ask the router what it would do with this request, and log it.

        The decision is always recorded, whatever the mode.  In shadow mode it is
        marked ``persisted_only`` so that nothing downstream can mistake a
        recorded SWITCH for one that was acted on.
        """
        state = current(session_id, model)
        if router_mode == "off":
            decision = Decision(action="STAY", target_model=None, reason="router disabled")
        else:
            decision = router.decide(body, state, routing)
            if router_mode != "active":
                decision = replace(decision, persisted_only=True)
        logger.info(
            "session=%s prior_requests=%d model=%s mode=%s decision=%s target=%s reason=%s",
            session_id,
            state.request_index,
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
        applied.  Only ``router_mode == "active"`` may rewrite ``model``.
        """
        if (
            router_mode == "active"
            and decision.action == "SWITCH"
            and decision.target_model
            and decision.target_model != body.get("model")
        ):
            return json.dumps({**body, "model": decision.target_model}).encode()
        return raw

    def advance(session_id: str, model_used: str) -> None:
        """Count this request and note the model it actually ran on."""
        state = current(session_id, model_used)
        sessions[session_id] = SessionState(
            session_id=session_id,
            request_index=state.request_index + 1,
            current_model=model_used,
        )

    def record(
        session_id: str,
        model_requested: str,
        model_used: str,
        usage: Usage,
        started: float,
        status: int,
        decision: Decision,
    ) -> None:
        cost = cost_for_anthropic(model_used, usage, sheet)
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
        )

    async def _stream_messages(
        upstream: httpx.Response,
        session_id: str,
        model_requested: str,
        decision: Decision,
        started: float,
    ) -> AsyncIterator[bytes]:
        collector = AnthropicStreamUsage()
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
                collector.feed(chunk)
        finally:
            await upstream.aclose()
            model_used = _model_name(collector.model)
            record(
                session_id,
                model_requested,
                model_used,
                collector.usage(),
                started,
                upstream.status_code,
                decision,
            )
            advance(session_id, model_used)

    @app.post(MESSAGES_PATH)
    async def messages(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _bad_request("request body is not valid JSON")
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")

        session_id = request.headers.get(SESSION_HEADER) or DEFAULT_SESSION
        model_requested = _model_name(body.get("model"))
        decision = plan(session_id, body, model_requested)

        outgoing = client.build_request(
            "POST",
            base + MESSAGES_PATH,
            content=outbound(raw, body, decision),
            headers=forward_headers(request),
        )
        started = time.perf_counter()

        if not body.get("stream"):
            upstream = await client.send(outgoing)
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
                usage_from_anthropic(details.get("usage")),
                started,
                upstream.status_code,
                decision,
            )
            advance(session_id, model_used)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=forward_response_headers(upstream.headers),
            )

        upstream = await client.send(outgoing, stream=True)
        return StreamingResponse(
            _stream_messages(upstream, session_id, model_requested, decision, started),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    _promote_route(app, MESSAGES_PATH)
    return app
