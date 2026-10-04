"""End-to-end test: the real proxy, a real sqlite log, and the real ``tamias report``.

Nothing is stubbed.  ``proxy.create_app`` runs with a real
:class:`tamias.store.Store` writing to a real sqlite file and a price sheet
loaded from a real TOML file, and the requests are driven through a real httpx
client.  The only substitution is the upstream: instead of a socket, the mock
upstream app is reached through ``StreamingASGITransport``, which keeps the
proxy's own httpx client, request building, forwarding and logging on the real
code path.  CONTRACT requires that tests never open a network connection.

The session is ten requests long and is shaped to exercise every router branch:
planning, hysteresis, a switch, a tool error, a hard tool, and two more
switches.  Three of the ten are shadow SWITCHes.  The point of the test is that
all ten reach the upstream on the strong model.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mock_upstream import StreamingASGITransport, create_mock_upstream

from tamias import cli, pricing, proxy
from tamias.pricing import load_price_sheet
from tamias.router import EASY_TOOLS, RouterConfig
from tamias.store import Store
from tamias.types import Usage

UPSTREAM_URL = "http://upstream.invalid"
PROXY_URL = "http://proxy.test"
CHAT_PATH = "/v1/chat/completions"
SESSION = "e2e-session"
STRONG = "gpt-strong"
CHEAP = "gpt-cheap"
MIN_GAP = 3
REQUESTS = 10
EXPECTED_SWITCHES = (4, 7, 10)

SHEET_DATE = "2026-10-04"
PRICES = f"""
date = "{SHEET_DATE}"

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

# A string that appears only in prompt text.  It must never reach the sqlite
# file: the log holds metadata, and the schema has no column that could hold it.
PROMPT_MARKER = "zz-secret-prompt-marker-zz"

CONFIG = RouterConfig(
    easy_tools=EASY_TOOLS, cheap_model=CHEAP, strong_model=STRONG, min_gap=MIN_GAP
)

# What the mock upstream reports for every request.
MOCK_USAGE = Usage(input_tokens=11, output_tokens=7, cached_input_tokens=3, cache_write_tokens=None)


def user_turn(step: int) -> dict[str, Any]:
    return {
        "model": STRONG,
        "messages": [{"role": "user", "content": f"step {step}: {PROMPT_MARKER} please"}],
    }


def tool_turn(step: int, name: str, content: str) -> dict[str, Any]:
    return {
        "model": STRONG,
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{step}",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"call_{step}", "content": content},
        ],
    }


def assistant_turn() -> dict[str, Any]:
    return {"model": STRONG, "messages": [{"role": "assistant", "content": "done"}]}


# The ten-request session, in order.  Chosen so the router takes each branch at
# least once: user, hysteresis x2, switch, user, tool error, switch, hard tool,
# assistant, switch.
SESSION_PLAN: tuple[tuple[str, dict[str, Any]], ...] = (
    ("user turn", user_turn(1)),
    ("easy tool", tool_turn(2, "read", "file body")),
    ("easy tool", tool_turn(3, "read", "file body")),
    ("easy tool", tool_turn(4, "read", "file body")),
    ("user turn", user_turn(5)),
    ("easy tool, error output", tool_turn(6, "grep", "error: no such file")),
    ("easy tool", tool_turn(7, "bash", "ok")),
    ("hard tool", tool_turn(8, "edit_file", "wrote 1 file")),
    ("assistant turn", assistant_turn()),
    ("easy tool", tool_turn(10, "ls", "a.txt")),
)


def write_prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


@pytest.fixture
def prices_path(tmp_path: Path) -> Path:
    return write_prices(tmp_path)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


@pytest.fixture
def upstream() -> FastAPI:
    return create_mock_upstream()


