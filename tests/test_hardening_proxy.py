"""Hardening tests for the proxy: concurrency, disconnects, upstream faults, passthrough.

Everything runs in-process.  The upstream is an ASGI app reached through
``StreamingASGITransport``, exactly as ``test_e2e`` does, so the proxy's own
httpx client, request building, streaming relay and logging all stay on the real
code path while no socket is ever opened.

These tests only *find* bugs.  Nothing here fixes ``src/``: a test that exposes a
real defect is marked ``xfail(strict=True)`` and carries a BUG id that also
appears in ``docs/audit/hardening-findings.md``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from mock_upstream import StreamingASGITransport, create_mock_upstream, sse

from tamias import proxy
from tamias.pricing import load_price_sheet
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.store import Store

UPSTREAM_URL = "http://upstream.invalid"
UPSTREAM_HOST = "upstream.invalid"
PROXY_URL = "http://proxy.test"
CHAT_PATH = proxy.CHAT_PATH

STRONG = "gpt-strong"
CHEAP = "gpt-cheap"
MIN_GAP = 3
CONFIG = RouterConfig(
    easy_tools=EASY_TOOLS, cheap_model=CHEAP, strong_model=STRONG, min_gap=MIN_GAP
)

PRICES = f"""
date = "2026-10-04"

[{STRONG}]
input = 3.0
output = 15.0
cached_input = 0.3
cache_write = 3.75

