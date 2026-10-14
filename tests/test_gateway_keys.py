"""Company-key authentication and model allowlisting at the gateway.

The security-relevant claim these tests defend is narrow and checkable: with a
key store configured, a request that names a model is refused (403) before it is
forwarded or logged unless the caller's key allows that model - and the check
binds every route that can name a model, not just the chat endpoint.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mock_upstream import StreamingASGITransport

from tamias import proxy
from tamias.keys import KeyStore
from tamias.pricing import ModelPrice, PriceSheet
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://upstream.invalid"
ALLOWED = "m-ok"
DENIED = "m-no"
JSON_HEADERS = {"content-type": "application/json"}

_CHAT_PATHS = ("/v1/chat/completions", "/v1/messages", "/v1/responses")


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
        date="2026-10-14",
        models={
            ALLOWED: ModelPrice(input=1.0, output=2.0),
            DENIED: ModelPrice(input=1.0, output=2.0),
        },
    )


@pytest.fixture
def upstream() -> FastAPI:
    """Valid, minimal shapes for all three model-bearing APIs plus a catch-all."""

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        body = json.loads(await request.body())
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

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        body = json.loads(await request.body())
        return JSONResponse(
            {
                "id": "resp-1",
                "model": body.get("model"),
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
    )
    async def catch_all(path: str, request: Request) -> Response:
        return JSONResponse({"ok": True, "path": path})

    return app


def build_app(
    upstream: FastAPI,
    store: FakeStore,
    sheet: PriceSheet,
    key_store: KeyStore,
    *,
    auth_token: str | None = None,
) -> FastAPI:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet,
        "off",
        transport=StreamingASGITransport(upstream),
        key_store=key_store,
        auth_token=auth_token,
    )


def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


def body_of(model: str) -> bytes:
    return json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()


def auth(token: str, **extra: str) -> dict[str, str]:
    return {**JSON_HEADERS, "authorization": f"Bearer {token}", **extra}


async def test_a_key_is_required_once_a_store_is_configured(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            missing = await client.post(
                "/v1/chat/completions", content=body_of(ALLOWED), headers=JSON_HEADERS
            )
            assert missing.status_code == 401

            forged = await client.post(
                "/v1/chat/completions",
                content=body_of(ALLOWED),
                headers=auth("vk-deadbeef-not-a-real-token"),
            )
            assert forged.status_code == 401
    finally:
        keys.close()


async def test_a_valid_key_reaches_an_allowed_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[ALLOWED])
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", content=body_of(ALLOWED), headers=auth(token)
            )
        assert response.status_code == 200
        assert store.last["model_requested"] == ALLOWED
    finally:
        keys.close()


async def test_a_restricted_key_is_refused_a_model_it_does_not_list(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[ALLOWED])
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", content=body_of(DENIED), headers=auth(token)
            )
        assert response.status_code == 403
        assert DENIED in response.text
        # The refusal happened before the request was forwarded or metered.
        assert store.rows == []
    finally:
        keys.close()


async def test_an_unrestricted_key_reaches_any_model(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="bob")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", content=body_of(DENIED), headers=auth(token)
            )
        assert response.status_code == 200
    finally:
        keys.close()


@pytest.mark.parametrize("path", _CHAT_PATHS)
async def test_the_allowlist_binds_every_model_bearing_route(
    path: str, upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[ALLOWED])
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            denied = await client.post(path, content=body_of(DENIED), headers=auth(token))
            allowed = await client.post(path, content=body_of(ALLOWED), headers=auth(token))
        assert denied.status_code == 403, path
        assert allowed.status_code == 200, path
        assert [row["model_requested"] for row in store.rows] == [ALLOWED]
    finally:
        keys.close()


async def test_the_allowlist_binds_the_passthrough_route(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[ALLOWED])
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            denied = await client.post(
                "/v1/embeddings",
                content=json.dumps({"model": DENIED, "input": "hi"}).encode(),
                headers=auth(token),
            )
            allowed = await client.post(
                "/v1/embeddings",
                content=json.dumps({"model": ALLOWED, "input": "hi"}).encode(),
                headers=auth(token),
            )
        assert denied.status_code == 403
        assert allowed.status_code == 200
    finally:
        keys.close()


async def test_a_passthrough_request_without_a_model_is_not_gated(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", models=[ALLOWED])
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 200
    finally:
        keys.close()


async def test_a_revoked_key_is_refused(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        key, token = keys.issue(label="alice", models=[ALLOWED])
        assert keys.revoke(key.key_id)
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", content=body_of(ALLOWED), headers=auth(token)
            )
        assert response.status_code == 401
    finally:
        keys.close()


async def test_the_operator_token_still_works_alongside_keys(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        app = build_app(upstream, store, sheet, keys, auth_token="s3cret-token")
        async with as_client(app) as client:
            accepted = await client.post(
                "/v1/chat/completions",
                content=body_of(DENIED),
                headers=auth("s3cret-token"),
            )
            wrong = await client.post(
                "/v1/chat/completions", content=body_of(DENIED), headers=auth("wrong")
            )
        # The shared token carries no identity, so an allowlist cannot apply.
        assert accepted.status_code == 200
        assert wrong.status_code == 401
    finally:
        keys.close()


async def test_the_keys_project_overrides_the_client_project_header(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    keys = KeyStore(":memory:")
    try:
        _, token = keys.issue(label="alice", project="acme")
        app = build_app(upstream, store, sheet, keys)
        async with as_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=body_of(ALLOWED),
                headers=auth(token, **{"x-tamias-project": "spoofed"}),
            )
        assert response.status_code == 200
        assert store.last["project"] == "acme"
    finally:
        keys.close()


async def test_no_key_store_means_no_key_check(
    upstream: FastAPI, store: FakeStore, sheet: PriceSheet
) -> None:
    """Without a key store the gateway keeps its old behaviour: open by default."""

    app = proxy.create_app(
        UPSTREAM_URL, store, sheet, "off", transport=StreamingASGITransport(upstream)
    )
    async with as_client(app) as client:
        response = await client.post(
            "/v1/chat/completions", content=body_of(ALLOWED), headers=JSON_HEADERS
        )
    assert response.status_code == 200
