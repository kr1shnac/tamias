"""Transparent HTTP proxy in front of an OpenAI-compatible chat completions API.

Request bodies and headers are forwarded byte-for-byte (no `stream_options` are
injected unless asked for), responses are relayed chunk by chunk, upstream status
codes are passed through, and every chat completion produces one row in the
request log.  No prompt or response text is ever stored.

Shadow mode is the default and is strictly read-only with respect to the
upstream: :func:`tamias.router.decide` is called for every request, its decision
is written to the request log and to this module's logger, and the bytes handed
to the upstream are the bytes that arrived.  Only ``router_mode="active"`` may
rewrite ``model``; only ``effort_mode="apply"`` may add a reasoning
effort, and only for a model the capability registry vouches for; only
the opt-in ``inject_usage`` may add ``stream_options``.

``active`` is opt-in and fail-closed.  It refuses to start unless its cheap
model is configured, listed in the capability registry, able to call tools,
and priced by the price sheet (:func:`_verify_active_target`), and a switch
the caller's own key may not use is suppressed rather than applied (see
:func:`tamias.router.applied_target`).

A request's session is a salted digest, produced by
:func:`tamias.session.derive_session_id` from the request's authorization
header and its first system/developer and first user message -- or, when the
client sends ``x-tamias-session``, from that value, which is hashed the same
way rather than trusted verbatim.  Only the digest is kept, so the log records
which conversation a request belonged to without recording any of the
conversation.

When ``key_store`` is given, the gateway authenticates each request with a
company key (``Authorization: Bearer vk-...``) from :mod:`tamias.keys`,
attaches that identity to the request, and refuses any model the key's
allowlist does not name.  The key's project, when it has one, overrides a
client-supplied project header, so a caller cannot relabel its spend.

A key may also carry a budget, and then the gateway enforces it: before a
request is forwarded it reserves ``request_ceiling_usd`` and refuses with 402
once the budget could not afford it, and when the request is logged the real
computed cost settles the reservation (:mod:`tamias.budget`).  A budget is only
held against spend the gateway measures, so a budgeted key is refused the
unpriced passthrough route when it names a model, and refused a model the price
sheet cannot price.

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
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from tamias import (
    anthropic_adapter,
    candidates,
    pricing,
    responses_adapter,
    router,
    session,
)
from tamias import limits as limits_mod
from tamias import manifest as manifest_mod
from tamias import providers as providers_mod
from tamias import sse as sse_module
from tamias.budget import (
    CURRENT_KEY_ID,
    DEFAULT_REQUEST_CEILING_USD,
    RATE_WINDOW_SECONDS,
    BudgetedLog,
    Reserve,
    estimate_tokens,
    parse_usd,
)
from tamias.effort import EFFORT_MODES, apply_effort
from tamias.keys import Key, KeyStore
from tamias.pricing import PriceSheet
from tamias.registry import Registry, empty_registry
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

# The routes whose cost the gateway measures and can therefore hold a budget
# against.  The passthrough below is deliberately absent: it relays an arbitrary
# provider body the gateway does not price.
METERED_PATHS = frozenset(
    (CHAT_PATH, anthropic_adapter.MESSAGES_PATH, responses_adapter.RESPONSES_PATH)
)

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
PROJECT_HEADER = "x-tamias-project"
#: A client's "leave this request alone" signal.  Active routing is opt-in and
#: bounded by three gates, but it still rewrites a model the *caller* named;
#: a caller that knows the step is a hard one must be able to say so without
#: turning the router off.  It is per-request and tamias-local: it is dropped
#: before the upstream request is sent, exactly like the session header.
PIN_HEADER = "x-tamias-pin"

#: What counts as "yes".  Anything else is ignored, so a stray
#: ``x-tamias-pin: maybe`` can never silently disable routing.
PIN_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {
    "accept-encoding",
    SESSION_HEADER,
    PIN_HEADER,
}


def pin_requested(headers: Mapping[str, str]) -> bool:
    """Does the client ask for this request to stay on the model it named?"""
    value = headers.get(PIN_HEADER, "")
    return value.strip().lower() in PIN_TRUE_VALUES

PASSTHROUGH_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# The count-only passthrough row still needs a non-NULL model (the log's schema
# requires one).  A read that names no model is recorded under this placeholder,
# never under a model name the caller did not send.
PASSTHROUGH_MODEL = "-"
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
        route: str | None = None,
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
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in DROPPED_REQUEST_HEADERS | {PROJECT_HEADER}
    }
    key = getattr(request.state, "key", None)
    key_id = getattr(key, "key_id", None) if key is not None else None
    if isinstance(key_id, str) and key_id.startswith("vk-"):
        headers.pop("authorization", None)
        headers.pop("Authorization", None)
    return headers


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


#: The longest cooldown a provider hint may impose.  A provider is free to ask
#: for a day off; it is not free to park a model forever because it sent a
#: malformed or epoch-valued header.
MAX_RETRY_AFTER_SECONDS = 86_400

# ``GET /limits`` is polled by the dashboard, so its model lists are capped by
# default while the counts stay complete.  A discovered catalogue can hold
# hundreds of models; listing all of them once a second would cost more than
# the requests the endpoint reports on.
DEFAULT_LIMITS_MODELS = 25
MAX_LIMITS_MODELS = 500

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def _duration_seconds(text: str) -> float | None:
    """Seconds from a Go-style duration (``6m0s``) or a bare number."""
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(value + unit for value, unit in parts) != text:
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(value) * scale[unit] for value, unit in parts)


def _numeric_retry_after(headers: httpx.Headers) -> float | None:
    """A cooldown from the rate-limit headers providers send instead of ``Retry-After``.

    Only values that are unambiguously a wait are used.  An epoch-valued
    ``x-ratelimit-reset`` is not guessed at: turning it into a wait needs the
    server's clock, and a wrong guess would park a healthy model.
    """
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        seconds = _duration_seconds(headers.get(name, ""))
        if seconds is not None and 0 < seconds <= MAX_RETRY_AFTER_SECONDS:
            return seconds
    return None


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


def _named_model(model: object) -> str | None:
    """A model name worth acting on: a non-empty string that is not UNKNOWN."""
    if isinstance(model, str) and model and model != UNKNOWN_MODEL:
        return model
    return None


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


def _forbidden_model(model: str, allowed: tuple[str, ...]) -> JSONResponse:
    """403 for a key whose allowlist does not name this model."""
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"model {model!r} is not allowed for this key "
                    f"(allowed: {', '.join(allowed)})"
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=403,
    )


def _unknown_provider(model: str, providers: tuple[str, ...]) -> JSONResponse:
    """400 for a model no configured provider serves.

    Refusing is the point: forwarding an unclaimed model to whichever upstream
    happened to be last would spend another provider's credit under a model id
    that provider does not offer.
    """
    known = ", ".join(providers) or "none"
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"no configured provider serves model {model!r} "
                    f"(providers: {known}). Add the provider to ~/.tamias/providers.toml "
                    "or the model to ~/.tamias/models.toml."
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=400,
    )


def _payment_required(key: Key, ceiling: Decimal, reserve: Reserve) -> JSONResponse:
    """402 for a key that cannot afford one more request.

    The numbers are the gateway's own ledger -- settled spend, money held by
    in-flight requests, and what one more request would need -- so the refusal
    states arithmetic the caller and the operator can both check.  ``model_cap``
    names the model's own sub-cap; ``budget`` is the key's total.
    """
    what = "budget" if reserve.reason == "budget" else f"{reserve.model!r} sub-cap"
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"{what} exhausted for key {key.key_id}: {reserve.spent_usd} spent, "
                    f"{reserve.reserved_usd} reserved, {reserve.remaining_usd} left, "
                    f"{ceiling} needed for one request"
                ),
                "type": "insufficient_quota",
            }
        },
        status_code=402,
    )


def _rate_limited(key: Key, reserve: Reserve) -> JSONResponse:
    """429 for a key that has spent its requests or tokens for the minute.

    A rolling window is refilled continuously, so the only honest advice is the
    window's own length; ``Retry-After`` says exactly that.
    """
    unit = "requests" if reserve.reason == "rpm" else "tokens"
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"rate limit for key {key.key_id}: {reserve.used} {unit} in the last "
                    f"{RATE_WINDOW_SECONDS}s, limit {reserve.limit}"
                ),
                "type": "rate_limit_exceeded",
            }
        },
        status_code=429,
        headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
    )


def _policy_absent() -> JSONResponse:
    """404: this gateway was not started with a signed policy manifest."""
    return JSONResponse(
        {
            "error": {
                "message": "no policy manifest is configured on this gateway",
                "type": "not_found_error",
            }
        },
        status_code=404,
    )


def _policy_unavailable(detail: str) -> JSONResponse:
    """503: a manifest is configured but no longer verifies (expired, altered)."""
    return JSONResponse(
        {
            "error": {
                "message": f"policy manifest is not valid: {detail}",
                "type": "service_unavailable",
            }
        },
        status_code=503,
    )


def _policy_forbidden(bound: str, caller: object) -> JSONResponse:
    """403: the manifest belongs to another project than the caller's key."""
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"policy manifest is for project {bound!r}, but this key "
                    f"belongs to {caller!r}"
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=403,
    )


