"""Hardening tests for privacy: nothing from a conversation may ever be written down.

The promise under test is the project's own: no prompt text, no response text and
no credentials are persisted, and a session is recorded as a digest rather than as
content.  These tests read the SQLite file as *bytes*, not just the columns the
code knows about, so anything smuggled into the file -- a stray column, a journal
entry, a blob -- is caught too.

``--verbose`` is checked as well, because "never stored" is not much of a promise
if the same text is printed to the terminal: whatever the user can see, someone
reading the log can keep.  Nothing here fixes ``src/``; a test that exposes a real
defect is marked ``xfail(strict=True)`` and carries a BUG id that also appears in
``docs/audit/hardening-findings.md``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mock_upstream import StreamingASGITransport

from tamias import cli, proxy, store
from tamias.pricing import load_price_sheet
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.store import Store

# Markers that must never turn up in the database file or in a log line.  Each is
# unique enough that a match means the text was written down, not a coincidence.
PROMPT_SECRET = "s3cr3t-user-prompt-7f2a91"
SYSTEM_SECRET = "s3cr3t-system-prompt-4c18b0"
REPLY_SECRET = "s3cr3t-assistant-reply-9de3f7"
API_KEY = "sk-live-abcdef0123456789-do-not-store"
ALL_SECRETS = (PROMPT_SECRET, SYSTEM_SECRET, REPLY_SECRET, API_KEY)

CHAT_PATH = proxy.CHAT_PATH
STRONG = "gpt-strong"
CHEAP = "gpt-cheap"
PROXY_URL = "http://proxy.test"

PRICES = f"""
date = "2026-10-04"

[{STRONG}]
input = 3.0
output = 15.0
cached_input = 0.3
cache_write = 3.75