[{CHEAP}]
input = 0.25
output = 1.25
cached_input = 0.025
cache_write = 0.3
"""

USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "total_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 3},
}


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def prices_path(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


def build_proxy(
    upstream_app: Any,
    db_path: Path,
    prices_path: Path,
    *,
    router_mode: str = "shadow",
    inject_usage: bool = False,
) -> tuple[FastAPI, Store]:
    """The real proxy app, wired to ``upstream_app`` in place of a socket."""
    store = Store(db_path)
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        load_price_sheet(prices_path),
        router_mode,
        config=CONFIG,
        inject_usage=inject_usage,
        transport=StreamingASGITransport(upstream_app),
    )
    return app, store


@asynccontextmanager
async def proxy_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url=PROXY_URL, timeout=None
    ) as client:
        yield client


def user_body(model: str = STRONG, *, stream: bool = False, text: str = "hello") -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
    }
    return json.dumps(body, separators=(",", ":")).encode()


def send_headers(session: str) -> dict[str, str]:
    return {"content-type": "application/json", proxy.SESSION_HEADER: session}


def status_of(store: Store) -> list[str]:
    return [str(row["status"]) for row in store.rows()]


def ok_completion() -> dict[str, Any]:
    """A minimal well-formed completion, used where an upstream must recover."""
    return {
        "id": "chatcmpl-ok",
        "object": "chat.completion",
        "model": STRONG,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": USAGE,
    }


# --- (c) 50 concurrent requests over 5 sessions ------------------------------


async def test_fifty_concurrent_requests_over_five_sessions_log_fifty_rows(
    db_path: Path, prices_path: Path
) -> None:
    """50 in-flight requests, half of them streaming, all reach the log intact.

    The proxy serialises its writes to one sqlite file and keeps per-session
    routing state in memory; neither may lose a row or raise when 50 requests
    are in flight at once.
    """
    app, store = build_proxy(create_mock_upstream(), db_path, prices_path)
    sessions = [f"conc-{index}" for index in range(5)]
    per_session = 10

    async with proxy_client(app) as client:

        async def fire(session: str, index: int) -> int:
            body = user_body(CHEAP if index % 2 else STRONG, stream=index % 3 != 0)
            response = await client.post(CHAT_PATH, content=body, headers=send_headers(session))
            return response.status_code

        tasks = [fire(session, index) for session in sessions for index in range(per_session)]
        statuses = await asyncio.gather(*tasks)

    assert all(status == 200 for status in statuses), f"not every request succeeded: {statuses}"

    rows = store.rows()
    assert len(rows) == 50, f"expected 50 logged rows, got {len(rows)}"
    by_session: dict[str, int] = {}
    for row in rows:
        by_session[str(row["session_id"])] = by_session.get(str(row["session_id"]), 0) + 1
    assert by_session == {session: per_session for session in sessions}, by_session
    store.close()


# --- (d) client disconnects mid-stream ---------------------------------------


async def test_client_disconnect_after_two_chunks_closes_upstream_and_invents_no_counts(
    db_path: Path, prices_path: Path
) -> None:
    """A client that walks away mid-stream must not wedge the proxy.

    The upstream response has to be closed, the request still logged, and the
    counts it never received recorded as UNKNOWN (NULL) rather than as zero.
    """
    state: dict[str, bool] = {"closed": False}

    async def chunks() -> AsyncIterator[bytes]:
        try:
            for index in range(6):
                yield sse(
                    {
                        "id": "chatcmpl-disc",
                        "object": "chat.completion.chunk",
                        "model": STRONG,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"chunk-{index} "},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                await asyncio.sleep(0.02)
        finally:
            state["closed"] = True

    faulty = FastAPI()

    @faulty.post(CHAT_PATH)
    async def chat(request: Request) -> Response:
        await request.body()
        return StreamingResponse(chunks(), media_type="text/event-stream")

    app, store = build_proxy(faulty, db_path, prices_path)
    body = user_body(stream=True)

    async with proxy_client(app) as client:
        received = 0
        async with client.stream(
            "POST", CHAT_PATH, content=body, headers=send_headers("disc-1")
        ) as response:
            assert response.status_code == 200
            async for _chunk in response.aiter_bytes():
                received += 1
                if received == 2:
                    break

    assert received == 2, f"expected to stop after 2 chunks, read {received}"
    for _ in range(50):
        if state["closed"]:
            break
        await asyncio.sleep(0.02)
    assert state["closed"], "the upstream response was never closed after the client left"

    rows = store.rows()
    assert len(rows) == 1, f"the disconnected request should still be logged once, got {len(rows)}"
    row = rows[0]
    for column in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens"):
        assert row[column] is None, f"{column} was invented as {row[column]!r} for a cut-off stream"
    assert row["model_used"] == STRONG
    assert row["status"] == "200"
    store.close()


# --- (e) upstream faults -----------------------------------------------------


def create_status_upstream(
    status: int,
    body: bytes,
    content_type: str,
    headers: dict[str, str] | None = None,
    *,
    fail_times: int = 1,
) -> FastAPI:
    """Fails the first ``fail_times`` requests, then behaves like a normal upstream.

    That way each test can prove that one fault does not poison the connection
    pool or the request log: the request after the fault must still succeed.
    """
    faulty = FastAPI()
    pending = {"left": fail_times}

    @faulty.post(CHAT_PATH)
    async def chat(request: Request) -> Response:
        await request.body()
        if pending["left"] > 0:
            pending["left"] -= 1
            return Response(
                content=body, status_code=status, media_type=content_type, headers=headers
            )
        return JSONResponse(ok_completion())

    return faulty


async def test_upstream_429_with_retry_after_passes_through_and_is_logged(
    db_path: Path, prices_path: Path
) -> None:
    """429: the status, the body and one log row, then service resumes."""
    payload = json.dumps({"error": {"message": "slow down", "type": "rate_limit_error"}}).encode()
    upstream = create_status_upstream(429, payload, "application/json", {"retry-after": "7"})
    app, store = build_proxy(upstream, db_path, prices_path)

    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH, content=user_body(), headers=send_headers("err-429")
        )
        assert response.status_code == 429
        assert response.content == payload, "the upstream error body did not pass through"
        assert status_of(store) == ["429"], status_of(store)

        follow_up = await client.post(
            CHAT_PATH, content=user_body(), headers=send_headers("err-429-again")
        )
        assert follow_up.status_code == 200, "a normal request after a 429 must still work"

    assert status_of(store) == ["429", "200"], status_of(store)
    store.close()


@pytest.mark.xfail(
    strict=True,
    reason="BUG-2: a 429 loses its Retry-After header, only content-type is relayed",
)
async def test_upstream_429_keeps_its_retry_after_header(db_path: Path, prices_path: Path) -> None:
    """A rate-limited client must learn when to come back.

    ``Retry-After`` is the one upstream header a client cannot do without: without
    it a well-behaved SDK either hammers the proxy or backs off for its own
    default, ignoring what the upstream actually asked for.  The proxy rebuilds
    the response instead of relaying it, so the header has to survive that.
    """
    payload = json.dumps({"error": {"message": "slow down", "type": "rate_limit_error"}}).encode()
    upstream = create_status_upstream(429, payload, "application/json", {"retry-after": "7"})
    app, store = build_proxy(upstream, db_path, prices_path)

    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH, content=user_body(), headers=send_headers("hdr-429")
        )

    assert response.status_code == 429
    assert response.headers.get("retry-after") == "7", dict(response.headers)
    store.close()


async def test_upstream_500_passes_through_and_is_logged(db_path: Path, prices_path: Path) -> None:
    """500: the status, the body and one log row."""
    payload = json.dumps(
        {"error": {"message": "upstream exploded", "type": "server_error"}}
    ).encode()
    upstream = create_status_upstream(500, payload, "application/json")
    app, store = build_proxy(upstream, db_path, prices_path)

    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH, content=user_body(), headers=send_headers("err-500")
        )
        assert response.status_code == 500
        assert response.content == payload, "the upstream error body did not pass through"

    rows = store.rows()
    assert len(rows) == 1, f"expected exactly one row for the 500, got {len(rows)}"
    assert rows[0]["status"] == "500"
    assert rows[0]["model_requested"] == STRONG
    store.close()


async def test_upstream_502_html_body_passes_through_and_is_logged(
    db_path: Path, prices_path: Path
) -> None:
    """502 with a non-JSON HTML body: no crash, bytes intact, one row."""
    payload = b"<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    upstream = create_status_upstream(502, payload, "text/html")
    app, store = build_proxy(upstream, db_path, prices_path)

    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH, content=user_body(), headers=send_headers("err-502")
        )
        assert response.status_code == 502
        assert response.content == payload, "the HTML error body was not relayed byte-for-byte"
        assert "text/html" in response.headers["content-type"]

    rows = store.rows()
    assert len(rows) == 1, f"expected exactly one row for the 502, got {len(rows)}"
    assert rows[0]["status"] == "502"
    store.close()


class RefusingTransport(httpx.AsyncBaseTransport):
    """An upstream that is not listening: every send raises ConnectError."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)


