"""Shared upstream request handling for the metered routes.

All three metered routes (chat completions, responses, messages) call the
upstream in the same two ways -- a plain send, and a send that streams the
response body -- and fail in the same ways.  Classifying those failures here,
instead of in each route, gives every route the same coded ``stage``/``type``
pair for its failure-ledger row and the same deliberate 502-class response.

A ledger row is metadata only: ``stage`` is ``setup`` or ``stream`` and
``type`` is one of the short codes :func:`classify_http_error` produces.  The
original exception text, response bodies and credentials never reach the row.
"""

from __future__ import annotations

import httpx

# The stage at which the failure happened.  ``setup`` means the send itself
# failed, so there is no upstream response to relay and the proxy answers with
# a 502 of its own.  ``stream`` means the response started but died while its
# body was being read.
SETUP = "setup"
STREAM = "stream"

# Failure types, ordered so the most specific class is tested first: httpx's
# hierarchy is shallow, and `ConnectTimeout` is not a `ConnectError`.
_CLASSIFICATIONS = (
    (httpx.ConnectTimeout, "connect_timeout"),
    (httpx.ReadTimeout, "read_timeout"),
    (httpx.ConnectError, "connect_error"),
    (httpx.ReadError, "read_error"),
    (httpx.ProtocolError, "protocol_error"),
    (httpx.TimeoutException, "timeout"),
    (httpx.HTTPError, "transport"),
)


def classify_http_error(exc: Exception) -> str:
    """A short coded failure type for an upstream transport exception.

    ``exc`` needs not be an ``httpx.HTTPError``: a non-httpx exception still
    yields ``"transport"``, so a route that catches broadly can name it without
    storing any of its text.
    """
    for error_type, code in _CLASSIFICATIONS:
        if isinstance(exc, error_type):
            return code
    return "transport"


class UpstreamFailure(Exception):
    """A transport-level upstream failure, coded and never carrying raw text.

    ``stage`` is :data:`SETUP` or :data:`STREAM`; ``failure_type`` is one of
    the codes from :func:`classify_http_error`.  The original exception is
    chained for logging, but nothing derived from it is written to the ledger.
    """

    def __init__(self, stage: str, failure_type: str) -> None:
        super().__init__(stage, failure_type)
        self.stage = stage
        self.failure_type = failure_type


async def send(
    client: httpx.AsyncClient, request: httpx.Request, *, stream: bool = False
) -> httpx.Response:
    """Send ``request`` through ``client``, turning transport errors into a coded failure.

    A failure here means the request never produced an upstream response body,
    so the caller answers the client with a 502 and records one ledger row
    carrying ``UpstreamFailure.stage`` and ``.failure_type``.
    """
    try:
        return await client.send(request, stream=stream)
    except httpx.HTTPError as exc:
        raise UpstreamFailure(SETUP, classify_http_error(exc)) from exc