[{CHEAP}]
input = 0.25
output = 1.25
cached_input = 0.025
cache_write = 0.3
"""

CONFIG = RouterConfig(easy_tools=EASY_TOOLS, cheap_model=CHEAP, strong_model=STRONG)

USAGE = {
    "prompt_tokens": 41,
    "completion_tokens": 13,
    "total_tokens": 54,
    "prompt_tokens_details": {"cached_tokens": 5},
}


# --- fixtures and helpers ----------------------------------------------------


@pytest.fixture
def prices_path(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


# An upstream that answers with text nobody is allowed to keep: both its own reply
# and the prompt it was sent come back inside the assistant message, so any code
# path that stores "what was said" is caught by the same search.
upstream = FastAPI()


@upstream.post(CHAT_PATH)
async def private_chat(request: Request) -> Response:
    await request.body()
    return JSONResponse(
        {
            "id": "chatcmpl-private",
            "object": "chat.completion",
            "model": STRONG,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"{REPLY_SECRET} {PROMPT_SECRET}"},
                }
            ],
            "usage": USAGE,
        }
    )


def secret_body(text: str = PROMPT_SECRET) -> bytes:
    return json.dumps(
        {
            "model": STRONG,
            "messages": [
                {"role": "system", "content": SYSTEM_SECRET},
                {"role": "user", "content": text},
            ],
            "stream": False,
        }
    ).encode()


def build_proxy(db_path: Path, prices_path: Path) -> tuple[FastAPI, Store]:
    store_ = Store(db_path)
    app = proxy.create_app(
        "http://upstream.invalid",
        store_,
        load_price_sheet(prices_path),
        "shadow",
        config=CONFIG,
        transport=StreamingASGITransport(upstream),
    )
    return app, store_


def upstream_answering(response_id: Any) -> FastAPI:
    """A mock upstream whose response carries ``response_id`` in its ``id`` field.

    The response id is the one part of a response body that reaches the log, so a
    test has to be able to choose it: a real id, an id with a space in it, or one
    past the length cap.
    """
    app = FastAPI()

    @app.post(CHAT_PATH)
    async def chat(request: Request) -> Response:
        await request.body()
        return JSONResponse(
            {
                "id": response_id,
                "object": "chat.completion",
                "model": STRONG,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"{REPLY_SECRET} {PROMPT_SECRET}",
                        },
                    }
                ],
                "usage": USAGE,
            }
        )

    return app


def build_proxy_answering(
    db_path: Path, prices_path: Path, response_id: Any
) -> tuple[FastAPI, Store]:
    """The proxy in front of an upstream that answers with ``response_id``."""
    request_log = Store(db_path)
    app = proxy.create_app(
        "http://upstream.invalid",
        request_log,
        load_price_sheet(prices_path),
        "shadow",
        config=CONFIG,
        transport=StreamingASGITransport(upstream_answering(response_id)),
    )
    return app, request_log


@asynccontextmanager
async def proxy_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url=PROXY_URL, timeout=None
    ) as client:
        yield client


def send(secret: str = API_KEY) -> dict[str, str]:
    return {"content-type": "application/json", "authorization": f"Bearer {secret}"}


async def send_secret_request(
    app: FastAPI, *, key: str = API_KEY, text: str = PROMPT_SECRET
) -> dict[str, Any]:
    """One request whose every text field is a secret, answered by the mock upstream."""
    async with proxy_client(app) as client:
        response = await client.post(CHAT_PATH, content=secret_body(text), headers=send(key))
    assert response.status_code == 200, response.text
    return response.json()


def db_bytes(db_path: Path) -> bytes:
    return db_path.read_bytes()


def row_text(row: Any) -> str:
    """Every stored value, flattened, so a secret anywhere in the row shows up."""
    return " | ".join("" if value is None else str(value) for value in tuple(row))


# --- the schema cannot hold text ---------------------------------------------


# The pinned schema, in order.  The three audit columns Steps 1-2 added are
# appended at the end rather than slotted into the middle, because that is the
# only place ALTER TABLE can put them: a fresh table and a migrated one must
# declare the same order.
EXPECTED_COLUMNS = (
    "ts",
    "session_id",
    "model_requested",
    "model_used",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "cost_usd",
    "price_sheet_date",
    "latency_ms",
    "status",
    "decision_action",
    "decision_target_model",
    "decision_reason",
    "price_sheet",
    "price_simulated",
    "provider_cost_usd",
    "generation_id",
)

# Every column whose declared SQL type can hold text, and the single thing allowed
# to write it.  This is an allow-list, so a text column that is not justified here
# fails the test rather than being added quietly.  Nothing in it may be written
# from a request body, a response body or a header.
TEXT_COLUMN_ALLOW_LIST = {
    "ts": "the clock: datetime.now(UTC), formatted by the proxy",
    "session_id": (
        "a salted digest of the credential and the first messages, or the "
        "client's explicit x-tamias-session id; never the digest's inputs"
    ),
    "model_requested": "the request body's `model` field, or the literal 'unknown'",
    "model_used": "the response's `model` field, or the requested one when absent",
    "price_sheet_date": "the `date` key of the price sheet, i.e. configuration",
    "status": "the upstream status code rendered as a string",
    "decision_action": "the router's own STAY or SWITCH",
    "decision_target_model": "the configured cheap model",
    "decision_reason": (
        "a literal the router builds, or a tool name that already matched "
        "RouterConfig.easy_tools, i.e. configuration"
    ),
    # price_sheet is the one column Steps 1-2 added that holds caller-supplied
    # text, and the only reason the allow-list above needed one: it is the id of
    # the price sheet the number came from.  Its value is written from
    # configuration alone -- proxy.record() passes `sheet.source`, the --prices
    # path the operator started the proxy with -- and never from a request or a
    # response.  test_request_text_never_lands_in_any_column is what holds it to
    # that: a prompt lands in no column at all, this one included.
    "price_sheet": "configuration: the --prices sheet path (store.Store log_request keyword)",
    # generation_id is the second column a value from outside reaches, and the
    # only one fed by a *response* body rather than by configuration: it is the
    # provider's own `id` field, kept so scripts/reconcile_live.py can look the
    # generation up.  It is allowed on shape, not on trust --
    # store.sanitize_generation_id keeps a bare id of at most 128 characters
    # matching [A-Za-z0-9_.:-]+ and stores NULL for anything else, an id with a
    # space in it included.  The tests below hold it to that: a valid id is
    # stored verbatim, an id with a space or over the length cap becomes NULL,
    # and "SECRET-PROMPT" from the request lands in no column at all.  Note the
    # limit of the guarantee: `-` is legal in a real id, so shape excludes
    # prose-with-spaces, it does not exclude every string that reads like text.
    "generation_id": (
        "the response's own `id`, only via store.sanitize_generation_id "
        "(bare identifier, <= 128 chars) or NULL"
    ),
}

TEXT_TYPES = ("CHAR", "CLOB", "TEXT")


def declared_text_columns() -> set[str]:
    """The columns of a fresh log whose declared type could hold arbitrary text."""
    opened = Store(":memory:")
    try:
        return {
            str(info[1])
            for info in opened._conn.execute("PRAGMA table_info(requests)")
            if any(kind in str(info[2]).upper() for kind in TEXT_TYPES)
        }
    finally:
        opened.close()


def test_request_log_schema_has_no_column_that_could_hold_text() -> None:
    """Pin the schema: if a column ever appears to hold prose, this test fails.

    Cheap to check and impossible to satisfy by accident, so it is the first line
    of defence against "we only started storing the prompt last week".

    Two pins, because the schema is now two promises.  The column list and its
    order are pinned outright, and every column whose declared type can hold text
    is on the allow-list above with the one thing allowed to write it -- so a new
    text column has to be justified rather than slipped in.
    """
    assert store.COLUMNS == EXPECTED_COLUMNS

    text_capable = declared_text_columns()
    unjustified = sorted(text_capable - set(TEXT_COLUMN_ALLOW_LIST))
    assert not unjustified, (
        f"{', '.join(unjustified)} can hold text and nothing is allowed to write them"
    )
    misdescribed = sorted(set(TEXT_COLUMN_ALLOW_LIST) - text_capable)
    assert not misdescribed, (
        f"{', '.join(misdescribed)} are allow-listed as text but no longer hold it"
    )


# --- prompts, replies and keys stay out of the database ----------------------


async def test_prompt_and_response_text_never_reach_the_database_file(
    db_path: Path, prices_path: Path
) -> None:
    """The whole file is searched, not just the known columns.

    Reading the bytes catches a secret hidden anywhere in the file: an extra
    table, a FTS side index, a WAL frame left behind.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)
    request_log.close()

    blob = db_bytes(db_path)
    assert blob, "the request log was never written, so nothing was proven"
    for secret in ALL_SECRETS:
        assert secret.encode() not in blob, f"{secret!r} was written to {db_path}"


