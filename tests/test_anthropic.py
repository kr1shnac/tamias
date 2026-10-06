"""Anthropic /v1/messages tests. Everything runs in-process: no socket is opened."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mock_anthropic_upstream import (
    FINAL_USAGE,
    MODEL_CACHED,
    MODEL_ERROR,
    MODEL_NO_USAGE,
    MODEL_OVERLOADED,
    MODEL_PARTIAL_USAGE,
    PARTIAL_USAGE,
    START_USAGE,
    STREAM_TEXTS,
    create_mock_anthropic_upstream,
    sse,
)
from mock_upstream import StreamingASGITransport

from tamias import anthropic_adapter, pricing, proxy
from tamias.anthropic_adapter import AnthropicStreamUsage, usage_from_anthropic
from tamias.pricing import ModelPrice, PriceSheet
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
NO_USAGE = Usage(None, None, None, None)
FULL_USAGE = Usage(1000, 42, 800, 100, 40)
ANTHROPIC_HEADERS = {
    "content-type": "application/json",
    "x-api-key": "sk-ant-mock-key",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
}


class FakeStore:
    """Collects the rows the adapter logs, with store.Store's exact signature."""

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
                "ts": ts,
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
        assert self.rows, "expected at least one logged row"
        return self.rows[-1]


@asynccontextmanager
async def closing_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Closes whatever client the adapter put on the app, the way a real one would."""
    try:
        yield
    finally:
        await application.state.upstream_client.aclose()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sheet() -> PriceSheet:
    return PriceSheet(
        date="2026-10-04",
        models={
            MODEL_CACHED: ModelPrice(input=3.0, output=15.0, cached_input=0.3, cache_write=3.75),
        },
    )


@pytest.fixture
def upstream() -> FastAPI:
    return create_mock_anthropic_upstream()


@pytest.fixture
def app(upstream: FastAPI, store: FakeStore, sheet: PriceSheet) -> FastAPI:
    """A bare app with only the Anthropic route registered on it."""
    return anthropic_adapter.register_routes(
        FastAPI(lifespan=closing_lifespan),
        UPSTREAM_URL,
        store,
        sheet,
        "shadow",
        transport=StreamingASGITransport(upstream),
    )


def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with as_client(app) as http_client:
        yield http_client


def make_body(model: str, **extra: Any) -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(extra)
    return json.dumps(body).encode()


def usage_frames() -> list[bytes]:
    """The four usage-bearing frames of a stream, plus a usage-free ping."""
    return [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {"model": MODEL_CACHED, "usage": dict(START_USAGE)},
            },
        ),
        sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "a"},
            },
        ),
        sse("ping", {"type": "ping"}),
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": dict(FINAL_USAGE),
            },
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]


def collect(collector: AnthropicStreamUsage, data: bytes, size: int) -> None:
    for start in range(0, len(data), size):
        collector.feed(data[start : start + size])


def parse_frames(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Split a relayed SSE body back into its (event, data) pairs."""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split(b"\n\n"):
        name = ""
        data: bytes | None = None
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                name = line[len(b"event:") :].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                data = line[len(b"data:") :]
        if data is not None:
            frames.append((name, json.loads(data)))
    return frames


def rebuild(frames: list[tuple[str, dict[str, Any]]]) -> bytes:
    return b"".join(
        b"event: "
        + name.encode("utf-8")
        + b"\ndata: "
        + json.dumps(payload).encode("utf-8")
        + b"\n\n"
        for name, payload in frames
    )


