"""Proxy behaviour tests. Everything runs in-process: no socket is opened."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mock_upstream import (
    MODEL_ERROR,
    MODEL_NO_USAGE,
    STREAM_TEXTS,
    StreamingASGITransport,
    create_mock_upstream,
)

from tamias import pricing, proxy
from tamias import store as store_module
from tamias.pricing import ModelPrice, PriceSheet
from tamias.router import RouterConfig
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
JSON_HEADERS = {"content-type": "application/json"}
NO_USAGE = Usage(None, None, None, None)
FULL_USAGE = Usage(11, 7, 3, None)
RAW_USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "total_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 3},
}


def test_usage_cost_is_read_when_openrouter_reports_it() -> None:
    assert proxy.to_usage({**RAW_USAGE, "cost": 0.0025}).provider_cost_usd == 0.0025


def test_usage_cost_is_unknown_when_absent() -> None:
    assert proxy.to_usage(RAW_USAGE).provider_cost_usd is None


def test_final_stream_usage_cost_is_read_from_a_canned_chunk() -> None:
    payload = proxy.sse_payload(
        b'data: {"usage":{"prompt_tokens":11,"completion_tokens":7,"cost":0.0025}}'
    )
    assert payload is not None
    assert proxy.to_usage(payload["usage"]).provider_cost_usd == 0.0025


class FakeStore:
    """Collects the rows the proxy logs, with store.Store's exact signature."""

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


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sheet() -> PriceSheet:
    return PriceSheet(
        date="2026-10-04",
        models={"gpt-mock": ModelPrice(input=3.0, output=15.0, cached_input=0.3, cache_write=3.75)},
    )


@pytest.fixture
def upstream() -> FastAPI:
    return create_mock_upstream()


@pytest.fixture
def app(upstream: FastAPI, store: FakeStore, sheet: PriceSheet) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL, store, sheet, "shadow", transport=StreamingASGITransport(upstream)
    )


@pytest.fixture
def injecting_app(upstream: FastAPI, store: FakeStore, sheet: PriceSheet) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "shadow",
        inject_usage=True,
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