def _unmetered_forbidden(model: str) -> JSONResponse:
    """403 when a budgeted key names a model on the unlogged passthrough route."""
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"model {model!r} cannot be reached through the passthrough route by "
                    "a key with a budget: the gateway bounds spend only on its metered "
                    "routes (/v1/chat/completions, /v1/messages, /v1/responses)"
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=403,
    )


def _budget_needs_model(key: Key) -> JSONResponse:
    """403 when a budgeted key reaches a metered route without naming a model."""
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"key {key.key_id} has a budget, so the request must name a model "
                    "the price sheet can price"
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=403,
    )


def _unpriced_forbidden(model: str) -> JSONResponse:
    """403 when a budgeted key names a model no price can be computed for."""
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"model {model!r} cannot be priced by the gateway's price sheet, so "
                    "a key with a budget cannot be held to one by reaching it"
                ),
                "type": "invalid_request_error",
            }
        },
        status_code=403,
    )


def _body_model(raw: bytes) -> str | None:
    """The model named in a JSON request body, or None.

    Used by the passthrough route, which may carry any provider's body: when
    there is a string ``model`` field it must clear the caller's allowlist, and
    when there is not, there is nothing to check.
    """

    def _find_model(obj: object, depth: int = 0) -> str | None:
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for key in ("model", "model_name", "model_id"):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    return val
            for val in obj.values():
                found = _find_model(val, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _find_model(item, depth + 1)
                if found is not None:
                    return found
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model:
            return model
    return _find_model(payload)


def _json_object(raw: bytes) -> dict[str, Any]:
    """A JSON object body, or {} when the body is not one.

    The passthrough's session derivation needs a mapping to group by, and
    off-route traffic may carry anything, so a non-object body is treated as
    empty rather than raising.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _verify_active_universe(
    routing: RouterConfig,
    capabilities: Registry,
    sheet: PriceSheet,
    catalogue: providers_mod.ProviderCatalogue,
    limits: limits_mod.LimitLedger,
) -> None:
    """Refuse to start active routing with nothing it is allowed to switch to.

    A provider catalogue makes the pool dynamic, so there is no fixed target list
    to check.  The same four conditions are still enforced -- registry membership,
    tool support, a known price, a reachable provider -- but they are enforced per
    request by the three routing gates.  This start-up check proves the gates can
    admit *something* at all, so active mode cannot come up with an empty pool and
    a promise it can never keep.
    """
    pool = candidates.build_pool(
        registry=capabilities,
        sheet=sheet,
        catalogue=catalogue,
        limits=limits,
        needs=candidates.Needs(tools=True),
        require_priced=True,
    )
    if not pool:
        raise ValueError(
            "active routing has no model that passes the routing gates: "
            f"{pool.reason()}. Each candidate must be in the registry, support "
            "tool calls, be priced by the sheet, and belong to a provider with a "
            "credential."
        )
    if routing.pool:
        allowed = candidates.pool_for_models(
            routing.pool,
            registry=capabilities,
            sheet=sheet,
            catalogue=catalogue,
            limits=limits,
            needs=candidates.Needs(tools=True),
            require_priced=True,
        )
        if not allowed:
            raise ValueError(
                f"active routing names a cheap pool ({', '.join(routing.pool)}) where "
                f"no member passes the routing gates: {allowed.reason()}"
            )


def _verify_active_target(
    routing: RouterConfig, capabilities: Registry, sheet: PriceSheet
) -> None:
    """Refuse to start active routing that cannot be honoured.

    Active is the only mode where the gateway's model choice is real, so every
    way the choice could be wrong is a startup error rather than a surprise at
    request time:

    * **no target.**  A SWITCH decision would have nothing to switch to, and the
      mode's entire promise would be silently empty.
    * **a target outside the registry.**  Routing may never pick a model the
      deployment has not vouched for; the registry is where it says which models
      exist at all.
    * **a target that cannot call tools.**  Every routed step is a tool-loop
      step, so a target that does not support tool calls would break the loop it
      was chosen to continue.
    * **an unpriced target.**  Cost arithmetic that cannot price a request
      settles as UNKNOWN, which would charge a budget the reservation ceiling
      and leave the run's own cost unreportable.

    Every member of the pool is checked, not just the primary: a pool is only a
    safety feature if every model it can pick is safe.
    """
    targets = routing.pool
    if not targets:
        raise ValueError(
            "active routing needs a cheap model: pass RouterConfig(cheap_model=...) "
            "or --cheap-model MODEL"
        )
    for target in targets:
        info = capabilities.get(target)
        if info is None:
            raise ValueError(
                f"active routing target {target!r} is not in the capability registry: "
                "add it to the registry file (--registry) so the router cannot pick a "
                "model the deployment has not vouched for"
            )
        if not info.tools:
            raise ValueError(
                f"active routing target {target!r} does not support tool calls "
                "(its registry entry has tools = false); every routed step is a "
                "tool-loop step, so the target must be able to continue the loop"
            )
        if not pricing.can_price(target, sheet):
            raise ValueError(
                f"active routing target {target!r} is not priced by the price sheet: "
                "an unpriced request settles as UNKNOWN, which a budget cannot "
                "enforce and a report cannot price"
            )


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
    effort_mode: str = "off",
    registry: Registry | None = None,
    key_store: KeyStore | None = None,
    request_ceiling_usd: object = None,
    manifest: dict[str, Any] | None = None,
    manifest_key: object = None,
    trusted_hosts: object = None,
    enforce_host: bool = False,
    providers: providers_mod.ProviderCatalogue | None = None,
    limits: limits_mod.LimitLedger | None = None,
) -> FastAPI:
    if router_mode not in ("shadow", "off", "active"):
        raise ValueError(
            f"unknown router mode: {router_mode} (allowed: shadow, off, active)"
        )
    if effort_mode not in EFFORT_MODES:
        allowed = ", ".join(EFFORT_MODES)
        raise ValueError(f"unknown effort mode: {effort_mode} (allowed: {allowed})")
    base = upstream_url.rstrip("/")
    timeout = upstream_timeout_seconds()
    client = httpx.AsyncClient(timeout=timeout, transport=transport)
    routing = router.DEFAULT_CONFIG if config is None else config
    catalogue = providers if providers is not None else providers_mod.empty_catalogue()
    limit_ledger = limits if limits is not None else limits_mod.empty_ledger()
    if effort_mode != "off" and not routing.effort_policy:
        # The mode is the operator's switch; the config only shapes the
        # policy.  meter and apply both need the policy on so decide()
        # computes a would-be effort: otherwise there is nothing to
        # record or send.
        routing = replace(routing, effort_policy=True)
    capabilities = registry if registry is not None else empty_registry()
    if len(catalogue) and len(capabilities):
        # The registry's ``provider`` column is authoritative for ids whose own
        # prefix does not name a provider, so the catalogue learns it here rather
        # than making every caller remember to merge.
        catalogue = providers_mod.catalogue_from_registry(catalogue, capabilities)
    if router_mode == "active":
        # Everything active mode needs to be sure of, checked once at start.
        if len(catalogue) and len(capabilities):
            _verify_active_universe(routing, capabilities, sheet, catalogue, limit_ledger)
        else:
            _verify_active_target(routing, capabilities, sheet)
    # One ledger per app, sharing the key store's connection.  ``metered`` is the
    # request log every route writes through; it settles the caller's reservation
    # at the one point the response's real cost is known.
    ledger = key_store.budget() if key_store is not None else None
    ceiling = parse_usd(request_ceiling_usd)
    if ceiling is None:
        ceiling = DEFAULT_REQUEST_CEILING_USD
    if ceiling <= 0:
        raise ValueError("request_ceiling_usd must be a positive USD amount")
    metered: RequestLog = store if ledger is None else BudgetedLog(store, ledger)
    table = SessionTable()
    session_salt = getattr(store, "session_salt", None)

    if router_mode != "off" and not routing.cheap_model and not len(catalogue):
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
    app.state.effort_mode = effort_mode
    app.state.registry = capabilities
    app.state.key_store = key_store
    app.state.providers = catalogue
    app.state.limits = limit_ledger
    # The verified policy manifest this gateway serves at GET /policy (D12),
    # and the optional pinned public key its signature must chain to.
    app.state.manifest = manifest
    app.state.manifest_key = manifest_key
    app.state.budget_ledger = ledger
    app.state.request_ceiling_usd = ceiling
    app.state.sessions = table

    # DNS-rebinding / cross-site guard (council run 20261020-1500 item [14]).
    # A malicious page can point a name it controls at 127.0.0.1 and drive the
    # proxy from the victim's browser.  Browsers mark such requests with a
    # foreign ``Origin`` (and a foreign ``Host``); both are refused here.  The
    # Origin check is always on — it never rejects a non-browser client, which
    # sends no Origin.  The Host allowlist is opt-in (``enforce_host``) because
    # an in-process test client legitimately uses a synthetic Host.
    @app.middleware("http")
    async def reject_cross_site(request: Request, call_next: Any) -> Response:
        from tamias.security import origin_is_allowed

        origin = request.headers.get("origin")
        if not origin_is_allowed(origin, trusted_hosts):
            return JSONResponse(
                status_code=403,
                content={"error": {"message": "cross-origin request refused", "type": "forbidden"}},
            )
        if enforce_host:
            from tamias.security import host_is_allowed

            if not host_is_allowed(request.headers.get("host"), trusted_hosts):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "message": (
                                "Host header not allowed; bind loopback or pass --trusted-host"
                            ),
                            "type": "forbidden",
                        }
                    },
                )
        return await call_next(request)

    if auth_token is not None or key_store is not None:
        @app.middleware("http")
        async def require_auth(request: Request, call_next: Any) -> Response:
            header = request.headers.get("authorization", "")
            scheme, separator, credential = header.partition(" ")
            if not separator or scheme.lower() != "bearer":
                return _unauthorized()
            if auth_token is not None and hmac.compare_digest(credential, auth_token):
                # The operator's shared token carries no identity of its own.
                request.state.key = None
                return await call_next(request)
            if key_store is not None:
                key = key_store.authenticate(credential)
                if key is not None:
                    request.state.key = key
                    if key.project is not None:
                        # The key's project is authoritative: a client cannot
                        # relabel its spend with a header it chose.
                        request.state.project = key.project
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

    def route_context(body: dict[str, Any], model_requested: str) -> dict[str, Any]:
        """The three routing gates, as keyword arguments for ``decide``.

        Every route asks the same question -- what may this request run on, what
        is locked out, and what is each model worth -- so the answer is computed
        once here and shared.  A route that re-derived it would drift: it could
        forget the requested model's own quality, or move off a locked-out model
        without telling the router it was limited.
        """
        pool = pool_for(body)
        # No registry means no capability facts, so the static configured pool
        # stays authoritative rather than being replaced by an empty ranked list.
        ranked = pool.ids() if len(capabilities) else None
        qualities = {candidate.model: candidate.quality for candidate in pool.candidates}
        costs = {candidate.model: candidate.cost for candidate in pool.candidates}
        info = capabilities.get(model_requested)
        if info is not None:
            qualities.setdefault(model_requested, candidates.quality_of(info))
        if model_requested:
            # The requested model may sit outside the configured cheap pool.
            # Its own price is what decides whether a pool member is actually
            # cheaper, so price it here even though it cannot be a target.
            costs.setdefault(
                model_requested,
                candidates.blended_cost(sheet.get(model_requested)),
            )
        limited: tuple[str, ...] = ()
        if model_requested:
            locked = limit_ledger.get(provider_of(model_requested), model_requested)
            if locked is not None:
                limited = (model_requested,)
        return {
            "ranked": ranked,
            "limited": limited,
            "qualities": qualities,
            "costs": costs,
        }

    def plan(
        session_id: str,
        body: dict[str, Any],
        model: str,
        *,
        pinned: bool = False,
        ranked: tuple[str, ...] | None = None,
        limited: tuple[str, ...] = (),
        qualities: dict[str, float] | None = None,
        costs: dict[str, float | None] | None = None,
    ) -> Decision:
        """Ask the router what it would do, and log it.

        The decision is always recorded, whatever the mode.  In shadow mode it is
        marked ``persisted_only`` so that nothing downstream can mistake a
        recorded SWITCH for one that was acted on.

        A client that names ``x-tamias-pin`` keeps the model it asked for: the
        decision is ``STAY`` with a reason that says why, in every mode including
        ``active``.  The router never sees the request, so no quality floor, cache
        or hysteresis argument can override the caller -- a pin is a statement
        about the step, and only the caller knows about the step.
        """
        record = table.get_or_create(session_id, model)
        if router_mode == "off":
            decision = Decision(action="STAY", target_model=None, reason="router disabled")
        elif pinned:
            decision = Decision(
                action="STAY",
                target_model=None,
                reason=f"pinned by client: kept {model}",
            )
        else:
            decision = router.decide(
                body,
                record.as_session_state(),
                routing,
                ranked=ranked,
                limited=limited,
                qualities=qualities,
                costs=costs,
            )
            if router_mode != "active" and not decision.availability:
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

    def resolve_effort(model: str, decision: Decision) -> str | None:
        """The effort to actually send, or None when none should be sent.

        Only ``effort_mode == "apply"`` ever sends an effort, and only for a
        model the registry vouches for: an unknown model, or one whose entry
        does not list reasoning, is never sent an effort field, so apply mode
        cannot make a request the model is known to reject.  A requested level
        the model does not list is downgraded to the nearest supported one by
        :meth:`~tamias.registry.Registry.resolve_effort`.
        """
        if effort_mode != "apply" or decision.target_effort is None:
            return None
        resolved = capabilities.resolve_effort(model, decision.target_effort)
        if resolved is None:
            logger.info(
                "effort %s not applied: %s is not in the registry or does not "
                "support reasoning",
                decision.target_effort,
                model,
            )
        return resolved

    def outbound(
        raw: bytes,
        body: dict[str, Any],
        routed_model: str | None,
        applied_effort: str | None = None,
    ) -> bytes:
        """The exact bytes to send upstream.

        ``routed_model`` is the model an active decision applies, or None when
        the request keeps the model it named (every shadow and off request, and
        any switch that was suppressed), in which case ``raw`` is returned
        unchanged.  Only an ``applied_effort`` resolved for this model
        (effort_mode ``apply``) adds the reasoning field; only the opt-in
        ``inject_usage`` may add ``stream_options``.
        """
        if routed_model is not None:
            body = {**body, "model": routed_model}
            raw = json.dumps(body).encode()
        if applied_effort is not None:
            body = apply_effort(body, applied_effort, style=app.state.effort_style)
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
        route: str = "chat",
    ) -> None:
        cost = pricing.compute_cost(model_used, usage, sheet)
        metered.log_request(
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
            route=route,
        )

    def budget_gate(
        request: Request, key: Key, named: str | None, body: bytes | None
    ) -> Response | None:
        """Reserve one request against the key's limits, or refuse it.

        Called only for a key that carries a limit.  The rules are deliberately
        narrow, because a limit is a promise about what the gateway measures: a
        metered route must name a model when money is at stake, and the
        passthrough -- which the gateway logs as a count-only row but never
        prices -- may not name a model at all.  ``named`` is the model the
        request will actually run on, which under active routing is the
        router's target rather than the one the caller typed.
        """
        assert ledger is not None  # callers only reach here with a key store
        limits = key.limits
        metered = request.scope["path"] in METERED_PATHS
        if named is None:
            # A metered route with no model cannot be priced; a passthrough read
            # that names no model has nothing to charge.
            return _budget_needs_model(key) if metered and limits.priced else None
        if not metered:
            return _unmetered_forbidden(named)
        if limits.priced and not pricing.can_price(named, sheet):
            return _unpriced_forbidden(named)
        # Settling is driven by this variable, read when the request is logged.
        CURRENT_KEY_ID.set(key.key_id)
        reserve = ledger.try_reserve(
            key.key_id,
            limits=limits,
            model=named,
            ceiling_usd=ceiling,
            token_estimate=estimate_tokens(body) if limits.tpm is not None else 0,
        )
        if reserve.allowed:
            return None
        if reserve.reason in ("rpm", "tpm"):
            return _rate_limited(key, reserve)
        return _payment_required(key, ceiling, reserve)

    def allowed_models_for(request: Request) -> tuple[str, ...] | None:
        """The models this caller's key may use, or None when unrestricted.

        A routing target outside this set must not be applied: the caller
        would then be running on a model their key forbids, which is exactly
        what the allowlist exists to prevent.  ``None`` means "no allowlist",
        which is every deployment that does not use company keys.
        """
        key = getattr(request.state, "key", None)
        if key is None or key.unrestricted:
            return None
        return key.models

    def authorize(
        request: Request,
        model: object,
        body: bytes | None = None,
        *,
        effective: str | None = None,
    ) -> Response | None:
        """None when the caller may use the model this request will run on.

        Two independent policies are applied, so they bind the chat, messages,
        responses and passthrough routes alike rather than one handler's idea of
        "the model":

        * the key's model allowlist (403), applied to the model the caller named
          *and*, when it differs, to the model the gateway will actually run;
        * the key's limits -- budget, per-model sub-caps, requests and tokens per
          minute -- which reserve the request before it is forwarded and refuse
          with 402 or 429 when it would take the key past one of them.  A
          per-model sub-cap therefore measures the model that runs, not the one
          that was asked for.

        ``body`` is the raw request bytes, used only to estimate a prompt's size
        against ``tpm``.  ``effective`` is the routing target when an active
        decision will rewrite the request, else None.  A key with an empty
        allowlist and no limits is unaffected by all of this.
        """
        key = getattr(request.state, "key", None)
        if key is None:
            return None
        named = _named_model(model)
        target = _named_model(effective) or named
        if not key.unrestricted and named is not None and named not in key.models:
            return _forbidden_model(named, key.models)
        if (
            not key.unrestricted
            and target is not None
            and target != named
            and target not in key.models
        ):
            return _forbidden_model(target, key.models)
        if not key.limited or ledger is None:
            return None
        return budget_gate(request, key, target, body)

    @app.get("/policy")
    async def policy_manifest(request: Request) -> Response:
        """Serve the signed policy manifest for the caller's project (D12).

        The policy is *verified*, not merely stored: an expired or altered
        manifest is refused here, so a gateway that loaded one at startup stops
        serving it the moment its expiry passes.  A manifest bound to one
        project is served only to a caller the key store placed in that project;
        the key's project is authoritative, so a client cannot relabel its way
        to another project's policy.
        """
        stored = getattr(app.state, "manifest", None)
        if stored is None:
            return _policy_absent()
        bound = stored.get("project") or ""
        caller = getattr(request.state, "project", None)
        if bound and caller != bound:
            return _policy_forbidden(bound, caller)
        try:
            verified = manifest_mod.verify_manifest(
                stored,
                expected_project=bound or None,
                trusted_public_key=getattr(app.state, "manifest_key", None),
            )
        except manifest_mod.ManifestError as exc:
            return _policy_unavailable(str(exc))
        return JSONResponse(verified)

    @app.get("/limits")
    async def limits_status(
        models: int = Query(
            DEFAULT_LIMITS_MODELS,
            ge=0,
            le=MAX_LIMITS_MODELS,
            description=(
                "how many candidates and rejections to list; the counts are "
                "always complete"
            ),
        )
    ) -> Response:
        """What may run right now, why the rest may not, and what is cooling down.

        Read-only and credential-free: a provider is reported as configured or
        not, never with the secret or the environment variable that holds it.
        The dashboard and ``tamias doctor`` both read this.

        The pool can hold hundreds of models once several providers are
        discovered, so the lists are capped and the ``pool`` block stays
        truthful about the totals.  A monitoring loop polls this endpoint; an
        unbounded response would make each poll cost more than the request it
        is reporting on.
        """
        pool = pool_for({})
        cooling = limit_ledger.active()
        shown = pool.candidates[:models]
        rejected = pool.rejected[:models]
        return JSONResponse(
            {
                "providers": [
                    {
                        "name": provider.name,
                        "base_url": provider.base_url,
                        "configured": provider.configured,
                        "key_source": (
                            "none"
                            if provider.auth == "none"
                            else "environment"
                            if provider.key_source.startswith("env:")
                            else "command"
                        ),
                    }
                    for provider in catalogue.all()
                ],
                "unconfigured": list(catalogue.unconfigured()),
                # Provider-published request quota.  Only a provider that names
                # its own remaining count appears here; tamias never estimates
                # one, so an empty list means "no provider said", not "no limit".
                "quota": [
                    {
                        "provider": quota.provider,
                        "remaining_requests": quota.remaining,
                        "resets_in": (
                            None
                            if quota.resets_in is None
                            else int(quota.resets_in)
                        ),
                    }
                    for quota in limit_ledger.quotas()
                ],
                "cooling_down": [
                    {
                        "provider": limit.provider,
                        "model": limit.model,
                        "status": limit.status,
                        "reason": limit.reason,
                        "remaining_seconds": int(limit.remaining()),
                        "strikes": limit.strikes,
                    }
                    for limit in cooling
                ],
                "pool": {
                    "considered": pool.considered,
                    "candidates": len(pool),
                    "refused": len(pool.rejected),
                    "listed": len(shown),
                    "truncated": len(shown) < len(pool) or len(rejected) < len(pool.rejected),
                    "gates": pool.rejection_counts(),
                },
                "candidates": [
                    {
                        "model": candidate.model,
                        "provider": candidate.provider,
                        "quality": candidate.quality,
                        "cost_per_mtok": candidate.cost,
                        "context_window": candidate.context_window,
                        "tools": candidate.tools,
                        "reasoning": candidate.reasoning,
                    }
                    for candidate in shown
                ],
                "refused": [
                    {"model": rejection.model, "gate": rejection.gate, "reason": rejection.reason}
                    for rejection in rejected
                ],
            }
        )

    async def relay(upstream: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    # -- multi-provider routing ------------------------------------------------
    # Tamias is a layer above the providers, never one of them: a model id picks
    # the provider, that provider supplies the URL, and its credential replaces
    # whatever the client used to reach this proxy.

    def root_for(model: str) -> str:
        """The provider root that serves ``model``, or the legacy single upstream."""
        if not len(catalogue):
            return base
        provider = catalogue.provider_for(model)
        return provider.root if provider is not None else ""

    def headers_for(model: str, request: Request) -> dict[str, str]:
        headers = forward_headers(request)
        if not len(catalogue):
            return headers
        provider = catalogue.provider_for(model)
        if provider is None:
            return headers
        return catalogue.apply_auth(provider.name, headers)

    def remember_transport_failure(model: str) -> None:
        """Cool a model down after a connection that never answered.

        ``setup``-stage failures only: a stream that dies halfway has already
        produced tokens, so the model is not unreachable, just unlucky.
        """
        provider = provider_of(model)
        if limit_ledger.record(provider, model, limits_mod.TRANSPORT_FAILURE) is not None:
            logger.info(
                "transport failure provider=%s model=%s; cooling down",
                provider or "-",
                model,
            )

    def pool_for(body: dict[str, Any]) -> candidates.CandidatePool:
        """Run the three routing gates for this request.

        An explicit ``--cheap-model`` list stays an allowlist: a deployment that
        named its cheap models must not be silently widened to everything the
        registry knows.  With no list, every discovered model is a candidate.
        """
        if not len(capabilities):
            return candidates.CandidatePool(candidates=())
        needs = candidates.needs_from_body(body)
        build = candidates.pool_for_models if routing.pool else candidates.build_pool
        kwargs: dict[str, Any] = {
            "registry": capabilities,
            "sheet": sheet,
            "catalogue": catalogue if len(catalogue) else None,
            "limits": limit_ledger,
            "needs": needs,
            # Active mode may only rewrite to a model the sheet can price: the
            # gateway reports the cost of the model that actually ran.
            "require_priced": router_mode == "active",
        }
        if routing.pool:
            return build(routing.pool, **kwargs)
        return build(**kwargs)

    def provider_of(model: str) -> str:
        if not len(catalogue):
            return ""
        return catalogue.provider_name_for(model)

    def remember_limit(model: str, response: httpx.Response) -> None:
        """Record a refusal so the next request routes around it."""
        provider = provider_of(model)
        if response.status_code < 400:
            if provider or model:
                limit_ledger.clear(provider, model)
            # A provider's own quota headers arrive on a *successful* response
            # too, and that is the only moment its remaining count is truthful.
            limit_ledger.record_quota(limits_mod.quota_from_headers(provider, response.headers))
            return
        retry_after = limits_mod.parse_retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = _numeric_retry_after(response.headers)
        limited = limit_ledger.record(
            provider, model, response.status_code, retry_after=retry_after
        )
        if limited is not None:
            logger.info(
                "limit provider=%s model=%s status=%s cooldown=%ds strikes=%d",
                provider or "-",
                model,
                limited.status,
                int(limited.remaining()),
                limited.strikes,
            )

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
        # The three routing gates run first: capability, supply, health.  What
        # survives is the ordered candidate list the router may switch within,
        # a locked-out requested model is named so rule 0 can move off it, and
        # every candidate carries the quality score ranking needs.  The
        # decision comes before the budget gate so the gate can price and cap
        # the model that will actually run; planning has no lasting side effect
        # for a request the gate then refuses.
        decision = plan(
            session_id,
            body,
            model_requested,
            pinned=pin_requested(dict(request.headers.items())),
            **route_context(body, model_requested),
        )
        routed_model = router.applied_target(
            decision,
            requested=model_requested,
            enabled=router_mode == "active" or decision.availability,
            allowed=allowed_models_for(request),
        )
        denied = authorize(request, model_requested, raw, effective=routed_model)
        if denied is not None:
            return denied
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
        running_model = routed_model or model_requested
        applied_effort = resolve_effort(running_model, decision)
        effort_used = applied_effort if applied_effort is not None else effort_requested

        root = root_for(running_model)
        if not root:
            # A catalogue is configured and nothing serves this model.  Sending
            # it to the last provider that happened to answer would bill the
            # wrong account and log a model id no provider offers.
            return _unknown_provider(running_model, catalogue.names())

        def build(target: str | None) -> httpx.Request:
            model = target or model_requested
            return client.build_request(
                "POST",
                root_for(model) + CHAT_PATH,
                content=outbound(raw, body, target, resolve_effort(model, decision)),
                headers=headers_for(model, request),
            )

        def retry_target(refused: str) -> str:
            """The next gated model after ``refused`` was turned away, or ``""``.

            The pool is rebuilt rather than filtered: recording the refusal also
            cooled the model down, so the rebuild is what makes the next request
            -- and this one -- see the limit.

            Quality is held where it can be.  The retry prefers a model at least
            as capable as the one that refused; only when no such model is left
            does it fall back to the strongest survivor.  A cheaper answer beats
            a failed turn, but a silent downgrade on a hard step is not the
            default.
            """
            pool = pool_for(body)
            info = capabilities.get(refused)
            floor = candidates.quality_of(info) if info is not None else 0.0
            best = pool.best(
                exclude={refused},
                min_quality=floor,
                require_priced=router_mode == "active",
            )
            if best is None:
                remaining = [c for c in pool.candidates if c.model != refused]
                if router_mode == "active":
                    remaining = [c for c in remaining if c.priced]
                best = max(remaining, key=lambda c: c.quality) if remaining else None
            return best.model if best is not None else ""

        outgoing = build(routed_model)
        started = time.perf_counter()

        if not streaming:
            try:
                upstream = await upstream_send(client, outgoing)
            except UpstreamFailure as exc:
                # The provider never answered; remember that so the next request
                # does not pay the same timeout again.
                remember_transport_failure(running_model)
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
            # A limit learned here is a limit every later request skips.  A 429
            # means nothing was generated, so one immediate retry on a model
            # that passed all three gates is safe and saves the turn.  A 404
            # counts too: it says this provider does not serve the model, which
            # is exactly the kind of refusal another candidate can absorb.
            remember_limit(running_model, upstream)
            if limits_mod.retryable(upstream.status_code):
                fallback = retry_target(running_model)
                if fallback:
                    await upstream.aclose()
                    decision = replace(
                        decision,
                        action="SWITCH",
                        target_model=fallback,
                        reason=(
                            f"upstream {upstream.status_code} on {running_model}: "
                            f"retried on {fallback}"
                        ),
                    )
                    routed_model = fallback
                    running_model = fallback
                    upstream = await upstream_send(client, build(fallback))
                    remember_limit(running_model, upstream)
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
            remember_transport_failure(running_model)
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
        # The status is known before a byte is relayed, so a refusal can still be
        # retried on another gated model without the client seeing a truncated
        # stream.
        remember_limit(running_model, upstream)
        if limits_mod.retryable(upstream.status_code):
            fallback = retry_target(running_model)
            if fallback:
                await upstream.aclose()
                decision = replace(
                    decision,
                    action="SWITCH",
                    target_model=fallback,
                    reason=(
                        f"upstream {upstream.status_code} on {running_model}: "
                        f"retried on {fallback}"
                    ),
                )
                routed_model = fallback
                running_model = fallback
                upstream = await upstream_send(client, build(fallback), stream=True)
                remember_limit(running_model, upstream)
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
        model = _body_model(raw)
        denied = authorize(request, model, raw)
        if denied is not None:
            return denied
        # The passthrough is never priced and never budget-gated, but it is not
        # invisible: one count-only row records that a request reached the
        # upstream off the metered routes, so traffic cannot leave the ledger
        # without at least a trace (F-12).  Token counts and cost stay NULL --
        # the route prices nothing -- so the row can never inflate a total.
        started = time.perf_counter()
        passthrough_root = root_for(model) if model else base
        outgoing = client.build_request(
            request.method,
            f"{passthrough_root or base}/{path}",
            content=raw,
            headers=(headers_for(model, request) if model else forward_headers(request)),
            params=request.query_params,
        )
        upstream = await client.send(outgoing, stream=True)
        record(
            session.derive_session_id(
                dict(request.headers.items()),
                _json_object(raw),
                session_salt,
                wire=session.WIRE_PASSTHROUGH,
            ),
            model or PASSTHROUGH_MODEL,
            model or PASSTHROUGH_MODEL,
            Usage(
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
            ),
            started,
            upstream.status_code,
            Decision(
                action="STAY",
                target_model=None,
                reason="passthrough route is not metered",
            ),
            project=request.state.project,
            route="passthrough",
        )
        return StreamingResponse(
            relay(upstream),
            status_code=upstream.status_code,
            headers=forward_response_headers(upstream.headers),
        )

    # Registered last, and promoted ahead of the catch-all above, so that
    # POST /v1/messages is logged rather than relayed unlogged.  It reuses the
    # client put on app.state above, so both routes share one connection pool
    # and one lifespan.
    # Both adapter routes build their own request, so the provider lookup is
    # published on app.state rather than threaded through each signature.
    app.state.upstream_root_for = root_for
    app.state.upstream_headers_for = headers_for
    app.state.remember_limit = remember_limit
    app.state.route_context = route_context

    anthropic_adapter.register_routes(
        app,
        upstream_url,
        metered,
        sheet,
        router_mode,
        config=routing,
        session_salt=session_salt,
        authorize=authorize,
        allowed_models=allowed_models_for,
    )
    responses_adapter.register_routes(
        app,
        upstream_url,
        metered,
        sheet,
        session_salt=session_salt,
        authorize=authorize,
    )

    return app