class HandlerTransport(httpx.AsyncBaseTransport):
    """Hands every outbound request to a caller-supplied coroutine.

    Lets one test mix a failing and a healthy upstream inside a single proxy, so
    that "one model is unreachable" can be told apart from "the proxy is down".
    """

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return await self.handler(request)


async def test_connection_refused_is_logged_and_service_resumes(
    db_path: Path, prices_path: Path
) -> None:
    """A refused upstream connection must not take the proxy down with it.

    The proxy answers the client with a 5xx of its own -- there is no upstream
    status to pass through -- the request must still leave a row behind, so that a
    failed call is visible in the audit log rather than silently absent, and the
    next request on the same session must still be served.

    The upstream refuses only the first call and then recovers, which is what a
    restarted backend looks like: the fault is transient, so a second 5xx would
    mean the proxy is unable to recover at all.
    """
    healthy = StreamingASGITransport(create_mock_upstream())
    strong_calls = {"n": 0}

    async def refuse_first_strong_call(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("model") == STRONG:
            strong_calls["n"] += 1
            if strong_calls["n"] == 1:
                raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        return await healthy.handle_async_request(request)

    store = Store(db_path)
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        load_price_sheet(prices_path),
        "shadow",
        config=CONFIG,
        transport=HandlerTransport(refuse_first_strong_call),
    )

    async with proxy_client(app) as client:
        refused = await client.post(
            CHAT_PATH, content=user_body(STRONG), headers=send_headers("refused")
        )
        assert refused.status_code >= 500, "a refused connection must surface as a 5xx, not a 2xx"

        served = await client.post(
            CHAT_PATH, content=user_body(STRONG), headers=send_headers("refused")
        )
        assert served.status_code == 200, "the request after a refusal must still be served"

    rows = store.rows()
    assert len(rows) == 2, (
        f"both requests must be logged, got {len(rows)}: {[dict(r) for r in rows]}"
    )
    assert status_of(store) == ["500", "200"], status_of(store)
    assert rows[0]["input_tokens"] is None
    assert rows[0]["output_tokens"] is None
    store.close()


async def test_slow_upstream_still_relays_and_logs_one_row(
    db_path: Path, prices_path: Path
) -> None:
    """A slow upstream is waited for, relayed, logged once, and does not block the next call."""
    delay = 0.25
    pending = {"slow_left": 1}
    calls = {"slow": 0, "fast": 0}
    slow = FastAPI()

    @slow.post(CHAT_PATH)
    async def chat(request: Request) -> Response:
        await request.body()
        if pending["slow_left"] > 0:
            pending["slow_left"] -= 1
            calls["slow"] += 1
            await asyncio.sleep(delay)
        else:
            calls["fast"] += 1
        return JSONResponse(
            {
                "id": "chatcmpl-slow",
                "object": "chat.completion",
                "model": STRONG,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": USAGE,
            }
        )

    app, store = build_proxy(slow, db_path, prices_path)

    async with proxy_client(app) as client:
        first = await client.post(CHAT_PATH, content=user_body(), headers=send_headers("slow-1"))
        assert first.status_code == 200
        second = await client.post(CHAT_PATH, content=user_body(), headers=send_headers("slow-2"))
        assert second.status_code == 200, "a normal request after a slow one must still work"

    rows = store.rows()
    assert len(rows) == 2, f"expected one row per request, got {len(rows)}"
    assert all(row["status"] == "200" for row in rows)
    assert rows[0]["latency_ms"] is not None and rows[0]["latency_ms"] >= 200, rows[0]["latency_ms"]
    assert calls == {"slow": 1, "fast": 1}
    store.close()


