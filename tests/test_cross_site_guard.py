"""The DNS-rebinding / cross-site guard (council run 20261020-1500 item [14]).

A page on any site can resolve a hostname it controls to ``127.0.0.1`` and then
drive the loopback proxy from the victim's browser ("DNS rebinding").  Browsers
mark such requests with the attacking site's ``Origin`` (and, for the rebound
name, its ``Host``), so the gateway refuses a foreign ``Origin`` always and a
foreign ``Host`` whenever the allowlist is enforced.

The guard is exercised through ``starlette.testclient.TestClient`` rather than
the suite's ``StreamingASGITransport``: that transport is built around a
streaming ``http.response.start`` and does **not** surface a response a
middleware returns without running the downstream route, so it reports 200 for
what ``TestClient`` and a real socket correctly see as 403.  This is a property
of the test transport, not of the app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_upstream import StreamingASGITransport, create_mock_upstream  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias import proxy  # noqa: E402
from tamias.dashboard import create_dashboard_app  # noqa: E402
from tamias.pricing import load_price_sheet  # noqa: E402
from tamias.security import host_is_allowed, origin_is_allowed  # noqa: E402
from tamias.store import Store  # noqa: E402

CHAT = "/v1/chat/completions"
BODY = {"model": "local/free-model", "messages": [{"role": "user", "content": "hi"}]}
PRICES = Path(__file__).resolve().parents[1] / "prices.simulated.toml"


def _proxy_app(tmp_path: Path, *, enforce_host: bool, trusted=()):
    store = Store(str(tmp_path / "r.db"))
    sheet = load_price_sheet(PRICES)
    app = proxy.create_app(
        "http://up.test",
        store,
        sheet,
        "off",
        transport=StreamingASGITransport(create_mock_upstream()),
        enforce_host=enforce_host,
        trusted_hosts=list(trusted),
    )
    return app, store


def _client(app) -> TestClient:
    # A loopback base URL makes the default Host ``127.0.0.1`` so the honest
    # cases are not tripped by ``TestClient``'s synthetic ``testserver`` name.
    return TestClient(app, base_url="http://127.0.0.1:8000")


@pytest.mark.parametrize(
    "origin,expected",
    [
        (None, True),
        ("http://127.0.0.1:8000", True),
        ("http://localhost:8010", True),
        ("http://evil.example", False),
        ("https://evil.example", False),
        ("null", False),
    ],
)
def test_origin_allowlist(origin, expected) -> None:
    assert origin_is_allowed(origin) is expected


@pytest.mark.parametrize(
    "host,expected",
    [
        (None, True),
        ("127.0.0.1:8000", True),
        ("localhost", True),
        ("[::1]:8000", True),
        ("evil.example", False),
        ("evil.example:8000", False),
    ],
)
def test_host_allowlist(host, expected) -> None:
    assert host_is_allowed(host) is expected


def test_host_allowlist_honours_operator_named_hosts() -> None:
    assert host_is_allowed("tamias.internal", ["tamias.internal"]) is True


def test_a_foreign_origin_is_refused_even_on_loopback(tmp_path) -> None:
    app, store = _proxy_app(tmp_path, enforce_host=False)
    client = _client(app)
    foreign = client.post(CHAT, json=BODY, headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403
    assert client.post(CHAT, json=BODY).status_code == 200
    store.close()


def test_a_rebound_host_is_refused_when_the_allowlist_is_enforced(tmp_path) -> None:
    app, store = _proxy_app(tmp_path, enforce_host=True, trusted=["tamias.internal"])
    client = _client(app)
    assert client.post(CHAT, json=BODY, headers={"Host": "evil.example"}).status_code == 403
    assert client.post(CHAT, json=BODY, headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.post(CHAT, json=BODY, headers={"Host": "tamias.internal"}).status_code == 200
    store.close()


def test_the_honest_non_browser_client_is_not_affected(tmp_path) -> None:
    """No Origin, loopback Host: an SDK's request passes untouched."""
    app, store = _proxy_app(tmp_path, enforce_host=True)
    assert _client(app).post(CHAT, json=BODY).status_code == 200
    store.close()


def test_the_dashboard_refuses_a_foreign_origin(tmp_path) -> None:
    db = tmp_path / "dash.db"
    Store(str(db)).close()
    app = create_dashboard_app(str(db), str(PRICES))
    client = _client(app)
    assert client.get("/api/summary", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/summary").status_code == 200


def test_the_dashboard_refuses_a_rebound_host_when_enforced(tmp_path) -> None:
    db = tmp_path / "dash.db"
    Store(str(db)).close()
    app = create_dashboard_app(str(db), str(PRICES), enforce_host=True, trusted_hosts=["ok.local"])
    client = _client(app)
    assert client.get("/api/summary", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/summary", headers={"Host": "ok.local"}).status_code == 200