async def test_stored_row_carries_counts_instead_of_text(db_path: Path, prices_path: Path) -> None:
    """The row must be worth having: counts and decision, no prose."""
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)

    rows = request_log.rows()
    request_log.close()
    assert len(rows) == 1
    row = rows[0]
    for secret in ALL_SECRETS:
        assert secret not in row_text(row), f"{secret!r} reached the stored row"

    assert row["input_tokens"] == USAGE["prompt_tokens"]
    assert row["output_tokens"] == USAGE["completion_tokens"]
    assert row["cached_input_tokens"] == USAGE["prompt_tokens_details"]["cached_tokens"]
    assert row["decision_action"] == "STAY"


async def test_request_text_never_lands_in_any_column(db_path: Path, prices_path: Path) -> None:
    """A prompt lands in no column at all, not even the ones allowed to hold text.

    ``price_sheet`` is on the schema allow-list because it holds a sheet id, so
    this is the test that earns it that place: its value comes from
    configuration, and every column of the stored row is checked by name rather
    than taken on trust.  The trailing assertions keep it honest -- the row has to
    exist and be worth having, so the check cannot pass because nothing was
    written at all.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app, text="SECRET-PROMPT")
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1, "the request was not logged, so nothing was proven"
    row = rows[0]
    for name, value in zip(row.keys(), row, strict=True):
        assert "SECRET-PROMPT" not in str(value), f"SECRET-PROMPT reached column {name!r}"
    assert b"SECRET-PROMPT" not in db_bytes(db_path), "the prompt is in the file somewhere"

    # The row is metadata, not a gap: counts are there...
    assert row["input_tokens"] == USAGE["prompt_tokens"]
    assert row["output_tokens"] == USAGE["completion_tokens"]
    assert row["decision_action"] == "STAY"
    # ...and the one text column allowed here holds the configured sheet, not the
    # conversation.
    assert row["price_sheet"] == str(prices_path)
    assert row["price_simulated"] == 0


# --- generation_id is an id or it is nothing ---------------------------------

# The characters OpenRouter's own documented examples use in a generation id, so
# the "valid" case is a real shape rather than one invented to fit the regex.
BARE_ID = "gen-2026_10.04:abcDEF123"
AT_THE_CAP = "g" * store.GENERATION_ID_MAX_CHARS
PAST_THE_CAP = "g" * (store.GENERATION_ID_MAX_CHARS + 1)


async def test_a_bare_provider_id_is_stored_as_the_generation_id(
    db_path: Path, prices_path: Path
) -> None:
    """A well-formed id is kept verbatim: it is what the reconciliation lookup needs.

    The rest of the row is asserted too, so this cannot pass because the request
    failed to be logged at all.
    """
    app, request_log = build_proxy_answering(db_path, prices_path, BARE_ID)
    await send_secret_request(app)
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1, "the request was not logged, so nothing was proven"
    row = rows[0]
    assert row["generation_id"] == BARE_ID
    assert row["input_tokens"] == USAGE["prompt_tokens"]
    assert row["decision_action"] == "STAY"


async def test_an_id_at_the_length_cap_is_still_stored(db_path: Path, prices_path: Path) -> None:
    """The cap is inclusive, so the longest admissible id is not thrown away."""
    app, request_log = build_proxy_answering(db_path, prices_path, AT_THE_CAP)
    await send_secret_request(app)
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1
    assert rows[0]["generation_id"] == AT_THE_CAP
    assert len(rows[0]["generation_id"]) == store.GENERATION_ID_MAX_CHARS


@pytest.mark.parametrize(
    "response_id",
    [
        "gen with spaces",
        "gen\tid",
        PAST_THE_CAP,
        "",
        12345,
        {"id": BARE_ID},
    ],
    ids=["spaces", "tab", "past-the-cap", "empty", "number", "object"],
)
async def test_anything_that_is_not_a_bare_id_is_stored_as_null(
    db_path: Path, prices_path: Path, response_id: Any
) -> None:
    """Whitespace, over-length and non-string ids all become NULL.

    UNKNOWN rather than a guess: a dropped id is a row that reconciliation skips,
    which is honest, whereas a mangled one would be looked up in the wrong place.
    The guarantee is the id's shape, so prose is excluded by its whitespace while
    a hyphen is kept -- real ids such as ``chatcmpl-...`` contain one.
    """
    app, request_log = build_proxy_answering(db_path, prices_path, response_id)
    await send_secret_request(app)
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1, "the request was not logged, so nothing was proven"
    assert rows[0]["generation_id"] is None


async def test_a_request_containing_secret_prompt_lands_in_no_column(
    db_path: Path, prices_path: Path
) -> None:
    """SECRET-PROMPT stays out of every column, generation_id included.

    ``generation_id`` is the one column fed by a response body, so it is named
    explicitly here alongside the by-name sweep of the row: what lands there is
    the provider's bare id, never anything the client sent.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app, text="SECRET-PROMPT")
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1, "the request was not logged, so nothing was proven"
    row = rows[0]
    for name, value in zip(row.keys(), row, strict=True):
        assert "SECRET-PROMPT" not in str(value), f"SECRET-PROMPT reached column {name!r}"
    assert row["generation_id"] == "chatcmpl-private"
    assert b"SECRET-PROMPT" not in db_bytes(db_path), "the prompt is in the file somewhere"
    # The row is still a real row, so the check cannot pass by storing nothing.
    assert row["input_tokens"] == USAGE["prompt_tokens"]
    assert row["output_tokens"] == USAGE["completion_tokens"]


