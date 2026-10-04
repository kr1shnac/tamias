#!/usr/bin/env python3
"""Live check of a running tamias proxy: three real requests, then the sqlite log.

Three probes go through the proxy at ``/v1/chat/completions``, each carrying the
same one-line prompt, so the three rows they leave behind differ only in how
usage was requested:

1. buffered (non-streaming) — the upstream reports usage in the JSON body;
2. streaming with no ``stream_options`` — the upstream sends no usage chunk;
3. streaming with ``stream_options.include_usage = true`` — the upstream sends
   a trailing usage chunk.

Probe 2 is the interesting one: its row *must* have NULL token counts, because
tamias forwards the body byte-for-byte and a stream that never asked for usage
cannot have any.  A non-NULL count there would mean the proxy had injected
``stream_options`` the agent never asked for.

The API key is read from an environment variable and is never printed: every
line of output passes through a redactor first.  The variable is ``ZEN_API_KEY``
by default; ``--api-key-env NAME`` reads a different one, which is how the same
script is pointed at OpenRouter (``--api-key-env OPENROUTER_API_KEY``) without
editing it.  Only the variable's *name* is ever printed.

With ``--mock`` no socket is opened and no key is needed.  The mock upstream the
test suite uses (``tests/mock_upstream.py``) and the real proxy app are wired
together in-process, and the requests go through the proxy exactly as they would
over a socket, so the script can be self-tested.

Run ``python scripts/live_check.py --mock`` first; the live run is
``python scripts/live_check.py`` against a proxy already listening.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

CHAT_PATH = "/v1/chat/completions"
SESSION_HEADER = "x-tamias-session"
KEY_ENV = "ZEN_API_KEY"
PROMPT = "Reply with the word ok"
SESSION_ID = "live-check"
MOCK_CHEAP_MODEL = "live-check-cheap"
MOCK_UPSTREAM_URL = "http://mock-upstream.invalid"
MOCK_SHEET_DATE = "2026-10-04"

DEFAULT_PROXY = "http://localhost:8000"
DEFAULT_MODEL = "big-pickle"
DEFAULT_DB = "live_check.db"
REPO_ROOT = Path(__file__).resolve().parent.parent

UNKNOWN = "?"

BUFFERED = "buffered"
NO_OPTIONS = "stream, no stream_options"
WITH_USAGE = "stream, include_usage=true"
LABELS = (f"1 {BUFFERED}", f"2 {NO_OPTIONS}", f"3 {WITH_USAGE}")

TABLE_COLUMNS = (
    "#",
    "model_requested",
    "input",
    "output",
    "cached",
    "cost",
    "decision",
    "session_id",
)

_SECRET = ""
_KEY_ENV = KEY_ENV


def _safe(text: str) -> str:
    """The API key with its value replaced, for anything headed for a stream."""
    return text.replace(_SECRET, "<redacted>") if _SECRET else text


def say(line: str = "") -> None:
    """Print one line of the report, with the API key redacted out of it."""
    print(_safe(line))


@dataclass(frozen=True)
class Probe:
    """One request to send, and the human label used in the report."""

    label: str
    body: dict[str, Any]


@dataclass
class Outcome:
    """What one probe got back from the proxy."""

    label: str
    status: int | None = None
    frames: int = 0
    saw_usage: bool = False
    done: bool = False
    error: str | None = None
    note: str = ""


def probes(model: str) -> tuple[Probe, ...]:
    """The three probes, in the order their rows will be logged."""
    base: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
    return (
        Probe(LABELS[0], dict(base)),
        Probe(LABELS[1], {**base, "stream": True}),
        Probe(LABELS[2], {**base, "stream": True, "stream_options": {"include_usage": True}}),
    )


def _frame(line: bytes) -> dict[str, Any] | str | None:
    """One SSE data line as a dict, the literal ``[DONE]``, or None to ignore."""
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    payload = line[len(b"data:") :].strip()
    if payload == b"[DONE]":
        return "[DONE]"
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _absorb(outcome: Outcome, payload: dict[str, Any] | str | None) -> None:
    """Fold one parsed SSE frame, or one buffered body, into its outcome."""
    if payload is None:
        return
    if payload == "[DONE]":
        outcome.done = True
        return
    outcome.frames += 1
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage:
        outcome.saw_usage = True


async def _send(client: httpx.AsyncClient, probe: Probe, headers: dict[str, str]) -> Outcome:
    """Send one probe and read the whole response, streaming or not.

    The buffered probe needs no care: the proxy logs its row before it answers.
    A streaming probe must be drained to the end, because the proxy logs its row
    when the stream closes — a response abandoned half-read would leave no row.
    """
    outcome = Outcome(label=probe.label)
    try:
        if not probe.body.get("stream"):
            response = await client.post(CHAT_PATH, json=probe.body, headers=headers)
            outcome.status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                outcome.error = f"response was not JSON: {response.text[:200]!r}"
                return outcome
            _absorb(outcome, payload if isinstance(payload, dict) else None)
            outcome.note = "buffered JSON"
            return outcome

        async with client.stream("POST", CHAT_PATH, json=probe.body, headers=headers) as response:
            outcome.status = response.status_code
            buffer = b""
            async for chunk in response.aiter_bytes():
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    _absorb(outcome, _frame(line))
            _absorb(outcome, _frame(buffer))
        outcome.note = "SSE"
        return outcome
    except httpx.HTTPError as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome


def _count(value: Any) -> str:
    """A token count, or ``?`` when the row says UNKNOWN."""
    return UNKNOWN if value is None else str(value)


def _money(value: Any) -> str:
    """A cost in dollars, or ``?``: an unknown cost is never shown as $0."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    if value == 0:
        return "$0.00"
    return f"${value:.6f}" if abs(value) < 1 else f"${value:.4f}"


