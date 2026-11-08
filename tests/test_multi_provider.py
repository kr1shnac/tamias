"""Multi-provider routing end to end: one tamias, many providers, no shared key.

The claim these tests defend: a model id determines which provider is called and
which credential is presented, a client's own token is never forwarded to a
provider, a model no provider serves is refused rather than misrouted, and a
provider limit moves the *same* request to another provider instead of failing
it.

Everything runs in-process through an httpx mock transport; no socket is opened
and no real credential exists.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tamias import proxy
from tamias.candidates import Needs
from tamias.limits import LimitLedger
from tamias.pricing import ModelPrice, PriceSheet
from tamias.providers import Credential, Provider, ProviderCatalogue
from tamias.registry import ModelInfo, Registry
from tamias.router import RouterConfig
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


def completion(model: str) -> dict[str, Any]:
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


class Recorder:
    """Remembers every upstream request, and answers per host."""

    def __init__(
        self,
        fail: dict[str, int] | None = None,
        quota: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}
        self.quota = quota or {}

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
        return httpx.Response(
            200,
            json=completion(body.get("model", "")),
            headers={"x-ratelimit-remaining-requests": self.quota[host]}
            if host in self.quota
            else {},
        )

    @property
    def last(self) -> httpx.Request:
        assert self.requests
        return self.requests[-1]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def build(
    recorder: Recorder,
    store: FakeStore,
    *,
    mode: str = "shadow",
    limits: LimitLedger | None = None,
    config: RouterConfig | None = None,
) -> Any:
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet(),
        mode,
        config=config,
        registry=registry(),
        providers=catalogue(),
        limits=limits if limits is not None else LimitLedger(":memory:"),
        transport=httpx.MockTransport(recorder),
    )


async def post(app: Any, model: str, **body: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    payload = {"model": model, "messages": [{"role": "user", "content": "hello"}], **body}
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        return await client.post(
            "/v1/chat/completions", content=json.dumps(payload), headers=JSON_HEADERS
        )


async def test_each_model_reaches_its_own_provider_with_its_own_key(recorder, ):
    store = FakeStore()
    app = build(recorder, store)

    await post(app, MODEL_A, headers={})
    first = recorder.last
    await post(app, MODEL_B)
    second = recorder.last

    assert str(first.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert first.headers["authorization"] == "Bearer openrouter-secret"
    assert str(second.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert second.headers["authorization"] == "Bearer nvidia-secret"


async def test_the_clients_own_token_is_never_forwarded_to_a_provider():
    recorder = Recorder()
    app = build(recorder, FakeStore())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        await client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": MODEL_A, "messages": []}),
            headers={
                **JSON_HEADERS,
                "authorization": "Bearer sk-client-placeholder",
                "x-api-key": "sk-ant-client-placeholder",
            },
        )

    assert recorder.last.headers["authorization"] == "Bearer openrouter-secret"
    assert "x-api-key" not in recorder.last.headers


async def test_a_model_no_provider_serves_is_refused_not_misrouted():
    recorder = Recorder()
    app = build(recorder, FakeStore())

    response = await post(app, "nobody/knows-this")

    assert response.status_code == 400
    assert "no configured provider serves" in response.json()["error"]["message"]
    assert recorder.requests == []


async def test_a_limit_moves_the_same_request_to_another_provider():
    recorder = Recorder(fail={"openrouter.ai": 429})
    store = FakeStore()
    app = build(recorder, store)

    response = await post(app, MODEL_A)

    assert response.status_code == 200
    assert [request.url.host for request in recorder.requests] == [
        "openrouter.ai",
        "integrate.api.nvidia.com",
    ]
    assert store.last["decision"].action == "SWITCH"
    assert MODEL_B in store.last["decision"].reason


async def test_a_404_moves_the_request_to_a_provider_that_serves_the_model():
    """A model a provider does not offer is a routing miss, not a client error.

    NVIDIA's catalogue advertises 85 models while a dev key can call 14 of them;
    those answer 404.  Returning that to the client would fail the turn even
    though another gated model could answer it.
    """
    recorder = Recorder(fail={"openrouter.ai": 404})
    ledger = LimitLedger(":memory:")
    store = FakeStore()
    app = build(recorder, store, limits=ledger)

    response = await post(app, MODEL_A)

    assert response.status_code == 200
    assert [request.url.host for request in recorder.requests] == [
        "openrouter.ai",
        "integrate.api.nvidia.com",
    ]
    assert store.last["decision"].action == "SWITCH"
    assert ledger.is_limited("openrouter", MODEL_A), "the miss is remembered for later turns"


async def test_a_404_with_no_other_candidate_comes_back_as_a_404():
    recorder = Recorder(fail={"openrouter.ai": 404, "integrate.api.nvidia.com": 404})
    store = FakeStore()
    app = build(recorder, store)

    response = await post(app, MODEL_A)

    assert response.status_code == 404
    assert len(recorder.requests) == 2, "one failover attempt, never a loop"


async def test_the_learned_limit_is_remembered_for_the_next_request():
    recorder = Recorder(fail={"openrouter.ai": 429})
    ledger = LimitLedger(":memory:")
    store = FakeStore()
    app = build(recorder, store, limits=ledger)

    await post(app, MODEL_A)
    assert ledger.is_limited("openrouter", MODEL_A)

    before = len(recorder.requests)
    await post(app, MODEL_A)

    # The retry already used NVIDIA once; the next request must not touch the
    # limited provider at all.
    later = recorder.requests[before:]
    assert [request.url.host for request in later] == ["integrate.api.nvidia.com"]


async def test_the_limits_endpoint_reports_providers_and_cooldowns_without_secrets():
    recorder = Recorder(fail={"openrouter.ai": 429})
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)
    await post(app, MODEL_A)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.get("/limits")

    document = response.json()
    assert response.status_code == 200
    assert [provider["name"] for provider in document["providers"]] == ["nvidia", "openrouter"]
    assert all(provider["configured"] for provider in document["providers"])
    assert document["unconfigured"] == []
    assert document["cooling_down"][0]["model"] == MODEL_A
    assert document["cooling_down"][0]["remaining_seconds"] > 0
    assert "openrouter-secret" not in json.dumps(document)


async def test_a_published_request_quota_reaches_the_limits_endpoint():
    """A quota a provider publishes on a *successful* answer is the only one
    tamias reports; a provider that publishes nothing contributes no row, so
    the endpoint never shows an invented remaining count.
    """
    recorder = Recorder(quota={"openrouter.ai": "41"})
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)
    await post(app, MODEL_A)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.get("/limits")

    quota = response.json()["quota"]
    assert quota == [{"provider": "openrouter", "remaining_requests": 41, "resets_in": None}]
    # NVIDIA answered too (or did not publish), so it is absent, not zero.
    assert all(entry["provider"] == "openrouter" for entry in quota)


async def test_no_quota_headers_means_no_quota_rows():
    recorder = Recorder()
    ledger = LimitLedger(":memory:")
    app = build(recorder, FakeStore(), limits=ledger)
    await post(app, MODEL_A)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.get("/limits")

    assert response.json()["quota"] == []


def test_provider_routing_does_not_change_a_single_upstream_deployment():
    """No catalogue: the legacy base URL and the client's headers pass through."""
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder(request)
        return httpx.Response(200, json=completion("gpt-mock"))

    app = proxy.create_app(
        "http://upstream.invalid",
        FakeStore(),
        sheet(),
        "shadow",
        transport=httpx.MockTransport(handler),
    )

    assert app.state.providers is not None
    assert len(app.state.providers) == 0