@pytest.fixture
async def injecting_client(injecting_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with as_client(injecting_app) as http_client:
        yield http_client


def make_body(model: str, **extra: Any) -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(extra)
    return json.dumps(body).encode()


async def read_sse(chunks: AsyncIterator[bytes]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            payloads.extend(parse_sse_line(line))
    return payloads


def parse_sse_line(line: bytes) -> list[dict[str, Any]]:
    line = line.strip()
    if not line.startswith(b"data:") or b"[DONE]" in line:
        return []
    return [json.loads(line[len(b"data:") :])]


def deltas_of(payloads: list[dict[str, Any]]) -> list[str]:
    deltas: list[str] = []
    for payload in payloads:
        for choice in payload.get("choices") or []:
            content = choice.get("delta", {}).get("content", "")
            if content:
                deltas.append(content)
    return deltas


def usage_of(payloads: list[dict[str, Any]]) -> Any:
    return next((payload["usage"] for payload in payloads if payload.get("usage")), None)


def forwarded_body(echo: dict[str, Any]) -> Any:
    """The body the upstream actually received, recovered from its echo."""
    return json.loads(base64.b64decode(echo["raw_body_b64"]))


async def test_body_forwarded_byte_identical(client: httpx.AsyncClient, store: FakeStore) -> None:
    raw = (
        b'{\n  "model": "gpt-mock",\n  "messages": [ {"role":"user",'
        b'"content":"caf\xc3\xa9 \\u00e9"} ],\n  "temperature":0.9,\n  "seed": 7\n}\n'
    )

    response = await client.post("/v1/chat/completions", content=raw, headers=JSON_HEADERS)

    assert response.status_code == 200
    assert base64.b64decode(response.json()["echo"]["raw_body_b64"]) == raw
    assert store.last["model_requested"] == "gpt-mock"


async def test_request_usage_cost_is_opt_in(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "shadow",
        request_usage_cost=True,
        transport=StreamingASGITransport(upstream),
    )
    async with as_client(app) as cost_client:
        response = await cost_client.post(
            "/v1/chat/completions", content=make_body("gpt-mock"), headers=JSON_HEADERS
        )

    assert forwarded_body(response.json()["echo"])["usage"] == {"include": True}


async def test_headers_forwarded_without_hop_by_hop_or_accept_encoding(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        content=make_body("gpt-mock"),
        headers={
            "content-type": "application/json",
            "x-agent-build": "cli/9.9",
            "accept-encoding": "gzip-not-forwarded",
        },
    )

    seen = response.json()["echo"]["headers"]
    assert seen["x-agent-build"] == "cli/9.9"
    assert "not-forwarded" not in seen.get("accept-encoding", "")


async def test_stream_options_not_injected_by_default(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    raw = make_body("gpt-mock", stream=True)

    response = await client.post("/v1/chat/completions", content=raw, headers=JSON_HEADERS)
    payloads = await read_sse(single_chunk(response.content))

    assert base64.b64decode(payloads[0]["echo"]["raw_body_b64"]) == raw
    assert "stream_options" not in forwarded_body(payloads[0]["echo"])
    assert usage_of(payloads) is None
    assert store.last["usage"] == NO_USAGE


async def test_inject_usage_flag_is_opt_in(
    injecting_client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await injecting_client.post(
        "/v1/chat/completions",
        content=make_body("gpt-mock", stream=True),
        headers=JSON_HEADERS,
    )
    payloads = await read_sse(single_chunk(response.content))

    assert forwarded_body(payloads[0]["echo"])["stream_options"] == {"include_usage": True}
    assert usage_of(payloads) == RAW_USAGE
    assert store.last["usage"] == FULL_USAGE


async def test_streamed_chunks_arrive_in_order(client: httpx.AsyncClient, store: FakeStore) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        content=make_body("gpt-mock", stream=True, stream_options={"include_usage": True}),
        headers=JSON_HEADERS,
    ) as response:
        assert response.status_code == 200
        payloads = await read_sse(response.aiter_bytes())

    assert deltas_of(payloads) == list(STREAM_TEXTS)
    assert payloads[-1]["choices"] == []
    assert usage_of(payloads) == RAW_USAGE
    assert store.last["status"] == "200"
    assert store.last["usage"] == FULL_USAGE


async def test_stream_is_relayed_without_buffering(client: httpx.AsyncClient) -> None:
    started = time.perf_counter()
    first_chunk_at: float | None = None

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        content=make_body("gpt-mock", stream=True),
        headers=JSON_HEADERS,
    ) as response:
        async for _chunk in response.aiter_bytes():
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter() - started

    total = time.perf_counter() - started
    assert first_chunk_at is not None
    assert first_chunk_at < total / 2


async def test_usage_and_cost_are_logged(
    client: httpx.AsyncClient, store: FakeStore, sheet: PriceSheet
) -> None:
    await client.post("/v1/chat/completions", content=make_body("gpt-mock"))

    row = store.last
    assert row["usage"] == FULL_USAGE
    assert row["model_requested"] == "gpt-mock"
    assert row["model_used"] == "gpt-mock"
    assert row["status"] == "200"
    assert row["latency_ms"] >= 0
    assert row["cost"] == pricing.compute_cost("gpt-mock", FULL_USAGE, sheet)
    assert row["cost"].price_sheet_date == "2026-10-04"
    assert row["cost"].usd is None
    assert "cache_write" in row["cost"].formula


async def test_missing_model_is_logged_as_unknown(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    await client.post("/v1/chat/completions", content=b'{"messages": []}', headers=JSON_HEADERS)

    assert store.last["model_requested"] == "unknown"
    assert store.last["model_used"] == "unknown"
    assert store.last["cost"].usd is None


async def test_shadow_decision_is_recorded_but_not_applied(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    body = {
        "model": "gpt-mock",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "1", "name": "read", "content": "file body"},
        ],
    }
    for _ in range(4):
        response = await client.post(
            "/v1/chat/completions", content=json.dumps(body).encode(), headers=JSON_HEADERS
        )

    decision = store.last["decision"]
    assert decision.persisted_only is True
    # The 4th request of the session is the first past the default min_gap of 3,
    # so the router would switch. Shadow mode records that and changes nothing.
    assert decision.action == "SWITCH"
    assert decision.target_model == ""
    assert "read" in decision.reason
    # The bytes the upstream received are unchanged: still the requested model,
    # and still the exact body that was posted.
    assert forwarded_body(response.json()["echo"])["model"] == "gpt-mock"
    assert base64.b64decode(response.json()["echo"]["raw_body_b64"]) == json.dumps(body).encode()
    assert store.last["model_requested"] == store.last["model_used"] == "gpt-mock"


async def test_shadow_mode_switch_names_the_configured_cheap_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "shadow",
        config=RouterConfig(cheap_model="gpt-cheap", strong_model="gpt-mock", min_gap=0),
        transport=StreamingASGITransport(upstream),
    )
    body = {
        "model": "gpt-mock",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "file body"},
        ],
    }
    async with as_client(app) as shadow_client:
        response = await shadow_client.post(
            "/v1/chat/completions", content=json.dumps(body).encode(), headers=JSON_HEADERS
        )

    decision = store.last["decision"]
    assert decision.action == "SWITCH"
    assert decision.target_model == "gpt-cheap"
    assert decision.persisted_only is True
    # Shadow mode still forwarded the strong model.
    assert forwarded_body(response.json()["echo"])["model"] == "gpt-mock"


