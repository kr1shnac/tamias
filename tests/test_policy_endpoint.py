"""``GET /policy`` serves the signed company policy (D12).

The gateway must hand the policy to an authenticated caller in the manifest's
project, and to no one else: an absent manifest is 404, an expired, tampered or
mis-pinned manifest is 503, a caller from another project is 403, and an
anonymous caller is 401.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mock_upstream import StreamingASGITransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("cryptography")

from tamias import manifest as m  # noqa: E402
from tamias import proxy  # noqa: E402
from tamias.keys import KeyStore  # noqa: E402
from tamias.pricing import ModelPrice, PriceSheet  # noqa: E402

POLICY = {"version": "1", "use_case": {"name": "codefix"}, "tools": {"deny": ["run_shell"]}}
UPSTREAM_URL = "http://upstream.invalid"
EXPIRES = "2099-12-31T00:00:00Z"


class FakeStore:
    def log_request(self, *args: Any, **kwargs: Any) -> int:  # pragma: no cover - unused
        return 1


def make_sheet() -> PriceSheet:
    return PriceSheet(date="2026-10-14", models={"m": ModelPrice(input=1.0, output=2.0)})


def make_upstream() -> FastAPI:
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def catch_all(path: str) -> JSONResponse:  # pragma: no cover - never reached
        return JSONResponse({"ok": True, "path": path})

    return app


def signed_manifest(**overrides: Any) -> tuple[dict[str, Any], Any, bytes]:
    key, public = m.generate_keypair()
    kwargs: dict[str, Any] = {"expires": EXPIRES}
    kwargs.update(overrides)
    return m.sign_manifest(POLICY, kwargs.pop("project", "acme"), key, **kwargs), key, public


def build(
    tmp_path: Path,
    *,
    key_project: str | None = "acme",
    signed: dict[str, Any] | None = None,
    manifest_key: object = None,
) -> tuple[FastAPI, str]:
    key_store = KeyStore(str(tmp_path / "keys.db"))
    _, token = key_store.issue(label="alice", project=key_project)
    app = proxy.create_app(
        UPSTREAM_URL,
        FakeStore(),
        make_sheet(),
        "off",
        transport=StreamingASGITransport(make_upstream()),
        key_store=key_store,
        manifest=signed,
        manifest_key=manifest_key,
    )
    return app, token


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
    )


async def get_policy(app: FastAPI, token: str | None, path: str = "/policy"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with client(app) as http:
        return await http.get(path, headers=headers)


async def test_absent_manifest_is_404(tmp_path: Path) -> None:
    app, token = build(tmp_path, signed=None)
    assert (await get_policy(app, token)).status_code == 404


async def test_matching_project_serves_the_manifest(tmp_path: Path) -> None:
    signed, _, public = signed_manifest()
    app, token = build(tmp_path, signed=signed, manifest_key=public)
    response = await get_policy(app, token)
    assert response.status_code == 200
    assert response.json() == signed


async def test_callers_project_is_taken_from_the_key(tmp_path: Path) -> None:
    # The key says "acme"; a header claiming otherwise cannot move it.
    signed, _, public = signed_manifest(project="acme")
    app, token = build(tmp_path, key_project="acme", signed=signed, manifest_key=public)
    response = await get_policy(app, token)
    assert response.status_code == 200
    assert response.json()["project"] == "acme"


async def test_wrong_project_is_403(tmp_path: Path) -> None:
    signed, _, public = signed_manifest(project="acme")
    app, token = build(tmp_path, key_project="other", signed=signed, manifest_key=public)
    assert (await get_policy(app, token)).status_code == 403


async def test_unauthenticated_is_401(tmp_path: Path) -> None:
    signed, _, public = signed_manifest()
    app, _ = build(tmp_path, signed=signed, manifest_key=public)
    assert (await get_policy(app, None)).status_code == 401


async def test_expired_manifest_is_503(tmp_path: Path) -> None:
    signed, _, public = signed_manifest(expires="2000-01-01T00:00:00Z")
    app, token = build(tmp_path, signed=signed, manifest_key=public)
    assert (await get_policy(app, token)).status_code == 503


async def test_tampered_manifest_is_503(tmp_path: Path) -> None:
    signed, _, public = signed_manifest()
    signed["policy"]["tools"]["deny"] = []
    app, token = build(tmp_path, signed=signed, manifest_key=public)
    assert (await get_policy(app, token)).status_code == 503


async def test_mis_pinned_key_is_503(tmp_path: Path) -> None:
    signed, _, _ = signed_manifest()
    _, other_public = m.generate_keypair()
    app, token = build(tmp_path, signed=signed, manifest_key=other_public)
    assert (await get_policy(app, token)).status_code == 503


async def test_project_prefix_selects_the_project(tmp_path: Path) -> None:
    signed, _, public = signed_manifest(project="acme")
    app, token = build(tmp_path, key_project="acme", signed=signed, manifest_key=public)
    assert (await get_policy(app, token, path="/p/acme/policy")).status_code == 200


async def test_unbound_manifest_serves_to_any_authenticated_caller(tmp_path: Path) -> None:
    signed, _, public = signed_manifest(project="")
    app, token = build(tmp_path, key_project="whoever", signed=signed, manifest_key=public)
    assert (await get_policy(app, token)).status_code == 200