async def test_authorization_header_is_never_stored(db_path: Path, prices_path: Path) -> None:
    """A bearer token is forwarded upstream but must not survive in the log.

    The proxy logs the upstream's token counts and cost; keeping the credential
    that pays for them would turn a cost report into a credential store.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1
    assert API_KEY.encode() not in db_bytes(db_path)
    assert "Bearer" not in row_text(rows[0])


# --- sessions are digests, not conversations ---------------------------------


async def test_session_id_is_a_digest_rather_than_conversation_text(
    db_path: Path, prices_path: Path
) -> None:
    """With no explicit session header the id must be a fixed-shape digest.

    Twelve hex characters: long enough not to collide by accident, short enough
    to read in a report, and impossible to read a conversation back out of.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)
    rows = request_log.rows()
    request_log.close()

    assert len(rows) == 1
    session = rows[0]["session_id"]
    assert session.startswith(proxy.SESSION_ID_PREFIX), session
    digest = session.removeprefix(proxy.SESSION_ID_PREFIX)
    assert len(digest) == proxy.SESSION_ID_HEX_CHARS, session
    assert all(character in "0123456789abcdef" for character in digest), session
    for secret in (PROMPT_SECRET, SYSTEM_SECRET):
        assert secret not in session


async def test_same_conversation_keeps_one_session_and_a_new_one_forks_it(
    db_path: Path, prices_path: Path
) -> None:
    """Two steps of one conversation agree; a different question does not.

    Both halves matter: a session id that changed every turn would make the report
    useless, and one that ignored the conversation would merge unrelated work.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)
    await send_secret_request(app)
    await send_secret_request(app, text="a completely different question")
    rows = request_log.rows()
    request_log.close()

    first, second, other = (row["session_id"] for row in rows)
    assert first == second, "one conversation must land on one session"
    assert first != other, "a different conversation must not share the session"
    assert first.startswith(proxy.SESSION_ID_PREFIX)


async def test_a_different_api_key_is_a_different_session(db_path: Path, prices_path: Path) -> None:
    """The digest covers the credential, so one client's rows cannot be joined to another's.

    Identical prompts from two credentials stay apart, which is what keeps the
    session column from being a conversation fingerprint shared across accounts.
    """
    app, request_log = build_proxy(db_path, prices_path)
    await send_secret_request(app)
    await send_secret_request(app, key="sk-live-second-key-000000")
    rows = request_log.rows()
    request_log.close()

    first, second = (row["session_id"] for row in rows)
    assert first != second


# --- nothing is printed either -----------------------------------------------


async def test_verbose_logging_never_prints_conversation_text(
    db_path: Path, prices_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``--verbose`` may narrate decisions, never quote the conversation.

    The assertion that a routing decision *was* logged keeps this honest: a test
    that captures no logs would pass for the wrong reason.
    """
    app, request_log = build_proxy(db_path, prices_path)
    with caplog.at_level(logging.DEBUG, logger="tamias"):
        await send_secret_request(app)
    request_log.close()

    tamias_records = [record for record in caplog.records if record.name.startswith("tamias")]
    assert tamias_records, "no tamias log records were captured, so nothing was proven"
    assert any("decision=" in record.getMessage() for record in tamias_records)

    transcript = "\n".join(record.getMessage() for record in tamias_records)
    for secret in ALL_SECRETS:
        assert secret not in transcript, f"{secret!r} was logged"


# --- the server is not reachable from off the machine by default -------------


def test_serve_binds_loopback_by_default() -> None:
    """A chat proxy holds bearer tokens; binding 0.0.0.0 would expose it.

    Checked on the parser default rather than by reading the source, so that
    changing the flag's default is what makes this test fail.
    """
    args = cli.build_parser().parse_args(
        ["serve", "--upstream", "http://x", "--prices", "p.toml", "--db", "d.db"]
    )
    assert ipaddress.ip_address(args.host).is_loopback, f"serve binds {args.host} by default"