async def test_active_mode_is_the_only_mode_that_rewrites_the_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "active",
        config=RouterConfig(cheap_model="gpt-cheap", strong_model="gpt-mock", min_gap=0),
        transport=StreamingASGITransport(upstream),
    )
    body = {
        "model": "gpt-mock",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "file body"},
        ],
    }
    async with as_client(app) as active_client:
        response = await active_client.post(
            "/v1/chat/completions", content=json.dumps(body).encode(), headers=JSON_HEADERS
        )

    assert store.last["decision"].persisted_only is False
    assert forwarded_body(response.json()["echo"])["model"] == "gpt-cheap"


async def test_router_off_records_a_stay_and_never_asks_the_router(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "off",
        config=RouterConfig(cheap_model="gpt-cheap", strong_model="gpt-mock", min_gap=0),
        transport=StreamingASGITransport(upstream),
    )
    body = {
        "model": "gpt-mock",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "file body"},
        ],
    }
    async with as_client(app) as off_client:
        response = await off_client.post(
            "/v1/chat/completions", content=json.dumps(body).encode(), headers=JSON_HEADERS
        )

    decision = store.last["decision"]
    assert decision.action == "STAY"
    assert decision.reason == "router disabled"
    assert forwarded_body(response.json()["echo"])["model"] == "gpt-mock"


