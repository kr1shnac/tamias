"""Company limits at the gateway: budget, sub-caps, requests and tokens.

The claim these tests defend: a key that carries a limit is refused *before* the
request reaches a provider, with the status and the arithmetic that say which
limit stopped it -- 402 for money, 429 for rate -- and the refusal binds every
metered route.  The converse matters just as much: a key with no limits is
untouched, and settlement charges what was really spent rather than the ceiling.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mock_upstream import StreamingASGITransport

from tamias import proxy
from tamias.budget import estimate_tokens, from_micros, to_micros
from tamias.keys import KeyStore
from tamias.pricing import ModelPrice, PriceSheet
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
CHEAP = "m-cheap"
STRONG = "m-strong"
UNPRICED = "m-unpriced"
JSON_HEADERS = {"content-type": "application/json"}
# Any cutoff early enough to include every row a test writes.
EPOCH = "1970-01-01T00:00:00.000+00:00"
CHAT = "/v1/chat/completions"
MESSAGES = "/v1/messages"
RESPONSES = "/v1/responses"


class FakeStore:
    """Collects exactly the fields the proxy logs."""

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
                "model_requested": model_requested,
                "model_used": model_used,
                "usage": usage,
                "cost": cost,
                "status": status,
                **provenance,
            }
        )
        return len(self.rows)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def sheet() -> PriceSheet:
    price = ModelPrice(input=1.0, output=2.0)
    return PriceSheet(date="2026-10-15", models={CHEAP: price, STRONG: price})


@pytest.fixture
def hits() -> list[str]:
    return []


@pytest.fixture
def upstream(hits: list[str]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        body = json.loads(await request.body())
        hits.append(str(body.get("model")))
        return JSONResponse(
            {
                "id": "chat-1",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        body = json.loads(await request.body())
        hits.append(str(body.get("model")))
        return JSONResponse(
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": body.get("model"),
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        )

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        body = json.loads(await request.body())
        hits.append(str(body.get("model")))
        return JSONResponse(
            {
                "id": "resp-1",
                "model": body.get("model"),
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def catch_all(path: str, request: Request) -> Response:
        hits.append(path)
        return JSONResponse({"ok": True, "path": path})

    return app


def build_app(
    upstream: FastAPI,
    store: FakeStore,
    sheet: PriceSheet,
    key_store: KeyStore,
    *,
    ceiling: str = "0.25",
) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "off",
        transport=StreamingASGITransport(upstream),
        key_store=key_store,
        request_ceiling_usd=ceiling,
    )


def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


def body_of(model: str, **extra: Any) -> bytes:
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}
    ).encode()


def body_for(path: str, model: str, **extra: Any) -> bytes:
    if path == RESPONSES:
        return json.dumps({"model": model, "input": "hi", **extra}).encode()
    return body_of(model, **extra)


def auth(token: str) -> dict[str, str]:
    return {**JSON_HEADERS, "authorization": f"Bearer {token}"}


async def post(
    client: httpx.AsyncClient, path: str, token: str, model: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        path, content=body_for(path, model, **extra), headers=auth(token)
    )


# --- money limits -------------------------------------------------------------


async def test_a_second_request_is_refused_with_402_once_the_budget_is_committed(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        key, token = keys.issue(label="alice", models=[CHEAP], budget_usd="0.25")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            first = await post(client, CHAT, token, CHEAP)
            assert first.status_code == 200
            # The first request settled for a fraction of a cent, and the whole
            # budget is already smaller than one more ceiling, so the key stops.
            second = await post(client, CHAT, token, CHEAP)
            assert second.status_code == 402
            assert second.json()["error"]["type"] == "insufficient_quota"
            assert key.key_id in second.json()["error"]["message"]
    finally:
        keys.close()


async def test_the_402_states_the_ledgers_own_arithmetic(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], budget_usd="0.25")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            await post(client, CHAT, token, CHEAP)
            refusal = (await post(client, CHAT, token, CHEAP)).json()["error"]["message"]
        assert "budget exhausted" in refusal
        # The numbers are the ledger's own, formatted exactly as it holds them.
        cost = Decimal(str(store.rows[0]["cost"].usd))
        assert f"{from_micros(to_micros(cost))} spent" in refusal
        assert "0.000000 reserved" in refusal
        assert "0.25 needed for one request" in refusal
    finally:
        keys.close()


async def test_a_refused_request_is_never_forwarded_never_logged(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet, hits: list[str]
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], budget_usd="0.25")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, CHAT, token, CHEAP)).status_code == 200
            assert (await post(client, CHAT, token, CHEAP)).status_code == 402
        # One call reached the provider and produced one log row; the refused one
        # produced neither.
        assert hits == [CHEAP]
        assert len(store.rows) == 1
    finally:
        keys.close()


@pytest.mark.parametrize("path", [CHAT, MESSAGES, RESPONSES])
async def test_every_metered_route_reserves_and_refuses(
    path: str, upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], budget_usd="0.25")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, path, token, CHEAP)).status_code == 200
            assert (await post(client, path, token, CHEAP)).status_code == 402
    finally:
        keys.close()


async def test_a_model_sub_cap_stops_that_model_and_leaves_the_others(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(
            label="alice",
            models=[CHEAP, STRONG],
            budget_usd="50.00",
            model_caps=[(CHEAP, "0.25")],
        )
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, CHAT, token, CHEAP)).status_code == 200
            capped = await post(client, CHAT, token, CHEAP)
            assert capped.status_code == 402
            assert "sub-cap" in capped.json()["error"]["message"]
            # The total is barely touched, so an uncapped model still fits.
            assert (await post(client, CHAT, token, STRONG)).status_code == 200
    finally:
        keys.close()


async def test_a_budgeted_key_is_refused_a_model_the_sheet_cannot_price(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        keys.issue(label="alice", models=[UNPRICED], budget_usd="5.00")
        # The key's own allowlist is unrestricted here, so only pricing stops it.
        _, token = keys.issue(label="bob", budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            refused = await post(client, CHAT, token, UNPRICED)
        assert refused.status_code == 403
        assert "price" in refused.json()["error"]["message"]
    finally:
        keys.close()


async def test_a_spent_budget_is_settled_from_the_real_cost(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        key, token = keys.issue(label="alice", models=[CHEAP], budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, CHAT, token, CHEAP)).status_code == 200
        ledger = app.state.budget_ledger
        assert ledger.spend_micros(key.key_id, None) > 0
        # The reservation was released when the request settled.
        assert ledger.reserved_micros(key.key_id, EPOCH) == 0
    finally:
        keys.close()


async def test_an_unmeasurable_cost_is_charged_the_ceiling_not_zero(
    store: FakeStore, sheet: PriceSheet
) -> None:
    """A usage the sheet cannot finish is charged the reservation, not nothing.

    Anthropic's ``input_tokens`` excludes cache reads and writes, so a usage
    missing those fields is UNKNOWN rather than small; the gateway must not let
    an unmeasured request pass through the budget for free.
    """
    upstream = FastAPI()

    @upstream.post("/v1/messages")
    async def messages(request: Request) -> Response:
        body = json.loads(await request.body())
        return JSONResponse(
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": body.get("model"),
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )

    keys = KeyStore(":memory:")
    try:
        key, token = keys.issue(label="alice", models=[CHEAP], budget_usd="0.25")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, MESSAGES, token, CHEAP)).status_code == 200
            refused = await post(client, MESSAGES, token, CHEAP)
        assert refused.status_code == 402
        ledger = app.state.budget_ledger
        assert ledger.spend_micros(key.key_id, None) == to_micros(Decimal("0.25"))
    finally:
        keys.close()


# --- rate limits --------------------------------------------------------------


async def test_rpm_refuses_the_request_after_the_keys_share_of_a_minute(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], rpm=1)
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, CHAT, token, CHEAP)).status_code == 200
            refused = await post(client, CHAT, token, CHEAP)
        assert refused.status_code == 429
        assert refused.json()["error"]["type"] == "rate_limit_exceeded"
        assert refused.headers["retry-after"] == "60"
        assert "requests" in refused.json()["error"]["message"]
    finally:
        keys.close()


async def test_tpm_reserves_the_prompt_estimate_before_the_request_is_sent(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """The very first request can be refused: the estimate is a pre-flight gate."""
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], tpm=1)
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            refused = await post(client, CHAT, token, CHEAP)
        assert refused.status_code == 429
        assert "tokens" in refused.json()["error"]["message"]
    finally:
        keys.close()


async def test_tpm_refuses_once_the_minute_holds_the_limit(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(
            label="alice", models=[CHEAP], tpm=estimate_tokens(body_of(CHEAP)) + 1
        )
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            assert (await post(client, CHAT, token, CHEAP)).status_code == 200
            # The settled request reported 7 tokens; one more estimate overflows.
            refused = await post(client, CHAT, token, CHEAP)
        assert refused.status_code == 429
    finally:
        keys.close()


# --- scope of the limits ------------------------------------------------------


async def test_a_limited_key_may_not_use_the_unmetered_passthrough_for_a_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            refused = await client.post(
                "/v1/embeddings",
                content=json.dumps({"model": CHEAP, "input": "hi"}).encode(),
                headers=auth(token),
            )
        assert refused.status_code == 403
        assert "metered" in refused.json()["error"]["message"]
    finally:
        keys.close()


async def test_a_limited_key_still_reads_a_passthrough_without_a_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            allowed = await client.get("/v1/models", headers=auth(token))
        assert allowed.status_code == 200
    finally:
        keys.close()


async def test_a_streamed_request_settles_its_reservation(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """The reservation is released even though the body outlives the handler."""
    keys = KeyStore(":memory:")
    try:
        key, token = keys.issue(label="alice", models=[CHEAP], budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            streamed = await post(client, CHAT, token, CHEAP, stream=True)
            assert streamed.status_code == 200
            assert len(streamed.content) > 0
        ledger = app.state.budget_ledger
        # Settlement happened in the streaming generator: an event exists.
        assert ledger.requests_in_window(key.key_id, EPOCH) == 1
        assert ledger.reserved_micros(key.key_id, EPOCH) == 0
    finally:
        keys.close()


async def test_a_key_with_no_limits_is_completely_untouched(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            for _ in range(3):
                assert (await post(client, CHAT, token, UNPRICED)).status_code == 200
            # The unmetered passthrough stays open to a key with no limits.
            assert (
                await client.post(
                    "/v1/embeddings",
                    content=json.dumps({"model": CHEAP, "input": "hi"}).encode(),
                    headers=auth(token),
                )
            ).status_code == 200
    finally:
        keys.close()


async def test_the_allowlist_still_applies_to_a_limited_key(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[CHEAP], budget_usd="5.00")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            refused = await post(client, CHAT, token, STRONG)
        assert refused.status_code == 403
    finally:
        keys.close()
