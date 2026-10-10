#!/usr/bin/env python3
"""Offline overhead benchmark for the metering proxy.

The question this answers is narrow and honest: how much latency and throughput
does the proxy add in front of an upstream, on this machine, with the log being
written?  It is an in-process ASGI benchmark -- both the upstream and the proxy
are ASGI apps driven by ``httpx.ASGITransport`` on one event loop -- so it
measures the proxy's own work (routing, body buffering, pricing, and the sqlite
write) and *not* uvicorn's socket handling.  It opens no network socket, so it
runs anywhere.

Two runs are timed with the same request body and the same client settings:

* ``baseline`` -- the request goes straight to the mock upstream;
* ``proxy``    -- the request goes through ``tamias.proxy.create_app`` in the
  default ``off`` router mode, so the delta is metering and logging only.

The reported numbers are single-run measurements, not a benchmark suite.  Treat
them as an order-of-magnitude check, not a guarantee about another machine.

Usage:
    python scripts/benchmark_overhead.py                 # human table
    python scripts/benchmark_overhead.py --json          # machine-readable
    python scripts/benchmark_overhead.py --requests 1000 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tamias import proxy
from tamias.pricing import ModelPrice, PriceSheet
from tamias.store import Store

UPSTREAM_URL = "http://upstream.invalid"
MODEL = "bench-model"
BODY: dict[str, Any] = {"model": MODEL, "messages": [{"role": "user", "content": "ping"}]}
USAGE: dict[str, Any] = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def _upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse(
            {
                "id": "chatcmpl-bench",
                "object": "chat.completion",
                "created": 1700000000,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
            }
        )

    return app


def _sheet() -> PriceSheet:
    return PriceSheet(date="2026-10-10", models={MODEL: ModelPrice(input=1.0, output=2.0)})


async def _drive(
    client: httpx.AsyncClient, path: str, requests: int, concurrency: int
) -> list[float]:
    """Send ``requests`` POSTs and return each request's wall-clock latency in ms."""
    latencies: list[float] = []
    lock = asyncio.Lock()

    async def one() -> None:
        started = time.perf_counter()
        response = await client.post(path, json=BODY)
        response.raise_for_status()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        async with lock:
            latencies.append(elapsed_ms)

    for start in range(0, requests, concurrency):
        batch = range(start, min(start + concurrency, requests))
        await asyncio.gather(*(one() for _ in batch))
    return latencies


def _summarise(latencies: list[float]) -> dict[str, float]:
    ordered = sorted(latencies)
    return {
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "max_ms": ordered[-1],
    }


async def _run(requests: int, warmup: int, concurrency: int) -> dict[str, Any]:
    upstream = _upstream()
    store = Store(":memory:")
    metered = proxy.create_app(
        UPSTREAM_URL,
        store,
        _sheet(),
        "off",
        transport=httpx.ASGITransport(upstream),
    )
    baseline_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(upstream), base_url="http://baseline.test"
    )
    proxy_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(metered), base_url="http://proxy.test"
    )
    try:
        for _ in range(warmup):
            await baseline_client.post("/v1/chat/completions", json=BODY)
            await proxy_client.post("/v1/chat/completions", json=BODY)

        started = time.perf_counter()
        baseline = await _drive(baseline_client, "/v1/chat/completions", requests, concurrency)
        baseline_s = time.perf_counter() - started

        rows_before = len(store.rows())
        started = time.perf_counter()
        through = await _drive(proxy_client, "/v1/chat/completions", requests, concurrency)
        through_s = time.perf_counter() - started
        rows_after = len(store.rows())
    finally:
        await baseline_client.aclose()
        await proxy_client.aclose()
        await metered.state.upstream_client.aclose()

    base = _summarise(baseline)
    prox = _summarise(through)
    rows = rows_after - rows_before
    assert rows == requests, f"expected {requests} logged rows, got {rows}"

    return {
        "requests": requests,
        "concurrency": concurrency,
        "baseline": {**base, "throughput_rps": requests / baseline_s},
        "proxy": {**prox, "throughput_rps": requests / through_s},
        "added_mean_ms": prox["mean_ms"] - base["mean_ms"],
        "added_p95_ms": prox["p95_ms"] - base["p95_ms"],
        "throughput_ratio": (requests / through_s) / (requests / baseline_s),
        "rows_logged": rows,
    }


def _print_table(result: dict[str, Any]) -> None:
    base = result["baseline"]
    prox = result["proxy"]
    print(
        f"tamias proxy overhead -- {result['requests']} requests, "
        f"concurrency {result['concurrency']}, in-process ASGI (no socket)\n"
    )
    header = f"{'path':<10} {'mean ms':>9} {'p50 ms':>9} {'p95 ms':>9} {'max ms':>9} {'req/s':>10}"
    print(header)
    print("-" * len(header))
    for name, stats in (("baseline", base), ("proxy", prox)):
        print(
            f"{name:<10} {stats['mean_ms']:>9.3f} {stats['p50_ms']:>9.3f} "
            f"{stats['p95_ms']:>9.3f} {stats['max_ms']:>9.3f} {stats['throughput_rps']:>10.1f}"
        )
    print(
        f"\nadded latency: {result['added_mean_ms']:+.3f} ms mean, "
        f"{result['added_p95_ms']:+.3f} ms p95; "
        f"throughput ratio {result['throughput_ratio']:.3f}x; "
        f"{result['rows_logged']} rows logged"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline tamias proxy overhead benchmark.")
    parser.add_argument("--requests", type=int, default=300, help="requests per path (default 300)")
    parser.add_argument(
        "--warmup", type=int, default=20, help="discarded warmup requests (default 20)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=1, help="in-flight requests (default 1)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    if args.requests < 1 or args.warmup < 0 or args.concurrency < 1:
        parser.error("--requests must be >= 1, --warmup >= 0, --concurrency >= 1")

    result = asyncio.run(_run(args.requests, args.warmup, args.concurrency))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