# --- (f) malformed request bodies -------------------------------------------


async def test_invalid_json_is_rejected_with_400(db_path: Path, prices_path: Path) -> None:
    app, store = build_proxy(create_mock_upstream(), db_path, prices_path)
    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH, content=b"{not json", headers=send_headers("bad-json")
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    store.close()


async def test_json_array_body_is_rejected_with_400(db_path: Path, prices_path: Path) -> None:
    app, store = build_proxy(create_mock_upstream(), db_path, prices_path)
    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH,
            content=b'[{"role": "user", "content": "hi"}]',
            headers=send_headers("bad-array"),
        )
    assert response.status_code == 400, response.text
    store.close()


async def test_body_without_messages_is_forwarded_and_logged(
    db_path: Path, prices_path: Path
) -> None:
    """A body with no ``messages`` key is unusual but not the proxy's business to reject.

    Sent without an explicit session header on purpose: deriving a session id and
    asking the router for a decision both have to cope with ``messages`` being
    absent, which is the part most likely to raise.
    """
    app, store = build_proxy(create_mock_upstream(), db_path, prices_path)
    async with proxy_client(app) as client:
        response = await client.post(
            CHAT_PATH,
            content=b'{"model":"gpt-strong"}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200, response.text
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0]["session_id"].startswith(proxy.SESSION_ID_PREFIX), rows[0]["session_id"]
    assert rows[0]["model_requested"] == STRONG
    store.close()


# --- (g) passthrough paths ---------------------------------------------------


class Recorder:
    """What the upstream actually received, taken from the request it was given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, request: Request) -> None:
        self.calls.append(
            {
                "method": request.method,
                "host": request.url.hostname,
                "path": request.url.path,
                "query": request.url.query,
            }
        )

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "the upstream was never called"
        return self.calls[-1]


def create_recording_upstream(recorder: Recorder) -> FastAPI:
    upstream = FastAPI()

    @upstream.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def catch_all(path: str, request: Request) -> Response:
        await request.body()
        recorder.record(request)
        return JSONResponse({"echo": path})

    return upstream


PASSTHROUGH_CASES = [
    ("double_slash_host", "//evil.example/x", "GET"),
    ("double_slash_host_post", "//evil.example/x", "POST"),
    ("dot_dot_segment", "/v1/../admin", "GET"),
    ("dot_dot_segment_post", "/v1/../admin", "POST"),
    ("at_prefixed_host", "/@evil.example/", "GET"),
    ("at_prefixed_host_delete", "/@evil.example/", "DELETE"),
    ("deep_escape", "/v1/chat/completions/../../etc/passwd", "GET"),
]


@pytest.mark.parametrize(
    "path,method",
    sorted((case[1], case[2]) for case in PASSTHROUGH_CASES),
    ids=[case[0] for case in sorted(PASSTHROUGH_CASES)],
)
async def test_passthrough_keeps_the_configured_upstream_host_query_and_method(
    path: str, method: str, db_path: Path, prices_path: Path
) -> None:
    """A client-supplied path can never redirect the proxy off its upstream.

    ``//evil.example/x`` and ``/@evil.example/`` look like an authority to a URL
    parser, and ``..`` segments look like a directory to a filesystem: the
    outbound request must still be addressed to the configured upstream host,
    with the query string and the method the client used.
    """
    recorder = Recorder()
    app, store = build_proxy(create_recording_upstream(recorder), db_path, prices_path)

    url = f"{PROXY_URL}{path}?alpha=1&beta=two"
    async with proxy_client(app) as client:
        response = await client.request(method, url, content=b"" if method != "GET" else None)

    assert response.status_code == 200, response.text
    call = recorder.last
    assert call["host"] == UPSTREAM_HOST, (
        f"outbound host was {call['host']!r}, not {UPSTREAM_HOST!r}"
    )
    assert call["query"] == "alpha=1&beta=two", f"query was rewritten to {call['query']!r}"
    assert call["method"] == method, f"method was rewritten to {call['method']!r}"
    store.close()


async def test_passthrough_does_not_log_rows(db_path: Path, prices_path: Path) -> None:
    """Passthrough traffic is relayed, not priced, so it must not invent log rows."""
    recorder = Recorder()
    app, store = build_proxy(create_recording_upstream(recorder), db_path, prices_path)

    async with proxy_client(app) as client:
        await client.get(f"{PROXY_URL}//evil.example/x?alpha=1")

    assert store.rows() == []
    store.close()