def echo_of(frame: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return frame[1]["message"]["echo"]


def forwarded_body(echo: dict[str, Any]) -> Any:
    """The body the upstream actually received, recovered from its echo."""
    return json.loads(base64.b64decode(echo["raw_body_b64"]))


def test_input_tokens_is_the_whole_prompt() -> None:
    usage = usage_from_anthropic(START_USAGE)

    assert usage == Usage(1000, 1, 800, 100, 40)
    assert usage.input_tokens == 100 + 800 + 100
    assert usage.cached_input_tokens == 800
    assert usage.cache_write_tokens == 100


def test_one_hour_cache_split_is_read() -> None:
    assert usage_from_anthropic(START_USAGE).cache_write_1h_tokens == 40
    assert usage_from_anthropic(PARTIAL_USAGE).cache_write_1h_tokens is None


def test_missing_cache_field_leaves_the_total_unknown() -> None:
    usage = usage_from_anthropic(PARTIAL_USAGE)

    assert usage == Usage(None, 7, None, 100, None)
    # The two known parts do not license reading the missing one as zero.
    assert usage.input_tokens is None


def test_absent_or_unusable_usage_is_all_unknown() -> None:
    assert usage_from_anthropic(None) == NO_USAGE
    assert usage_from_anthropic({}) == NO_USAGE
    assert usage_from_anthropic([1, 2]) == NO_USAGE
    assert usage_from_anthropic({"output_tokens": "many"}) == NO_USAGE
    # A count that is not a number is UNKNOWN, and leaves the total uncomputed
    # rather than blowing up the sum.
    assert usage_from_anthropic({"input_tokens": "100", "cache_read_input_tokens": 1}) == Usage(
        None, None, 1, None
    )


def test_stream_collector_merges_message_start_and_message_delta() -> None:
    collector = AnthropicStreamUsage()

    for frame in usage_frames():
        collector.feed(frame)

    assert collector.usage() == FULL_USAGE
    assert collector.model == MODEL_CACHED


def test_stream_collector_keeps_the_last_value_of_every_field() -> None:
    collector = AnthropicStreamUsage()

    collector.feed(usage_frames()[0])
    collector.feed(sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 20}}))
    collector.feed(
        sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": None}})
    )
    collector.feed(sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 42}}))

    # message_delta never repeats the prompt counts, so they must survive it, and
    # a null field must not erase the value that came before it.
    assert collector.usage() == FULL_USAGE


def test_stream_collector_survives_chunk_boundaries_mid_line() -> None:
    raw = b"".join(usage_frames())

    for size in (1, 3, 17, 256):
        collector = AnthropicStreamUsage()
        collect(collector, raw, size)
        assert collector.usage() == FULL_USAGE, f"lost usage at chunk size {size}"


def test_stream_collector_absorbs_a_final_line_without_a_newline() -> None:
    frames = usage_frames()
    collector = AnthropicStreamUsage()

    collector.feed(b"".join(frames[:3]) + frames[3][:-2])

    assert collector.usage() == FULL_USAGE


def test_stream_collector_ignores_anything_that_is_not_a_data_frame() -> None:
    collector = AnthropicStreamUsage()

    collector.feed(b"event: message_delta\n\n: a comment\n\ndata: not json\n\ndata: [1, 2]\n\n")

    assert collector.usage() == NO_USAGE
    assert collector.model is None


def test_cost_of_the_total_prompt(sheet: PriceSheet) -> None:
    cost = anthropic_adapter.cost_for_anthropic(MODEL_CACHED, FULL_USAGE, sheet)

    # The total is what makes this arithmetic possible: 100 uncached input at 3.0,
    # 800 cached reads at 0.3, 60 5m writes and 40 1h writes at 3.75, and 42
    # output at 15.0.  Handed the 100 uncached `input_tokens` instead, the writes
    # and the reads could not be billed apart.
    assert cost.usd == pytest.approx(0.001545)
    assert "uncached_input 100" in cost.formula
    assert "cached_input 800" in cost.formula
    assert "1h_writes 40" in cost.formula
    assert cost.price_sheet_date == "2026-10-04"


@pytest.mark.parametrize("missing_rate", ["cached_input", "cache_write"])
def test_cache_usage_without_a_cache_rate_is_unknown(missing_rate: str) -> None:
    prices = {"input": 3.0, "output": 15.0, "cached_input": 0.3, "cache_write": 3.75}
    prices[missing_rate] = None
    sheet = PriceSheet(date="2026-10-06", models={MODEL_CACHED: ModelPrice(**prices)})

    cost = anthropic_adapter.cost_for_anthropic(MODEL_CACHED, FULL_USAGE, sheet)

    assert cost.usd is None
    assert missing_rate + "_rate" in cost.formula


