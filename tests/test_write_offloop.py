"""The metering write must leave the event loop.

``record`` runs on every metered route and it commits a SQLite row -- an fsync.
Done inline, one metered request stalls every other in-flight stream for the
length of that flush, which is a per-request latency floor the client pays
whether or not it is the one being logged.

These tests pin the behaviour rather than the mechanism: a store double that
records which thread called it proves the write happens somewhere other than
the loop, and a slow store proves the loop keeps running while it commits.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mock_upstream import StreamingASGITransport

from tamias import proxy
from tamias.demo_upstream import create_mock_upstream
from tamias.pricing import PriceSheet

UPSTREAM_URL = "http://upstream.invalid"
JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def upstream() -> FastAPI:
    return create_mock_upstream()


class ThreadRecordingStore:
    """A request log that remembers which thread appended, and can be slow."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.threads: list[int] = []
        self.rows: list[dict[str, Any]] = []
        self.session_salt: bytes | None = None
        self._delay = delay

    def log_request(self, *args: Any, **kwargs: Any) -> int:
        self.threads.append(threading.get_ident())
        if self._delay:
            # A blocking sleep stands in for the fsync: it must not stop the
            # loop from serving anything else.
            import time

            time.sleep(self._delay)
        self.rows.append({"args": args, "kwargs": kwargs})
        return len(self.rows)

    def close(self) -> None:  # pragma: no cover - nothing to release
        return


def sheet() -> PriceSheet:
    return PriceSheet(date="2026-11-09", models={}, simulated=False, source="test")


async def as_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


async def test_the_write_happens_off_the_event_loop(
    upstream: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ThreadRecordingStore()
    app = proxy.create_app(
        UPSTREAM_URL, store, sheet(), "off", transport=StreamingASGITransport(upstream)
    )
    async with await as_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model": "gpt-mock", "messages": []}',
            headers=JSON_HEADERS,
        )
    assert response.status_code == 200
    assert store.rows, "the request was logged"
    loop_thread = threading.get_ident()
    for wrote_from in store.threads:
        assert wrote_from != loop_thread, "the ledger write ran on the event loop"


async def test_a_slow_write_does_not_stall_other_requests(
    upstream: FastAPI,
) -> None:
    store = ThreadRecordingStore(delay=0.15)
    app = proxy.create_app(
        UPSTREAM_URL, store, sheet(), "off", transport=StreamingASGITransport(upstream)
    )
    async with await as_client(app) as client:
        # A second request must not wait out the 150 ms the first one spends
        # committing; the two sleeps overlap instead of queueing behind it.
        async def one() -> int:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"model": "gpt-mock", "messages": []}',
                headers=JSON_HEADERS,
            )
            return response.status_code

        started = asyncio.get_running_loop().time()
        codes = await asyncio.gather(one(), one())
        elapsed = asyncio.get_running_loop().time() - started
    assert codes == [200, 200]
    assert elapsed < 0.29, f"two concurrent metered requests took {elapsed:.2f}s"