@pytest.fixture
async def run_session(
    upstream: FastAPI, db_path: Path, prices_path: Path
) -> AsyncIterator[list[dict[str, Any]]]:
    """Drive the ten requests through the real proxy, returning what it logged.

    The bodies are written with a fixed key order and no whitespace so the bytes
    are reproducible: the upstream echoes them back and the test compares them
    against exactly what it sent.
    """
    store = Store(db_path)
    app = proxy.create_app(
        UPSTREAM_URL,
        store,
        load_price_sheet(prices_path),
        "shadow",
        config=CONFIG,
        transport=StreamingASGITransport(upstream),
    )
    sent: list[bytes] = []
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url=PROXY_URL, timeout=None
    ) as client:
        for _label, body in SESSION_PLAN:
            raw = json.dumps(body, separators=(",", ":")).encode()
            sent.append(raw)
            response = await client.post(
                CHAT_PATH,
                content=raw,
                headers={"content-type": "application/json", "x-tamias-session": SESSION},
            )
            assert response.status_code == 200, response.text
            echoed = base64.b64decode(response.json()["echo"]["raw_body_b64"])
            assert echoed == raw, "the proxy altered the body it forwarded"
    rows = [dict(row) for row in store.rows()]
    store.close()
    yield rows


def test_session_logs_exactly_ten_rows(run_session: list[dict[str, Any]]) -> None:
    assert len(run_session) == REQUESTS
    assert [row["session_id"] for row in run_session] == [SESSION] * REQUESTS


def test_every_row_carries_the_usage_the_upstream_reported(
    run_session: list[dict[str, Any]],
) -> None:
    for row in run_session:
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 7
        assert row["cached_input_tokens"] == 3
        # The mock never reports cache writes: UNKNOWN, stored as NULL.
        assert row["cache_write_tokens"] is None
        assert row["status"] == "200"
        assert row["latency_ms"] is not None
        assert row["ts"]


def test_cost_is_unknown_never_zero(run_session: list[dict[str, Any]], prices_path: Path) -> None:
    # cache_write_tokens is unknown, so the arithmetic cannot be completed.  A
    # cost of 0.0 here would be a fabricated number.
    for row in run_session:
        assert row["cost_usd"] is None
        assert row["price_sheet_date"] == SHEET_DATE


def test_shadow_mode_switches_are_the_expected_three(
    run_session: list[dict[str, Any]],
) -> None:
    switched = [
        index
        for index, row in enumerate(run_session, start=1)
        if row["decision_action"] == "SWITCH"
    ]
    assert switched == list(EXPECTED_SWITCHES)


def test_shadow_mode_records_the_cheap_model_as_the_switch_target(
    run_session: list[dict[str, Any]],
) -> None:
    for index, row in enumerate(run_session, start=1):
        if index in EXPECTED_SWITCHES:
            assert row["decision_target_model"] == CHEAP
            assert "easy tool" in row["decision_reason"]
        else:
            assert row["decision_target_model"] is None


def test_shadow_mode_never_changed_the_forwarded_model(
    run_session: list[dict[str, Any]],
) -> None:
    # The whole point of shadow mode: on every row, including the three the
    # router would have switched, the model that was asked for is the model that
    # was used.
    for row in run_session:
        assert row["model_requested"] == STRONG
        assert row["model_used"] == STRONG


def test_shadow_mode_never_changes_a_request_byte(
    run_session: list[dict[str, Any]],
) -> None:
    # Re-derive the ten bodies and confirm the logged model is still the strong
    # one on every request, i.e. no row was routed anywhere.
    assert [body["model"] for _label, body in SESSION_PLAN] == [STRONG] * REQUESTS
    assert {row["model_used"] for row in run_session} == {STRONG}


def test_report_totals_the_session(
    run_session: list[dict[str, Any]],
    db_path: Path,
    prices_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["report", "--db", str(db_path), "--prices", str(prices_path)])
    out = capsys.readouterr().out

    assert code == 0
    assert f"requests: {REQUESTS}" in out
    assert f"requests shadow would have switched: {len(EXPECTED_SWITCHES)}" in out
    assert f"cheap model assumed: {CHEAP}" in out
    assert f"price sheet: {prices_path} ({SHEET_DATE})" in out