async def test_body_forwarded_byte_identical(client: httpx.AsyncClient, store: FakeStore) -> None:
    raw = (
        b'{\n  "model": "mock-claude-cached",\n  "max_tokens": 64,\n  "messages": [\n'
        b'    {"role": "user", "content": "caf\xc3\xa9 \\u00e9"}\n  ]\n}\n'
    )

    response = await client.post("/v1/messages", content=raw, headers=ANTHROPIC_HEADERS)

    assert response.status_code == 200
    assert response.json()["type"] == "message"
    assert base64.b64decode(response.json()["echo"]["raw_body_b64"]) == raw
    assert store.last["model_requested"] == MODEL_CACHED


async def test_streamed_body_forwarded_byte_identical(client: httpx.AsyncClient) -> None:
    raw = make_body(MODEL_CACHED, stream=True)

    async with client.stream(
        "POST", "/v1/messages", content=raw, headers=ANTHROPIC_HEADERS
    ) as response:
        raw_response = b"".join([chunk async for chunk in response.aiter_bytes()])

    frames = parse_frames(raw_response)
    assert base64.b64decode(echo_of(frames[0])["raw_body_b64"]) == raw
    assert "stream_options" not in forwarded_body(echo_of(frames[0]))


async def test_api_key_and_version_reach_the_upstream(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/messages",
        content=make_body(MODEL_CACHED),
        headers={**ANTHROPIC_HEADERS, "accept-encoding": "identity-not-forwarded"},
    )

    seen = response.json()["echo"]["headers"]
    assert seen["x-api-key"] == "sk-ant-mock-key"
    assert seen["anthropic-version"] == "2023-06-01"
    assert seen["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert "not-forwarded" not in seen.get("accept-encoding", "")
    assert seen["content-type"] == "application/json"


async def test_stream_events_are_relayed_unchanged_and_in_order(
    client: httpx.AsyncClient,
) -> None:
    async with client.stream(
        "POST",
        "/v1/messages",
        content=make_body(MODEL_CACHED, stream=True),
        headers=ANTHROPIC_HEADERS,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw_response = b"".join([chunk async for chunk in response.aiter_bytes()])

    frames = parse_frames(raw_response)
    assert [name for name, _payload in frames] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "ping",
        "content_block_delta",
        "ping",
        "content_block_delta",
        "ping",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert [
        payload["delta"]["text"]
        for _name, payload in frames
        if payload["type"] == "content_block_delta"
    ] == list(STREAM_TEXTS)
    assert frames[-2][1]["delta"]["stop_reason"] == "end_turn"
    # Rebuilt byte for byte: the relay neither reformatted nor reordered anything.
    assert rebuild(frames) == raw_response


async def test_stream_is_relayed_without_buffering(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    started = time.perf_counter()
    first_chunk_at: float | None = None

    async with client.stream(
        "POST",
        "/v1/messages",
        content=make_body(MODEL_CACHED, stream=True),
        headers=ANTHROPIC_HEADERS,
    ) as response:
        async for _chunk in response.aiter_bytes():
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter() - started
                # Nothing is logged until the stream ends: the usage only exists
                # once message_delta has arrived.
                assert store.rows == []

    total = time.perf_counter() - started
    assert first_chunk_at is not None
    assert first_chunk_at < total / 2


async def test_stream_usage_and_cost_are_logged(
    client: httpx.AsyncClient, store: FakeStore, sheet: PriceSheet
) -> None:
    async with client.stream(
        "POST",
        "/v1/messages",
        content=make_body(MODEL_CACHED, stream=True),
        headers=ANTHROPIC_HEADERS,
    ) as response:
        _ = [chunk async for chunk in response.aiter_bytes()]

    row = store.last
    assert row["usage"] == FULL_USAGE
    assert row["model_requested"] == row["model_used"] == MODEL_CACHED
    assert row["status"] == "200"
    assert row["latency_ms"] >= 0
    assert row["cost"] == anthropic_adapter.cost_for_anthropic(MODEL_CACHED, FULL_USAGE, sheet)
    assert row["cost"].usd == pytest.approx(0.001545)


async def test_buffered_usage_and_cost_are_logged(
    client: httpx.AsyncClient, store: FakeStore, sheet: PriceSheet
) -> None:
    response = await client.post(
        "/v1/messages", content=make_body(MODEL_CACHED), headers=ANTHROPIC_HEADERS
    )

    assert response.status_code == 200
    row = store.last
    assert row["usage"] == FULL_USAGE
    assert row["status"] == "200"
    assert row["cost"] == pricing.compute_cost(MODEL_CACHED, FULL_USAGE, sheet)


async def test_missing_cache_fields_log_an_unknown_total_and_no_cost(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post(
        "/v1/messages", content=make_body(MODEL_PARTIAL_USAGE), headers=ANTHROPIC_HEADERS
    )

    assert response.status_code == 200
    row = store.last
    assert row["usage"] == Usage(None, 42, None, 100, None)
    assert row["usage"].input_tokens is None
    assert row["cost"].usd is None
    assert "unknown" in row["cost"].formula


async def test_missing_usage_logs_none(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post(
        "/v1/messages", content=make_body(MODEL_NO_USAGE), headers=ANTHROPIC_HEADERS
    )

    assert response.status_code == 200
    assert "usage" not in response.json()
    assert store.last["usage"] == NO_USAGE

    async with client.stream(
        "POST",
        "/v1/messages",
        content=make_body(MODEL_NO_USAGE, stream=True),
        headers=ANTHROPIC_HEADERS,
    ) as streamed:
        raw_response = b"".join([chunk async for chunk in streamed.aiter_bytes()])

    assert [name for name, _payload in parse_frames(raw_response)][-1] == "message_stop"
    assert store.last["usage"] == NO_USAGE


async def test_overloaded_529_passes_through(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post(
        "/v1/messages", content=make_body(MODEL_OVERLOADED), headers=ANTHROPIC_HEADERS
    )

    assert response.status_code == 529
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "overloaded_error"
    row = store.last
    assert row["status"] == "529"
    assert row["model_requested"] == MODEL_OVERLOADED
    assert row["usage"] == NO_USAGE
    assert row["cost"].usd is None


async def test_overloaded_529_passes_through_a_streaming_request(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    async with client.stream(
        "POST",
        "/v1/messages",
        content=make_body(MODEL_OVERLOADED, stream=True),
        headers=ANTHROPIC_HEADERS,
    ) as response:
        assert response.status_code == 529
        raw_response = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert json.loads(raw_response)["error"]["type"] == "overloaded_error"
    assert store.last["status"] == "529"
    assert store.last["usage"] == NO_USAGE


async def test_upstream_500_passes_through(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post(
        "/v1/messages", content=make_body(MODEL_ERROR), headers=ANTHROPIC_HEADERS
    )

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "api_error"
    assert store.last["status"] == "500"


async def test_invalid_json_returns_the_anthropic_error_shape(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post("/v1/messages", content=b"{not json", headers=ANTHROPIC_HEADERS)

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert store.rows == []


async def test_non_object_json_body_returns_400(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post("/v1/messages", content=b"[1, 2, 3]", headers=ANTHROPIC_HEADERS)

    assert response.status_code == 400
    assert store.rows == []


async def test_shadow_decision_is_recorded_but_the_bytes_are_untouched(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    raw = make_body(MODEL_CACHED, stream=True)

    async with client.stream(
        "POST", "/v1/messages", content=raw, headers=ANTHROPIC_HEADERS
    ) as response:
        frames = parse_frames(b"".join([chunk async for chunk in response.aiter_bytes()]))

    decision = store.last["decision"]
    assert decision.persisted_only is True
    assert decision.action == "STAY"
    assert store.last["model_requested"] == store.last["model_used"] == MODEL_CACHED
    assert base64.b64decode(echo_of(frames[0])["raw_body_b64"]) == raw


async def test_active_mode_leaves_a_stay_decision_alone(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = anthropic_adapter.register_routes(
        FastAPI(lifespan=closing_lifespan),
        UPSTREAM_URL,
        store,
        sheet,
        "active",
        transport=StreamingASGITransport(upstream),
    )
    raw = make_body(MODEL_CACHED)

    async with as_client(app) as client:
        response = await client.post("/v1/messages", content=raw, headers=ANTHROPIC_HEADERS)

    assert store.last["decision"].persisted_only is False
    assert store.last["decision"].action == "STAY"
    assert base64.b64decode(response.json()["echo"]["raw_body_b64"]) == raw


async def test_router_off_records_a_stay(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = anthropic_adapter.register_routes(
        FastAPI(lifespan=closing_lifespan),
        UPSTREAM_URL,
        store,
        sheet,
        "off",
        transport=StreamingASGITransport(upstream),
    )

    async with as_client(app) as client:
        await client.post(
            "/v1/messages", content=make_body(MODEL_CACHED), headers=ANTHROPIC_HEADERS
        )

    assert store.last["decision"].action == "STAY"
    assert store.last["decision"].reason == "router disabled"


async def test_session_id_from_header_else_default(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    await client.post(
        "/v1/messages",
        content=make_body(MODEL_CACHED),
        headers={**ANTHROPIC_HEADERS, "x-tamias-session": "sess-42"},
    )
    assert store.last["session_id"] == "sess-42"

    await client.post("/v1/messages", content=make_body(MODEL_CACHED), headers=ANTHROPIC_HEADERS)
    assert store.last["session_id"] == "default"


async def test_missing_model_is_logged_as_unknown(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    await client.post("/v1/messages", content=b'{"messages": []}', headers=ANTHROPIC_HEADERS)

    row = store.last
    assert row["model_requested"] == "unknown"
    assert row["model_used"] == "unknown"
    # The counts still arrived, but no model means no rate, so no cost.
    assert row["usage"] == FULL_USAGE
    assert row["cost"].usd is None
    assert "unknown" in row["cost"].formula


async def test_no_prompt_or_response_text_is_logged(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    body = make_body(MODEL_CACHED)
    body = body.replace(b'"hi"', b'"SECRET-PROMPT-MARKER"')

    await client.post("/v1/messages", content=body, headers=ANTHROPIC_HEADERS)

    logged = repr(store.last)
    assert "SECRET-PROMPT-MARKER" not in logged
    assert "alpha" not in logged
    assert "SECRET-PROMPT-MARKER" in body.decode()


async def test_the_route_is_matched_before_a_catch_all_relay(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["POST", "GET"])
    async def relay(path: str, request: Request) -> Response:
        return JSONResponse({"relayed": path})

    anthropic_adapter.register_routes(
        app, UPSTREAM_URL, store, sheet, "shadow", transport=StreamingASGITransport(upstream)
    )

    async with as_client(app) as client:
        response = await client.post(
            "/v1/messages", content=make_body(MODEL_CACHED), headers=ANTHROPIC_HEADERS
        )
        relayed = await client.get("/v1/anything")

    # The catch-all was registered first; /v1/messages must still be logged.
    assert response.json()["type"] == "message"
    assert store.last["model_requested"] == MODEL_CACHED
    assert relayed.json()["relayed"] == "v1/anything"


async def test_registers_next_to_the_chat_completions_route(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    # create_app wires the Anthropic route itself, so no extra call is needed.
    app = proxy.create_app(
        UPSTREAM_URL, store, sheet, "shadow", transport=StreamingASGITransport(upstream)
    )

    async with as_client(app) as client:
        response = await client.post(
            "/v1/messages", content=make_body(MODEL_CACHED), headers=ANTHROPIC_HEADERS
        )
        relayed = await client.get("/v1/models")

    assert response.json()["type"] == "message"
    assert store.last["model_requested"] == MODEL_CACHED
    assert relayed.status_code == 200


async def test_an_existing_upstream_client_is_reused(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = FastAPI()
    client = httpx.AsyncClient(transport=StreamingASGITransport(upstream))
    app.state.upstream_client = client

    anthropic_adapter.register_routes(app, UPSTREAM_URL, store, sheet, "shadow")

    assert app.state.upstream_client is client
    await client.aclose()


async def test_the_client_it_creates_is_closed_by_the_lifespan(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = anthropic_adapter.register_routes(
        FastAPI(lifespan=closing_lifespan),
        UPSTREAM_URL,
        store,
        sheet,
        "shadow",
        transport=StreamingASGITransport(upstream),
    )
    client = app.state.upstream_client
    assert isinstance(client, httpx.AsyncClient)
    assert not client.is_closed

    async with app.router.lifespan_context(app):
        pass

    assert client.is_closed