def _decision(row: dict[str, Any]) -> str:
    """The recorded STAY/SWITCH, i.e. what shadow mode decided it would do."""
    action = row.get("decision_action")
    if not isinstance(action, str):
        return UNKNOWN
    return action


def _row_cells(index: int, row: dict[str, Any]) -> list[str]:
    """One logged row as the table's cells, UNKNOWN rendered as ``?``."""
    return [
        str(index),
        str(row.get("model_requested") or UNKNOWN),
        _count(row.get("input_tokens")),
        _count(row.get("output_tokens")),
        _count(row.get("cached_input_tokens")),
        _money(row.get("cost_usd")),
        _decision(row),
        str(row.get("session_id") or UNKNOWN),
    ]


def print_table(rows: list[dict[str, Any]]) -> None:
    """The last few logged rows, oldest first, with the columns that matter."""
    body = [_row_cells(index, row) for index, row in enumerate(rows, start=1)]
    columns = list(zip(TABLE_COLUMNS, *body, strict=True))
    widths = [max(len(cell) for cell in column) for column in columns]

    def render(cells: tuple[str, ...]) -> str:
        padded = (cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
        return ("  " + "  ".join(padded)).rstrip()

    say(render(TABLE_COLUMNS))
    say("  " + "  ".join("-" * width for width in widths))
    for cells in body:
        say(render(cells))


def read_last_rows(db: Path, limit: int) -> list[dict[str, Any]]:
    """Read the newest ``limit`` rows, oldest first, without writing to the file."""
    uri = f"{db.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM requests ORDER BY id DESC LIMIT ?"
        return [dict(row) for row in reversed(conn.execute(query, (limit,)).fetchall())]
    finally:
        conn.close()


def _row_text(row: dict[str, Any]) -> str:
    return json.dumps(row, default=str, sort_keys=True)


def _tokens(row: dict[str, Any]) -> str:
    return f"input={_count(row.get('input_tokens'))} output={_count(row.get('output_tokens'))}"


def _has_tokens(row: dict[str, Any]) -> bool:
    return row.get("input_tokens") is not None and row.get("output_tokens") is not None


class Checks:
    """Collects PASS/FAIL lines and remembers whether anything failed."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str]] = []

    def check(self, ok: bool, passed: str, failed: str) -> bool:
        self.rows.append((ok, passed if ok else failed))
        return ok

    def report(self) -> bool:
        say("checks")
        for ok, text in self.rows:
            say(f"  {'PASS' if ok else 'FAIL'}  {text}")
        return all(ok for ok, _ in self.rows)


def evaluate(rows: list[dict[str, Any]], db: Path, outcomes: list[Outcome]) -> bool:
    """Run the PASS/FAIL checks over the logged rows and print the verdicts."""
    checks = Checks()

    bad = [
        f"{outcome.label} -> {outcome.error or f'HTTP {outcome.status}'}"
        for outcome in outcomes
        if outcome.error is not None or outcome.status != 200
    ]
    delivered = not bad
    checks.check(
        delivered,
        f"all {len(outcomes)} requests came back HTTP 200",
        f"not every request succeeded: {'; '.join(bad)}",
    )

    if delivered:
        if len(rows) != len(outcomes):
            checks.check(
                False,
                "",
                f"{len(outcomes)} requests were sent but {len(rows)} rows are logged "
                f"({len(outcomes) - len(rows)} missing)",
            )
        else:
            checks.check(True, f"{len(rows)} rows logged, one per request", "")
    else:
        checks.check(
            False,
            "",
            "the log was not judged: the rows above cannot be this run's rows",
        )

    if delivered:
        for index, label in ((1, BUFFERED), (3, WITH_USAGE)):
            if index > len(rows):
                checks.check(False, "", f"request {index} has no row to check")
                continue
            row = rows[index - 1]
            has_counts = _has_tokens(row)
            checks.check(
                has_counts,
                f"request {index} ({label}) logged token counts: {_tokens(row)}",
                f"request {index} ({label}) logged no token counts ({_tokens(row)}); "
                "the upstream reported no usage for a request that asked for it",
            )

        if len(rows) >= 2:
            row = rows[1]
            blank = not _has_tokens(row)
            checks.check(
                blank,
                f"request 2 ({NO_OPTIONS}) logged NULL token counts, "
                "as expected: usage was never requested",
                f"request 2 ({NO_OPTIONS}) logged token counts ({_tokens(row)}) although "
                "the body carried no stream_options, so the proxy added stream_options "
                "the agent did not ask for",
            )

    leaked = [index for index, row in enumerate(rows, start=1) if PROMPT in _row_text(row)]
    checks.check(
        not leaked,
        f"no logged row contains the prompt text {PROMPT!r}",
        f"rows {leaked} contain the prompt text {PROMPT!r}: prompt text reached the log",
    )

    blob = db.read_bytes()
    checks.check(
        PROMPT.encode() not in blob,
        f"no page of {db} contains the prompt text either",
        f"the prompt text is present in the bytes of {db}",
    )

    if _SECRET:
        keyed = [index for index, row in enumerate(rows, start=1) if _SECRET in _row_text(row)]
        checks.check(
            not keyed and _SECRET.encode() not in blob,
            f"no logged row or page contains the {_KEY_ENV} value",
            f"the {_KEY_ENV} value is present in the log; stop using this database",
        )

    return checks.report()


def print_outcomes(outcomes: list[Outcome]) -> None:
    say("requests")
    for outcome in outcomes:
        status = "----" if outcome.status is None else str(outcome.status)
        detail = f"HTTP {status}  {outcome.note}"
        if outcome.note == "SSE":
            detail += f"  frames={outcome.frames}  usage={'yes' if outcome.saw_usage else 'no'}"
            detail += f"  done={'yes' if outcome.done else 'no'}"
        else:
            detail += f"  usage={'yes' if outcome.saw_usage else 'no'}"
        if outcome.error:
            detail += f"  ERROR {outcome.error}"
        say(f"  {outcome.label:<30} {detail}")


def header(proxy: str, model: str, db: Path, mock: bool) -> None:
    say("tamias live check")
    say(f"  proxy   {proxy}" + ("  (in-process mock upstream, no socket)" if mock else ""))
    say(f"  model   {model}")
    say(f"  db      {db}" + ("  (temporary)" if mock else ""))
    say(f"  prompt  {PROMPT!r}  x3  session {SESSION_ID}")
    say(f"  key     {'in-process, none needed' if mock else f'${_KEY_ENV} set, never printed'}")


def finish(db: Path, outcomes: list[Outcome]) -> int:
    print_outcomes(outcomes)
    say()
    try:
        rows = read_last_rows(db, len(outcomes))
    except (OSError, sqlite3.DatabaseError) as exc:
        say(f"could not read {db}: {exc}")
        say()
        say("RESULT: FAIL")
        return 1

    say(f"logged rows in {db} (newest {len(outcomes)}, oldest first)")
    if not rows:
        say("  (none)")
    else:
        print_table(rows)
    say(f"  {UNKNOWN} means UNKNOWN, which is never stored as 0")
    if any(outcome.error is not None or outcome.status != 200 for outcome in outcomes):
        say("  at least one request did not complete: these rows may be from an earlier run")
    say()

    passed = evaluate(rows, db, outcomes)
    say()
    say(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def mock_sheet(model: str, directory: Path) -> Path:
    """A price sheet for the mock run, declaring the model free (every rate 0).

    The mock upstream reports fixed token counts, so any cost the proxy logs is
    this sheet's arithmetic applied to those fixed counts, not a measurement of
    anything that costs money.
    """
    path = directory / "prices.toml"
    path.write_text(
        f'date = "{MOCK_SHEET_DATE}"\n\n[{model}]\ninput = 0.0\noutput = 0.0\n',
        encoding="utf-8",
    )
    return path


async def _run_mock(args: argparse.Namespace, db: Path) -> int:
    """Drive the three probes through the real proxy onto the tests' mock upstream."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from mock_upstream import StreamingASGITransport, create_mock_upstream

    from tamias import proxy
    from tamias.pricing import load_price_sheet
    from tamias.router import EASY_TOOLS, RouterConfig
    from tamias.store import Store

    with tempfile.TemporaryDirectory(prefix="tamias-live-check-") as name:
        sheet = load_price_sheet(mock_sheet(args.model, Path(name)))
        store = Store(db)
        app = proxy.create_app(
            MOCK_UPSTREAM_URL,
            store,
            sheet,
            "shadow",
            config=RouterConfig(
                easy_tools=EASY_TOOLS,
                cheap_model=MOCK_CHEAP_MODEL,
                strong_model=args.model,
                min_gap=3,
            ),
            transport=StreamingASGITransport(create_mock_upstream()),
        )
        headers = {SESSION_HEADER: SESSION_ID}
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url=args.proxy,
            timeout=None,
        ) as client:
            outcomes = [await _send(client, probe, headers) for probe in probes(args.model)]
        store.close()
    return finish(db, outcomes)


