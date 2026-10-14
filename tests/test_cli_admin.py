"""``tamias admin key`` and ``serve --keys-db``, exercised through the CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from mock_upstream import StreamingASGITransport, create_mock_upstream

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli
from tamias.keys import KeyStore

UPSTREAM_URL = "http://upstream.invalid"
MODEL = "gpt-mock"
CHAT_PATH = "/v1/chat/completions"
JSON_HEADERS = {"content-type": "application/json"}
PRICES = f'date = "2026-10-14"\n\n[{MODEL}]\ninput = 3.0\noutput = 15.0\n'


def field(stdout: str, name: str) -> str:
    prefix = f"{name}:"
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"no {name!r} line in:\n{stdout}")


def test_admin_key_issue_then_list_never_repeats_the_secret(tmp_path: Path, capsys) -> None:
    db = tmp_path / "keys.db"
    code = cli.main(
        [
            "admin",
            "key",
            "issue",
            "--keys-db",
            str(db),
            "--label",
            "alice",
            "--project",
            "acme",
            "--model",
            MODEL,
        ]
    )
    assert code == 0
    issued = capsys.readouterr().out
    token = field(issued, "token")
    key_id = field(issued, "key_id")
    assert token.startswith(key_id + "-")
    assert "acme" in issued and MODEL in issued

    assert cli.main(["admin", "key", "list", "--keys-db", str(db)]) == 0
    listing = capsys.readouterr().out
    assert key_id in listing
    assert token not in listing


def test_admin_key_revoke_is_idempotent_and_reports_an_unknown_key(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "keys.db"
    cli.main(["admin", "key", "issue", "--keys-db", str(db), "--label", "alice"])
    key_id = field(capsys.readouterr().out, "key_id")

    assert cli.main(["admin", "key", "revoke", key_id, "--keys-db", str(db)]) == 0
    assert "revoked" in capsys.readouterr().out
    assert cli.main(["admin", "key", "revoke", key_id, "--keys-db", str(db)]) == 1
    assert "no active key" in capsys.readouterr().err


def test_admin_needs_a_subcommand() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["admin"])
    assert excinfo.value.code == 2


def test_admin_key_refuses_an_invalid_project(tmp_path: Path, capsys) -> None:
    db = tmp_path / "keys.db"
    code = cli.main(
        ["admin", "key", "issue", "--keys-db", str(db), "--project", "not a project"]
    )
    assert code == 1
    assert "project must match" in capsys.readouterr().err


async def test_serve_built_app_enforces_keys_when_keys_db_is_given(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "keys.db"
    log = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    cli.main(["admin", "key", "issue", "--keys-db", str(db), "--model", MODEL])
    token = field(capsys.readouterr().out, "token")

    # The issued token really is in the store the app will read.
    store = KeyStore(db)
    assert store.authenticate(token) is not None
    store.close()

    app = cli.build_serve_app(
        UPSTREAM_URL,
        str(prices),
        str(log),
        "off",
        transport=StreamingASGITransport(create_mock_upstream()),
        keys_db=str(db),
    )
    try:
        body = json.dumps(
            {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app), base_url="http://proxy.test", timeout=None
        ) as client:
            missing = await client.post(CHAT_PATH, content=body, headers=JSON_HEADERS)
            accepted = await client.post(
                CHAT_PATH,
                content=body,
                headers={**JSON_HEADERS, "authorization": f"Bearer {token}"},
            )
        assert missing.status_code == 401
        assert accepted.status_code == 200
    finally:
        app.state.store.close()
        app.state.key_store.close()
