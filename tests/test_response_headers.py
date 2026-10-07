"""The proxy must not describe a body it has already decoded.

httpx decodes `content-encoding` on the way in, so what the proxy holds is
plain bytes. `forward_response_headers` used to copy the upstream's
`content-encoding` straight through, which handed the client a plain body
labelled `gzip`. A client that honours the header then gunzips JSON, the read
fails, and the OpenAI SDK reports it as `APIConnectionError: Connection error.`
rather than anything that points at the proxy.

That was seen live: `tamias serve` returned 200 and logged it, while the agent
one hop away failed on every non-streaming call. Streaming escaped only because
the upstream does not gzip `text/event-stream`.

Nothing here opens a socket; the upstream is an ASGI app on
``StreamingASGITransport``, exactly as ``test_e2e`` uses it.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response as StarletteResponse
from mock_upstream import StreamingASGITransport

from tamias import anthropic_adapter, proxy, responses_adapter
from tamias.pricing import load_price_sheet
from tamias.store import Store

PROXY_URL = "http://proxy.test"
CHAT_PATH = proxy.CHAT_PATH
SHEET_DATE = "2026-10-04"

PRICES = f"""
date = "{SHEET_DATE}"

["gpt-strong"]
input = 3.0
output = 15.0
"""

PLAIN = {"id": "chatcmpl-x", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}


@pytest.mark.parametrize("module", [proxy, anthropic_adapter, responses_adapter])
def test_forward_response_headers_drops_content_encoding(module: Any) -> None:
    """All three response paths drop it; the body has already been decoded."""
    headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "1497",
            "x-request-id": "req-1",
        }
    )

    forwarded = {
        key.lower(): value
        for key, value in module.forward_response_headers(headers).items()
    }

    assert "content-encoding" not in forwarded, (
        f"{module.__name__}.forward_response_headers advertised gzip for a body "
        "httpx had already decoded"
    )
    assert "content-length" not in forwarded, "stale length must not survive"
    assert forwarded["content-type"] == "application/json"
    assert forwarded["x-request-id"] == "req-1"


def gzip_upstream() -> FastAPI:
    """An upstream that really does gzip, so httpx really does decode."""
    app = FastAPI()

    @app.post(CHAT_PATH)
    async def chat() -> Any:
        return StarletteResponse(
            content=gzip.compress(json.dumps(PLAIN).encode()),
            media_type="application/json",
            headers={"content-encoding": "gzip"},
        )

    return app


@pytest.fixture
def prices_path(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


async def _round_trip(prices_path: Path, tmp_path: Path) -> httpx.Response:
    upstream = gzip_upstream()
    app = proxy.create_app(
        "http://upstream.invalid",
        Store(tmp_path / "requests.db"),
        load_price_sheet(prices_path),
        "off",
        transport=StreamingASGITransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url=PROXY_URL, timeout=None
    ) as client:
        return await client.post(
            CHAT_PATH,
            json={"model": "gpt-strong", "messages": [{"role": "user", "content": "hi"}]},
        )


async def test_a_gzipped_upstream_body_reaches_the_client_as_plain_json(
    prices_path: Path, tmp_path: Path
) -> None:
    """End to end: gzip in, no content-encoding out, body still readable."""
    response = await _round_trip(prices_path, tmp_path)

    assert response.status_code == 200
    assert "content-encoding" not in {
        key.lower() for key in response.headers
    }, "the client is about to decode a body that is already plain"
    assert response.json() == PLAIN, response.content[:200]
    assert not response.content.startswith(b"\x1f\x8b"), "body must be the decoded bytes"


async def test_the_proxy_still_logs_the_request_it_forwarded(
    prices_path: Path, tmp_path: Path
) -> None:
    """Fixing the header must not have skipped the row."""
    db = tmp_path / "requests.db"
    response = await _round_trip(prices_path, tmp_path)

    assert response.status_code == 200
    assert Store(db).rows(), "the request was not logged"
