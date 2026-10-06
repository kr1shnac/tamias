from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mock_upstream import StreamingASGITransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import proxy
from tamias.pricing import ModelPrice, PriceSheet
from tamias.store import PROJECT_MAX_CHARS, Store, sanitize_project


def test_project_sanitizer_accepts_only_bounded_identifiers() -> None:
    assert sanitize_project("work-1.alpha") == "work-1.alpha"
    for invalid in ("", "bad name", "slash/name", "x" * (PROJECT_MAX_CHARS + 1)):
        assert sanitize_project(invalid) is None


@pytest.mark.parametrize("value", [None, 0, [], {}])
def test_project_sanitizer_rejects_non_strings(value: object) -> None:
    assert sanitize_project(value) is None


async def test_proxy_propagates_projects_without_forwarding_or_logging_prompts(
    tmp_path: Path,
) -> None:
    upstream_headers: list[dict[str, str]] = []
    upstream = FastAPI()

    @upstream.post("/{path:path}")
    async def relay(path: str, request: Request):
        upstream_headers.append(dict(request.headers.items()))
        body = json.loads(await request.body())
        if path == "v1/responses":
            if body.get("stream"):
                payload = (
                    b"data: "
                    + json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "model",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        }
                    ).encode()
                    + b"\n\n"
                )
                return StreamingResponse(iter([payload]), media_type="text/event-stream")
            return JSONResponse(
                {"id": "resp-1", "model": "model", "usage": {"input_tokens": 1, "output_tokens": 1}}
            )
        if body.get("stream"):
            payload = (
                b"data: "
                + json.dumps(
                    {
                        "id": "chat-1",
                        "model": "model",
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ).encode()
                + b"\n\n"
            )
            return StreamingResponse(iter([payload]), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": "chat-1",
                "model": "model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            status_code=500 if body.get("model") == "error" else 200,
        )

    store = Store(tmp_path / "requests.db")
    sheet = PriceSheet(date="2026-10-06", models={"model": ModelPrice(input=1, output=1)})
    app = proxy.create_app(
        "http://upstream.invalid",
        store,
        sheet,
        transport=StreamingASGITransport(upstream),
    )
    body = {"model": "model", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]}
    try:
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
        ) as client:
            prefixed = await client.post("/p/alpha/v1/chat/completions", json=body)
            from_header = await client.post(
                "/v1/chat/completions", json=body, headers={proxy.PROJECT_HEADER: "beta"}
            )
            unassigned = await client.post("/v1/chat/completions", json=body)
            streamed = await client.post(
                "/p/stream/v1/chat/completions", json={**body, "stream": True}
            )
            errored = await client.post(
                "/p/error/v1/chat/completions", json={**body, "model": "error"}
            )
            response_row = await client.post("/p/response/v1/responses", json={"model": "model"})
            response_stream = await client.post(
                "/p/response-stream/v1/responses", json={"model": "model", "stream": True}
            )
            anthropic_row = await client.post(
                "/p/anthropic/v1/messages", json={"model": "model", "messages": [], "max_tokens": 1}
            )
            anthropic_stream = await client.post(
                "/p/anthropic-stream/v1/messages",
                json={"model": "model", "messages": [], "max_tokens": 1, "stream": True},
            )
            invalid = await client.post("/p/bad name/v1/chat/completions", json=body)

        assert prefixed.status_code == 200, prefixed.text
        assert from_header.status_code == 200, from_header.text
        assert unassigned.status_code == 200, unassigned.text
        assert streamed.status_code == 200
        assert errored.status_code == 500
        assert response_row.status_code == 200
        assert response_stream.status_code == 200
        assert anthropic_row.status_code == 200
        assert anthropic_stream.status_code == 200
        assert invalid.status_code == 400
        assert "invalid project" in invalid.json()["error"]["message"]
        assert [row["project"] for row in store.rows()] == [
            "alpha",
            "beta",
            None,
            "stream",
            "error",
            "response",
            "response-stream",
            "anthropic",
            "anthropic-stream",
        ]
        assert all(proxy.PROJECT_HEADER not in headers for headers in upstream_headers)
        assert all("SECRET-PROMPT" not in str(value) for row in store.rows() for value in row)
    finally:
        await app.state.upstream_client.aclose()
        store.close()