async def test_session_counter_counts_the_first_request(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    # SessionState.request_index is "how many requests have been seen", so the
    # nth request of a session must be planned with request_index == n - 1.
    planned: list[int] = []

    def spy(body: dict[str, Any], state: Any, config: Any = None) -> Decision:
        planned.append(state.request_index)
        return Decision(action="STAY", target_model=None, reason="spy")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(proxy.router, "decide", spy)
    try:
        for _ in range(4):
            await client.post(
                "/v1/chat/completions",
                content=make_body("gpt-mock"),
                headers={"x-tamias-session": "sess-counter"},
            )
    finally:
        monkeypatch.undo()

    assert planned == [0, 1, 2, 3]


async def test_session_id_from_header_else_derived(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    await client.post(
        "/v1/chat/completions",
        content=make_body("gpt-mock"),
        headers={"x-tamias-session": "sess-42"},
    )
    assert store.last["session_id"] == "sess-42"

    await client.post("/v1/chat/completions", content=make_body("gpt-mock"))
    assert store.last["session_id"].startswith("auto-")


async def test_header_session_id_wins_over_the_derived_id(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    """A client that labels its own session is never overridden by derivation."""
    body = {"model": "gpt-mock", "messages": [{"role": "user", "content": "hello there"}]}

    await client.post(
        "/v1/chat/completions",
        content=json.dumps(body).encode(),
        headers={**JSON_HEADERS, "x-tamias-session": "sess-explicit"},
    )
    with_header = store.last["session_id"]

    await client.post(
        "/v1/chat/completions", content=json.dumps(body).encode(), headers=JSON_HEADERS
    )
    without_header = store.last["session_id"]

    assert with_header == "sess-explicit"
    assert without_header.startswith("auto-")
    assert with_header != without_header


async def test_one_conversation_keeps_one_session_id_across_steps(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    """Each step of one conversation repeats the same identity inputs."""
    system = {"role": "system", "content": "you are a coding agent"}
    user = {"role": "user", "content": "fix the failing test"}
    steps = [
        [system, user],
        [system, user, {"role": "assistant", "content": "looking"}],
        [
            system,
            user,
            {"role": "assistant", "content": "looking"},
            {"role": "tool", "tool_call_id": "1", "content": "file body"},
        ],
        [
            system,
            user,
            {"role": "assistant", "content": "patched"},
            {"role": "user", "content": "now add a regression test"},
        ],
    ]

    for messages in steps:
        body = {"model": "gpt-mock", "messages": messages}
        await client.post(
            "/v1/chat/completions",
            content=json.dumps(body).encode(),
            headers={**JSON_HEADERS, "authorization": "Bearer k"},
        )

    session_ids = {row["session_id"] for row in store.rows}
    assert len(store.rows) == 4
    assert len(session_ids) == 1
    assert session_ids.pop().startswith("auto-")


async def test_two_conversations_get_different_session_ids(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    conversations = [
        [{"role": "system", "content": "sys A"}, {"role": "user", "content": "question A"}],
        [{"role": "system", "content": "sys B"}, {"role": "user", "content": "question B"}],
    ]

    for messages in conversations:
        body = {"model": "gpt-mock", "messages": messages}
        await client.post(
            "/v1/chat/completions",
            content=json.dumps(body).encode(),
            headers={**JSON_HEADERS, "authorization": "Bearer k"},
        )

    first, second = (row["session_id"] for row in store.rows)
    assert first.startswith("auto-")
    assert second.startswith("auto-")
    assert first != second


async def test_interleaved_sessions_keep_independent_counters(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    """Regression: the counter bug was one shared counter for everything.

    With no usable session header every request used to fall into a single
    "default" session, so request_index was really "how many requests this proxy
    has ever seen".  One conversation's traffic therefore advanced another
    conversation's hysteresis gap, and a chatty agent could push a brand new
    conversation straight past min_gap and into a recorded SWITCH.
    """
    planned: list[tuple[str, int]] = []

    def spy(body: dict[str, Any], state: Any, config: Any = None) -> Decision:
        planned.append((state.session_id, state.request_index))
        return Decision(action="STAY", target_model=None, reason="spy")

    def conversation(label: str) -> bytes:
        return json.dumps(
            {
                "model": "gpt-mock",
                "messages": [{"role": "user", "content": f"working on {label}"}],
            }
        ).encode()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(proxy.router, "decide", spy)
    try:
        for step in range(3):
            await client.post(
                "/v1/chat/completions", content=conversation("alpha"), headers=JSON_HEADERS
            )
            await client.post(
                "/v1/chat/completions", content=conversation("beta"), headers=JSON_HEADERS
            )
            assert len(planned) == 2 * (step + 1)
    finally:
        monkeypatch.undo()

    by_session: dict[str, list[int]] = {}
    for session_id, index in planned:
        by_session.setdefault(session_id, []).append(index)

    assert len(by_session) == 2, "the two conversations shared one session"
    assert sorted(by_session.values()) == [[0, 1, 2], [0, 1, 2]]


async def test_no_message_text_reaches_the_database_file(
    upstream: FastAPI, sheet: PriceSheet, tmp_path: Path
) -> None:
    """The log on disk holds session ids and counts, never conversation text."""
    db_path = tmp_path / "requests.db"
    db_store = store_module.Store(db_path)
    app = proxy.create_app(
        UPSTREAM_URL, db_store, sheet, "shadow", transport=StreamingASGITransport(upstream)
    )
    secrets = ["hunter2-api-key", "please-rewrite-the-parser", "confidential-incident-42"]

    async with as_client(app) as db_client:
        response = await db_client.post(
            "/v1/chat/completions",
            content=json.dumps(
                {
                    "model": "gpt-mock",
                    "messages": [
                        {"role": "system", "content": secrets[1]},
                        {"role": "user", "content": secrets[2]},
                    ],
                }
            ).encode(),
            headers={**JSON_HEADERS, "authorization": f"Bearer {secrets[0]}"},
        )
        assert response.status_code == 200

    assert store_module.Store(db_path).rows(), "expected the request to be logged"

    raw = db_path.read_bytes()
    lowered = raw.lower()
    for secret in secrets:
        assert secret.encode() not in lowered, f"{secret!r} was written to the database"
    assert b"auto-" in raw


async def test_upstream_500_is_passed_through(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post("/v1/chat/completions", content=make_body(MODEL_ERROR))

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "mock upstream failure"
    assert store.last["status"] == "500"
    assert store.last["usage"] == NO_USAGE


async def test_missing_usage_logs_none(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post("/v1/chat/completions", content=make_body(MODEL_NO_USAGE))

    assert response.status_code == 200
    assert "usage" not in response.json()
    assert store.last["usage"] == NO_USAGE


async def test_stream_without_usage_chunk_logs_none(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post(
        "/v1/chat/completions", content=make_body(MODEL_NO_USAGE, stream=True)
    )
    payloads = await read_sse(single_chunk(response.content))

    assert deltas_of(payloads) == list(STREAM_TEXTS)
    assert usage_of(payloads) is None
    assert store.last["usage"] == NO_USAGE
    assert store.last["model_used"] == MODEL_NO_USAGE


async def test_invalid_json_body_returns_400(client: httpx.AsyncClient, store: FakeStore) -> None:
    response = await client.post("/v1/chat/completions", content=b"{not json", headers=JSON_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert store.rows == []


async def test_non_object_json_body_returns_400(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    response = await client.post("/v1/chat/completions", content=b"[1, 2, 3]", headers=JSON_HEADERS)

    assert response.status_code == 400
    assert store.rows == []


async def test_other_paths_and_methods_pass_through(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    listing = await client.get("/v1/models")
    assert listing.status_code == 200
    assert listing.json()["echo"]["path"] == "/v1/models"
    assert listing.json()["echo"]["method"] == "GET"

    created = await client.post("/v1/embeddings", content=b'{"input":"x"}')
    assert created.status_code == 200
    assert created.json()["echo"]["method"] == "POST"
    assert created.json()["echo"]["raw_body_b64"] == base64.b64encode(b'{"input":"x"}').decode()

    deleted = await client.delete("/v1/threads/abc")
    assert deleted.status_code == 200
    assert deleted.json()["echo"]["path"] == "/v1/threads/abc"

    assert store.rows == []


async def test_passthrough_response_is_relayed(client: httpx.AsyncClient) -> None:
    async with client.stream("GET", "/v1/anything") as response:
        assert response.status_code == 200
        body = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert json.loads(body)["echo"]["path"] == "/v1/anything"


def single_chunk(content: bytes) -> AsyncIterator[bytes]:
    async def iterator() -> AsyncIterator[bytes]:
        yield content

    return iterator()
