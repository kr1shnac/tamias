"""Adapter failover: the Anthropic and Responses wires obey the same routing rules.

The chat route already turns a provider refusal (429, 404) into a retry on
another gated model and remembers the refusal for later requests.  The two
adapter routes were wired around that machinery: a limited model behind
``/v1/messages`` failed the turn, and a model no provider serves was relayed to
whichever root happened to be last.  These tests pin the shared behaviour down.

Everything runs in-process through an httpx mock transport; no socket is opened
and no real credential exists.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from tamias import proxy
from tamias.limits import LimitLedger
from tamias.pricing import ModelPrice, PriceSheet
from tamias.providers import Credential, Provider, ProviderCatalogue
from tamias.registry import ModelInfo, Registry
from tamias.types import CostBreakdown, Decision, Usage

UPSTREAM_URL = "http://single-upstream.invalid"
JSON_HEADERS = {"content-type": "application/json"}

PROVIDER_A = Provider(
    name="openrouter",
    base_url="https://openrouter.ai/api",
    key_source="env:OPENROUTER_API_KEY",
)
PROVIDER_B = Provider(
    name="nvidia",
    base_url="https://integrate.api.nvidia.com/v1",
    key_source="env:NVIDIA_API_KEY",
)

MODEL_A = "deepseek/deepseek-v4.1-flash"
MODEL_B = "meta/llama-3.1-8b"


class FakeStore:
    """Collects logged rows with Store's signature."""

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
                "status": status,
                "decision": decision,
                **provenance,
            }
        )
        return len(self.rows)

    @property
    def last(self) -> dict[str, Any]:
        assert self.rows
        return self.rows[-1]


def catalogue() -> ProviderCatalogue:
    return ProviderCatalogue(
        providers={PROVIDER_A.name: PROVIDER_A, PROVIDER_B.name: PROVIDER_B},
        credentials={
            PROVIDER_A.name: Credential("openrouter-secret"),
            PROVIDER_B.name: Credential("nvidia-secret"),
        },
    )


def registry() -> Registry:
    return Registry(
        {
            MODEL_A: ModelInfo(
                id=MODEL_A, provider="openrouter", context_window=1_000_000, tools=True
            ),
            MODEL_B: ModelInfo(id=MODEL_B, provider="nvidia", context_window=128_000, tools=True),
        }
    )


def sheet() -> PriceSheet:
    return PriceSheet(
        date="2026-11-01",
        models={
            MODEL_A: ModelPrice(input=0.2, output=0.8),
            MODEL_B: ModelPrice(input=0.0, output=0.0),
        },
    )


def anthropic_message(model: str) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def anthropic_stream(model: str) -> bytes:
    frames = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": model,
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        b"event: " + name.encode() + b"\ndata: " + json.dumps(payload).encode() + b"\n\n"
        for name, payload in frames
    )


def responses_object(model: str) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "model": model,
        "output": [],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


class Recorder:
    """Remembers every upstream request, and answers per host and wire."""

    def __init__(self, fail: dict[str, int] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        status = self.fail.get(host)
        if status is not None:
            return httpx.Response(
                status,
                json={"error": {"message": "rate limited"}},
                headers={"retry-after": "600"} if status == 429 else {},
            )
        body = json.loads(request.content)
        model = body.get("model", "unknown")
        if request.url.path == "/v1/messages":
            if body.get("stream"):
                return httpx.Response(
                    200,
                    content=anthropic_stream(model),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=anthropic_message(model))
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json=responses_object(model))
        return httpx.Response(404, json={"error": {"message": "wrong wire"}})


def build(
    recorder: Recorder,
    store: FakeStore,
    *,
    limits: LimitLedger | None = None,
) -> Any:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet(),
        "shadow",
        registry=registry(),
        providers=catalogue(),
        limits=limits if limits is not None else LimitLedger(":memory:"),
        transport=httpx.MockTransport(recorder),
    )


async def post(app: Any, path: str, payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        return await client.post(path, content=json.dumps(payload), headers=JSON_HEADERS)


def message_body(model: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(extra)
    return body


async def test_a_limit_moves_a_messages_request_to_another_provider():
    recorder = Recorder(fail={"openrouter.ai": 429})
    store = FakeStore()
    app = build(recorder, store)

    response = await post(app, "/v1/messages", message_body(MODEL_A))

    assert response.status_code == 200
    assert [request.url.host for request in recorder.requests] == [
        "openrouter.ai",
        "integrate.api.nvidia.com",
    ]
    assert store.last["decision"].action == "SWITCH"
    assert MODEL_B in store.last["decision"].reason
    assert store.last["model_used"] == MODEL_B
    assert response.json()["content"][0]["text"] == "ok"


async def test_a_messages_stream_is_retried_before_the_first_byte():
    recorder = Recorder(fail={"openrouter.ai": 429})
    store = FakeStore()
    app = build(recorder, store)

    response = await post(app, "/v1/messages", message_body(MODEL_A, stream=True))

    assert response.status_code == 200
    assert [request.url.host for request in recorder.requests] == [
        "openrouter.ai",
        "integrate.api.nvidia.com",
    ]
    assert "message_stop" in response.text
    assert store.last["status"] == "200"
    assert store.last["model_used"] == MODEL_B


async def test_a_messages_limit_is_remembered_for_the_next_request():
    recorder = Recorder(fail={"openrouter.ai": 429})
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)

    await post(app, "/v1/messages", message_body(MODEL_A))
    assert ledger.is_limited("openrouter", MODEL_A)

    before = len(recorder.requests)
    await post(app, "/v1/messages", message_body(MODEL_A))

    later = recorder.requests[before:]
    assert [request.url.host for request in later] == ["integrate.api.nvidia.com"]


async def test_a_messages_404_is_learned_as_unavailable_not_as_a_rate_limit():
    """A provider that does not serve a model gets the 12-hour miss, not the
    short rate-limit ladder, exactly as on the chat route."""
    recorder = Recorder(fail={"openrouter.ai": 404})
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)

    response = await post(app, "/v1/messages", message_body(MODEL_A))

    assert response.status_code == 200
    limit = ledger.get("openrouter", MODEL_A)
    assert limit is not None
    assert limit.status == 404
    assert "not offered" in limit.reason
    assert limit.remaining() > 3600, "a 404 is remembered for hours, not the 60s first strike"


async def test_a_messages_model_no_provider_serves_is_refused_not_relayed():
    recorder = Recorder()
    app = build(recorder, FakeStore())

    response = await post(app, "/v1/messages", message_body("nobody/knows-this"))

    assert response.status_code == 400
    assert "no configured provider serves" in response.json()["error"]["message"]
    assert recorder.requests == []


async def test_a_responses_limit_is_learned_and_never_retried():
    """The Responses route is meter-only: it records the refusal for the routes
    that do route, but it must not rewrite the client's model on its own."""
    recorder = Recorder(fail={"openrouter.ai": 429})
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)

    response = await post(app, "/v1/responses", {"model": MODEL_A, "input": "hi"})

    assert response.status_code == 429
    assert [request.url.host for request in recorder.requests] == ["openrouter.ai"]
    assert ledger.is_limited("openrouter", MODEL_A)


async def test_a_responses_model_no_provider_serves_is_refused_not_relayed():
    recorder = Recorder()
    app = build(recorder, FakeStore())

    response = await post(app, "/v1/responses", {"model": "nobody/knows-this", "input": "hi"})

    assert response.status_code == 400
    assert "no configured provider serves" in response.json()["error"]["message"]
    assert recorder.requests == []