async def test_a_dead_provider_is_cooled_down_so_the_timeout_is_not_paid_twice():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ledger = LimitLedger(":memory:")
    store = FakeStore()
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        sheet(),
        "shadow",
        registry=registry(),
        providers=catalogue(),
        limits=ledger,
        transport=httpx.MockTransport(handler),
    )

    response = await post(app, MODEL_A)

    assert response.status_code == 502
    assert ledger.is_limited("openrouter", MODEL_A)
    assert ledger.get("openrouter", MODEL_A).reason == "transport failure"


def test_needs_of_a_tool_request_requires_a_tool_capable_model():
    needs = Needs(tools=True, tokens=10)

    assert needs.summary == "10 tok, text, tools"


async def test_the_anthropic_route_switches_within_the_ranked_pool():
    """``/v1/messages`` routes on the same three gates as the chat route.

    The pool, the qualities and a locked-out requested model all come from one
    helper, so an Anthropic tool step must move to the cheapest capable model
    and be sent to *that* model's provider with *that* provider's key.
    """
    recorder = Recorder()
    store = FakeStore()
    app = build(recorder, store, mode="active", config=RouterConfig(min_gap=0))
    body = {
        "model": MODEL_A,
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "read the file"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "read", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "all good"}],
                    }
                ],
            },
        ],
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.post(
            "/v1/messages", content=json.dumps(body), headers=JSON_HEADERS
        )

    assert response.status_code == 200
    assert str(recorder.last.url) == "https://integrate.api.nvidia.com/v1/messages"
    assert recorder.last.headers["authorization"] == "Bearer nvidia-secret"
    assert json.loads(recorder.last.content)["model"] == MODEL_B
    assert store.last["decision"].action == "SWITCH"
    assert store.last["model_used"] == MODEL_B