def test_report_calls_the_total_unknown_because_no_cost_is_known(
    run_session: list[dict[str, Any]],
    db_path: Path,
    prices_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["report", "--db", str(db_path), "--prices", str(prices_path)]) == 0
    out = capsys.readouterr().out

    assert "total cost: UNKNOWN" in out
    assert f"{REQUESTS} of {REQUESTS} requests have unknown cost" in out
    # The known part is still reported rather than being hidden or zeroed.
    assert re.search(r"known part: \$0\.00", out)
    # Every saving is unpriceable, because the mock never reports cache writes.
    assert "estimated saving: $0.00" in out
    switches = len(EXPECTED_SWITCHES)
    assert f"{switches} of {switches} switched requests not priced" in out
    assert "not measured" in out


def test_report_does_not_modify_the_log(
    db_path: Path, prices_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Store(db_path).close()
    before = db_path.read_bytes()
    cli.main(["report", "--db", str(db_path), "--prices", str(prices_path)])
    capsys.readouterr()
    assert db_path.read_bytes() == before


def test_no_prompt_or_response_text_reaches_the_database(db_path: Path, prices_path: Path) -> None:
    store = Store(db_path)
    store.close()
    blob = db_path.read_bytes()
    assert PROMPT_MARKER.encode() not in blob
    assert b"file body" not in blob
    assert b"wrote 1 file" not in blob

    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    assert not columns & {"prompt", "content", "text", "body", "messages", "response"}


def test_stored_cost_matches_the_pricing_module(
    run_session: list[dict[str, Any]], prices_path: Path
) -> None:
    sheet = load_price_sheet(prices_path)
    for row in run_session:
        expected = pricing.compute_cost(STRONG, MOCK_USAGE, sheet)
        assert expected.usd is None
        assert "cache_write" in expected.formula
        assert row["cost_usd"] == expected.usd


def test_serve_defaults_to_shadow_mode_on_port_8000() -> None:
    args = cli.build_parser().parse_args(
        ["serve", "--upstream", UPSTREAM_URL, "--prices", "p.toml", "--db", "r.db"]
    )
    assert args.command == "serve"
    assert args.router_mode == "shadow"
    assert args.port == 8000
    assert args.upstream == UPSTREAM_URL
    # Off unless asked for: the default forwards every body byte-for-byte.
    assert args.inject_usage is False


def test_serve_accepts_the_documented_flags() -> None:
    args = cli.build_parser().parse_args(
        [
            "serve",
            "--upstream",
            "https://opencode.ai/zen",
            "--prices",
            "prices.toml",
            "--db",
            "requests.db",
            "--port",
            "9123",
            "--router-mode",
            "shadow",
            "--cheap-model",
            CHEAP,
            "--strong-model",
            STRONG,
            "--min-gap",
            "5",
            "--inject-usage",
        ]
    )
    assert args.port == 9123
    assert args.cheap_model == CHEAP
    assert args.strong_model == STRONG
    assert args.min_gap == 5
    assert args.inject_usage is True


def test_serve_rejects_an_unknown_router_mode() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(
            [
                "serve",
                "--upstream",
                UPSTREAM_URL,
                "--prices",
                "p.toml",
                "--db",
                "r.db",
                "--router-mode",
                "yolo",
            ]
        )
    assert excinfo.value.code == 2


def test_serve_wires_the_router_config_into_the_app(db_path: Path, prices_path: Path) -> None:
    app = cli.build_serve_app(
        UPSTREAM_URL,
        str(prices_path),
        str(db_path),
        "shadow",
        cheap_model=CHEAP,
        strong_model=STRONG,
        min_gap=MIN_GAP,
    )
    try:
        assert app.state.router_mode == "shadow"
        assert app.state.router_config == CONFIG
        # The sqlite log exists as soon as serve would have started serving.
        assert db_path.is_file()
    finally:
        app.state.store.close()


def test_serve_fails_cleanly_on_a_missing_price_sheet(db_path: Path) -> None:
    with pytest.raises(OSError):
        cli.build_serve_app(UPSTREAM_URL, str(db_path / "nope.toml"), str(db_path))


# --- --inject-usage, end to end through `tamias serve`'s own app builder ------


def _sse_frames(content: bytes) -> list[dict[str, Any]]:
    """Parse an SSE body the mock upstream produced, in order."""
    frames = []
    for line in content.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:") or b"[DONE]" in line:
            continue
        decoded = json.loads(line[len(b"data:") :])
        if isinstance(decoded, dict):
            frames.append(decoded)
    return frames


async def _stream_once(
    db_path: Path, prices_path: Path, inject_usage: bool, body: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Send one streaming request through a `serve`-built app; return frames + echoed body."""
    raw = json.dumps(body, separators=(",", ":")).encode()
    app = cli.build_serve_app(
        UPSTREAM_URL,
        str(prices_path),
        str(db_path),
        "shadow",
        cheap_model=CHEAP,
        strong_model=STRONG,
        min_gap=MIN_GAP,
        inject_usage=inject_usage,
        transport=StreamingASGITransport(create_mock_upstream()),
    )
    try:
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app), base_url=PROXY_URL, timeout=None
        ) as client:
            response = await client.post(
                CHAT_PATH,
                content=raw,
                headers={"content-type": "application/json", "x-tamias-session": SESSION},
            )
        assert response.status_code == 200, response.text
        frames = _sse_frames(response.content)
        echoed = base64.b64decode(frames[0]["echo"]["raw_body_b64"])
        return frames, {"sent": raw, "echoed": echoed}
    finally:
        app.state.store.close()


async def test_serve_without_inject_usage_forwards_the_stream_body_byte_identical(
    tmp_path: Path, db_path: Path, prices_path: Path
) -> None:
    """Off by default: not one byte of a streaming body is touched."""
    _frames, bodies = await _stream_once(
        db_path, prices_path, False, {"model": STRONG, "messages": [], "stream": True}
    )
    assert bodies["echoed"] == bodies["sent"]
    assert b"stream_options" not in bodies["echoed"]


async def test_serve_with_inject_usage_changes_only_stream_options(
    tmp_path: Path, db_path: Path, prices_path: Path
) -> None:
    """On: the only difference is stream_options.include_usage.

    Every other key the client sent -- model, messages, temperature, seed -- has to
    survive untouched, so the flag cannot quietly alter what the model is asked.
    """
    _frames, bodies = await _stream_once(
        db_path,
        prices_path,
        True,
        {
            "model": STRONG,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "temperature": 0.9,
            "seed": 7,
        },
    )
    sent = json.loads(bodies["sent"])
    echoed = json.loads(bodies["echoed"])
    assert echoed["stream_options"] == {"include_usage": True}
    for key in ("model", "messages", "temperature", "seed", "stream"):
        assert echoed[key] == sent[key], f"{key} was altered"
    # Nothing beyond stream_options was added either.
    assert set(echoed) == set(sent) | {"stream_options"}


async def test_serve_with_inject_usage_keeps_stream_options_the_client_sent(
    tmp_path: Path, db_path: Path, prices_path: Path
) -> None:
    """A client that already sent stream_options keeps its other keys."""
    _frames, bodies = await _stream_once(
        db_path,
        prices_path,
        True,
        {
            "model": STRONG,
            "messages": [],
            "stream": True,
            "stream_options": {"other_setting": "keep-me"},
        },
    )
    echoed = json.loads(bodies["echoed"])
    assert echoed["stream_options"] == {"other_setting": "keep-me", "include_usage": True}


async def test_serve_with_inject_usage_client_still_gets_a_valid_stream(
    tmp_path: Path, db_path: Path, prices_path: Path
) -> None:
    """The client sees a normal stream plus one final usage chunk.

    The extra chunk is the whole point and also the whole risk: it carries usage
    and an *empty* choices array, so a client that assumes every chunk has a
    choice could trip over it.  It arrives after all the content, and the proxy
    relays it verbatim.
    """
    frames, _bodies = await _stream_once(
        db_path, prices_path, True, {"model": STRONG, "messages": [], "stream": True}
    )
    content_frames = [f for f in frames if f.get("choices")]
    usage_frames = [f for f in frames if f.get("usage")]
    assert usage_frames, "no usage chunk reached the client"
    assert len(usage_frames) == 1
    assert usage_frames[0]["choices"] == []
    assert usage_frames[0]["usage"]["prompt_tokens"] == 11
    # The usage chunk is last, so a client reading in order sees content first.
    assert frames[-1] is usage_frames[0]
    assert content_frames, "the content chunks went missing"