async def _run_live(args: argparse.Namespace, db: Path) -> int:
    """Drive the three probes through a proxy that is already listening."""
    headers = {"authorization": f"Bearer {_SECRET}", SESSION_HEADER: SESSION_ID}
    async with httpx.AsyncClient(base_url=args.proxy, timeout=120.0) as client:
        outcomes = []
        for probe in probes(args.model):
            outcomes.append(await _send(client, probe, headers))
    return finish(db, outcomes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_check.py",
        description=(
            "Send three requests through a running tamias proxy and check what it logged. "
            f"The API key is read from ${KEY_ENV} (override with --api-key-env) and is "
            "never printed."
        ),
    )
    parser.add_argument(
        "--proxy", default=DEFAULT_PROXY, help=f"proxy base URL (default: {DEFAULT_PROXY})"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"model to ask for (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"sqlite request log to read (default: {DEFAULT_DB}, or a temporary file with --mock)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run against the tests' mock upstream in-process, with no key and no socket",
    )
    parser.add_argument(
        "--api-key-env",
        default=KEY_ENV,
        metavar="NAME",
        help=(
            "environment variable holding the API key (default: %(default)s). "
            "Use OPENROUTER_API_KEY for an OpenRouter upstream. Only the name is printed."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global _SECRET, _KEY_ENV
    args = build_parser().parse_args(argv)
    _KEY_ENV = args.api_key_env

    if args.mock:
        with tempfile.TemporaryDirectory(prefix="tamias-live-check-") as name:
            db = Path(args.db) if args.db else Path(name) / "requests.db"
            header(args.proxy, args.model, db, mock=True)
            say()
            try:
                return asyncio.run(_run_mock(args, db))
            except (httpx.HTTPError, OSError, sqlite3.DatabaseError) as exc:
                say(f"live check aborted: {exc}")
                say()
                say("RESULT: FAIL")
                return 1

    _SECRET = os.environ.get(_KEY_ENV, "").strip()
    if not _SECRET:
        say(f"{_KEY_ENV} is not set; export the API key, or use --mock.")
        return 2
    db = Path(args.db or DEFAULT_DB)
    if not db.is_file():
        say(f"no request log at {db}; start tamias serve with --db {db} first")
        return 2

    header(args.proxy, args.model, db, mock=False)
    say()
    try:
        return asyncio.run(_run_live(args, db))
    except httpx.HTTPError as exc:
        say(f"cannot reach the proxy at {args.proxy}: {exc}")
        say()
        say("RESULT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