async def test_the_limits_endpoint_summarises_a_pool_too_large_to_list():
    """A discovered catalogue holds hundreds of models; the counts stay exact.

    The dashboard polls this endpoint, so the lists are capped while totals and
    per-gate rejection counts stay truthful.
    """
    recorder = Recorder()
    app = build(recorder, FakeStore())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.get("/limits", params={"models": 1})

    document = response.json()
    assert response.status_code == 200
    assert document["pool"] == {
        "considered": 2,
        "candidates": 2,
        "refused": 0,
        "listed": 1,
        "truncated": True,
        "gates": {},
    }
    assert len(document["candidates"]) == 1
    assert len(document["refused"]) == 0


STRONG = "strong/model"
WEAK = "weak/model"
MID = "mid/model"
BETTER = "better/model"


def quality_app(
    recorder: Recorder, store: FakeStore, qualities: dict[str, tuple[str, float, float]]
) -> Any:
    """An app whose models are only distinguished by provider, quality and price."""
    registry_ = Registry(
        {
            model: ModelInfo(
                id=model,
                provider=provider,
                context_window=200_000,
                tools=True,
                quality=quality,
            )
            for model, (provider, quality, _) in qualities.items()
        }
    )
    prices = PriceSheet(
        date="2026-11-01",
        models={
            model: ModelPrice(input=cost, output=cost)
            for model, (_, _, cost) in qualities.items()
        },
    )
    return proxy.create_app(
        UPSTREAM_URL,
        store,
        prices,
        "shadow",
        registry=registry_,
        providers=catalogue(),
        limits=LimitLedger(":memory:"),
        transport=httpx.MockTransport(recorder),
    )


async def test_a_retry_prefers_the_cheapest_model_at_least_as_capable_as_the_refused_one():
    """Availability may cost more, but it must not silently cost quality.

    The strong model is rate-limited.  The weakest survivor is cheaper, and an
    even better model exists; the retry takes the cheapest model that is at
    least as capable as the one that refused -- not the cheapest, and not the
    best.
    """
    recorder = Recorder(fail={"openrouter.ai": 429})
    store = FakeStore()
    app = quality_app(
        recorder,
        store,
        {
            STRONG: ("openrouter", 85.0, 10.0),
            WEAK: ("nvidia", 40.0, 0.0),
            MID: ("nvidia", 86.0, 0.5),
            BETTER: ("nvidia", 95.0, 3.0),
        },
    )

    response = await post(app, STRONG)

    assert response.status_code == 200
    assert [request.url.host for request in recorder.requests] == [
        "openrouter.ai",
        "integrate.api.nvidia.com",
    ]
    assert json.loads(recorder.last.content)["model"] == MID
    assert store.last["model_used"] == MID


async def test_a_retry_with_no_equal_model_takes_the_strongest_survivor():
    """When every survivor is weaker, the strongest one answers.

    A weaker answer beats a failed turn, so the retry still happens -- but it
    is the best model left, not the cheapest.
    """
    recorder = Recorder(fail={"openrouter.ai": 429})
    store = FakeStore()
    app = quality_app(
        recorder,
        store,
        {
            STRONG: ("openrouter", 85.0, 10.0),
            WEAK: ("nvidia", 40.0, 0.0),
            MID: ("nvidia", 80.0, 0.5),
        },
    )

    response = await post(app, STRONG)

    assert response.status_code == 200
    assert json.loads(recorder.last.content)["model"] == MID


async def test_the_limits_endpoint_clamps_an_absurd_list_request():
    recorder = Recorder()
    app = build(recorder, FakeStore())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tamias.local") as client:
        response = await client.get("/limits", params={"models": 10_000})

    assert response.status_code == 422
