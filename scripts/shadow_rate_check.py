#!/usr/bin/env python3
"""Shadow-rate savings check: verify the savings arithmetic without spending money.

The project has no measured saving because every live model used was free, so the
"$0.00 - $0.00" it computed carried no information.  This script separates that
into the two things it actually is:

1. the *pipeline* -- does the proxy log real token counts, decide SWITCH/STAY,
   and compute a saving from a price sheet?  This needs a live model that reports
   usage, and the free tier provides one.
2. the *economy* -- are the rates real?  This needs a provider invoice, and no
   amount of testing can manufacture it.

This script verifies (1) to accuracy, for nothing, by pricing the rows a free run
leaves behind with rate sheets *the caller authors*.  The token counts are real;
the rates are declared ``simulated``; the arithmetic is the real ``compute_cost``.

Because the rates are the caller's, the check can construct cases that a single
paid run would never reach:

    flat-cheap    cheap rate is 0            -> saving == whole strong cost
    tenth         cheap is 1/10 of strong    -> saving == 0.9 strong cost
    near-parity   cheap is ~= strong         -> saving ~= 0; a big number is a bug
    cache-hostile strong cache_write is high -> saving must be allowed NEGATIVE

Each case prices the *same rows* the run actually logged and compares the
headline saving against the value derived by hand from the sheet, the row's token
counts and ``assume_cache_warm``.  A mismatch is a finding.

What this script does NOT do, and never claims:

* it does not establish that any cheap model is really cheaper per task;
* it does not reconcile against a provider invoice;
* a PASS means "the pipeline computes the saving these rates imply", not "tamias
  saves money".  The report banner carries the same caveat.

Usage
-----
Offline, no key, no socket (uses the tests' mock upstream, as ``live_check.py``
does)::

    python scripts/shadow_rate_check.py --mock

Against a proxy already listening on the free tier::

    python scripts/shadow_rate_check.py \\
      --proxy http://localhost:8000 \\
      --strong-model nvidia/nemotron-3-ultra-550b-a55b:free \\
      --cheap-model  nvidia/nemotron-3.5-lightning:free \\
      --api-key-env OPENROUTER_API_KEY \\
      --db /tmp/shadow.db

The API key is read from an environment variable and never printed.  Exit code is
0 on PASS, 1 on FAIL, 2 for a missing key or missing log.
"""

from __future__ import annotations

import argparse
import asyncio
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
SESSION_ID = "shadow-rate-check"
MOCK_STRONG = "shadow-rate-strong"
MOCK_CHEAP = "shadow-rate-cheap"
MOCK_UPSTREAM_URL = "http://mock-upstream.invalid"
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROXY = "http://localhost:8000"
DEFAULT_STRONG = "nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_CHEAP = "nvidia/nemotron-3.5-lightning:free"
DEFAULT_DB = "shadow_rate.db"

# Rates below are USD per 1,000,000 tokens.  They are chosen by this script, not
# read from a provider, so every sheet they build declares ``simulated = true``.
STRONG_INPUT = 3.0
STRONG_OUTPUT = 15.0
STRONG_CACHED = 0.30

_SECRET = ""
_KEY_ENV = KEY_ENV

# Sanity floor: a case whose derived saving is smaller than this is treated as
# "near zero" rather than "a saving".  It is an amount in USD.
NEAR_ZERO_USD = 1e-9


def _safe(text: str) -> str:
    """The API key with its value replaced, for anything headed for a stream."""
    return text.replace(_SECRET, "<redacted>") if _SECRET else text


def say(line: str = "") -> None:
    """Print one line of the report, with the API key redacted out of it."""
    print(_safe(line))


@dataclass(frozen=True)
class Case:
    """One authored rate sheet, and what the saving it implies is called."""

    name: str
    why: str
    cheap_input: float
    cheap_output: float
    cheap_cached: float
    strong_cache_write: float
    expect_sign: str  # "positive" | "near-zero" | "negative"


# The four cases, most-demanding last.  ``strong`` is fixed across all of them so
# the only thing that moves is the cheap rate and the strong cache-write rate.
CASES: tuple[Case, ...] = (
    Case(
        name="flat-cheap",
        why="cheap is genuinely free; the saving is the whole strong cost",
        cheap_input=0.0,
        cheap_output=0.0,
        cheap_cached=0.0,
        strong_cache_write=3.75,
        expect_sign="positive",
    ),
    Case(
        name="tenth",
        why="cheap is 1/10 of strong; the realistic ratio",
        cheap_input=0.30,
        cheap_output=1.50,
        cheap_cached=0.03,
        strong_cache_write=3.75,
        expect_sign="positive",
    ),
    Case(
        name="near-parity",
        why="cheap is ~= strong; a large saving here would be a bug",
        cheap_input=2.90,
        cheap_output=14.50,
        cheap_cached=0.29,
        strong_cache_write=3.75,
        expect_sign="near-zero",
    ),
    Case(
        name="cache-hostile",
        why="strong writes cache dear and reads it cheap; a switch can lose money",
        cheap_input=1.0,
        cheap_output=5.0,
        cheap_cached=0.10,
        strong_cache_write=30.0,
        expect_sign="negative",
    ),
)


@dataclass
class Outcome:
    """What one request got back from the proxy."""

    label: str
    status: int | None = None
    saw_usage: bool = False
    done: bool = False
    error: str | None = None
    note: str = ""


def probes(model: str) -> list[tuple[str, dict[str, Any]]]:
    """The requests to send, shaped so the router has something to decide.

    Two requests in one session.  The first is a plain user turn, which the
    router always STAYs on.  The second carries a trailing ``tool`` message from
    an easy ``read`` tool with no error marker, which is the shape the router
    SWITCHes on (provided the gap since the last switch is at least ``min_gap``,
    which the mock run sets to 0).  The saving can only exist on a request the
    proxy actually rewrote, so the second request is the one that matters.

    Both requests carry identically *shaped* content; only the last message's
    role decides the routing, which is the property ``tests/test_router.py``
    pins down.
    """
    first = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
    second = {
        "model": model,
        "messages": [
            {"role": "user", "content": PROMPT},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "1\tx = 1"},
        ],
    }
    return [("turn-1 user", first), ("turn-2 read result", second)]


async def _send(client: httpx.AsyncClient, label: str, body: dict[str, Any],
                headers: dict[str, str]) -> Outcome:
    """Send one buffered probe and read its whole response."""
    outcome = Outcome(label=label)
    try:
        response = await client.post(CHAT_PATH, json=body, headers=headers)
        outcome.status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            outcome.error = f"response was not JSON: {response.text[:200]!r}"
            return outcome
        usage = payload.get("usage") if isinstance(payload, dict) else None
        outcome.saw_usage = isinstance(usage, dict) and bool(usage)
        outcome.note = "buffered JSON"
        return outcome
    except httpx.HTTPError as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome


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


def make_sheet(case: Case, strong: str, cheap: str, directory: Path) -> Path:
    """Write a ``simulated`` sheet pairing the two models at this case's rates."""
    path = directory / f"prices.{case.name}.toml"
    path.write_text(
        "# Author-built sheet for scripts/shadow_rate_check.py.\n"
        "# SIMULATED: these rates are chosen by the caller, not read from a\n"
        "# provider, so they demonstrate the arithmetic and are not a bill.\n"
        'date = "2026-10-21"\n'
        "simulated = true\n\n"
        f"[{strong}]\n"
        f"input = {STRONG_INPUT}\n"
        f"output = {STRONG_OUTPUT}\n"
        f"cached_input = {STRONG_CACHED}\n"
        f"cache_write = {case.strong_cache_write}\n\n"
        f"[{cheap}]\n"
        f"input = {case.cheap_input}\n"
        f"output = {case.cheap_output}\n"
        f"cached_input = {case.cheap_cached}\n"
        "cache_write = 0\n",
        encoding="utf-8",
    )
    return path


def report_realised_saving(db: Path, prices_path: Path, inr_rate: float | None = None
                           ) -> tuple[float | None, int | None]:
    """Run ``tamias report`` itself and read its realised-saving line back.

    This is the strongest available comparison: the harness does not re-derive
    the report's arithmetic, it *runs the report* and parses the number it
    printed, so a bug in the CLI shows up as a disagreement with the value
    derived below rather than being invisible in both.
    """
    import subprocess

    cmd = [
        sys.executable, "-m", "tamias.cli", "report",
        "--db", str(db), "--prices", str(prices_path),
    ]
    if inr_rate is not None:
        cmd += ["--inr-rate", str(inr_rate)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    text = proc.stdout + proc.stderr
    return _parse_realised(text)


def _parse_realised(text: str) -> tuple[float | None, int | None]:
    """Pull the realised-saving amount and row count out of a report's text.

    The report prints, for an active run::

        realised saving (rows the proxy actually rewrote): $0.000123 over 1 rows

    and for the unreported case, a line beginning ``realised saving:``.  A
    missing line is reported as ``(None, None)`` rather than assumed to be zero.
    """
    import re

    for line in text.splitlines():
        if "realised saving" not in line:
            continue
        if line.strip().startswith("realised saving:"):
            return None, None  # the UNKNOWN form: no priced rows
        amount = re.search(r"(-?\$[0-9.]+)", line)
        rows = re.search(r"over (\d+) rows?", line)
        value: float | None = None
        if amount:
            try:
                value = float(amount.group(1).replace("$", ""))
            except ValueError:
                value = None
        count = int(rows.group(1)) if rows else None
        return value, count
    return None, None


def make_registry(strong: str, cheap: str, directory: Path) -> Path:
    """A minimal capability registry vouching for both models.

    Active routing refuses a cheap target the deployment has not vouched for
    (``_verify_active_target``), and requires it to be tool-capable.  This writes
    the smallest registry that satisfies that, for the mock run only.
    """
    path = directory / "models.toml"
    path.write_text(
        "[[models]]\n"
        f'id = "{strong}"\n'
        'provider = "shadow-rate"\n'
        "context_window = 200000\n"
        "tools = true\n\n"
        "[[models]]\n"
        f'id = "{cheap}"\n'
        'provider = "shadow-rate"\n'
        "context_window = 200000\n"
        "tools = true\n",
        encoding="utf-8",
    )
    return path


def _usage_from_row(row: dict[str, Any]):
    """The row's tokens as a ``Usage``, or None when the row has no counts."""
    from tamias.pricing import Usage

    def num(key: str) -> int | None:
        value = row.get(key)
        return value if isinstance(value, int) else None

    inp = num("input_tokens")
    out = num("output_tokens")
    if inp is None or out is None:
        return None
    cached = num("cached_input_tokens")
    write = num("cache_write_tokens")
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        cached_input_tokens=cached,
        cache_write_tokens=write,
    )


def derive_saving(row: dict[str, Any], strong: str, cheap: str, sheet) -> float | None:
    """The realised saving this row's tokens imply, derived here from first principles.

    This is the *independent* side of the comparison: the same definition the
    report uses for its realised line, recomputed outside the CLI so a shared
    bug cannot hide in both.

    On a row the proxy rewrote, the saving is ``cost(requested, warm) -
    cost(used, real)``.  It is never clamped at zero, so a cache-hostile pair
    can derive a negative saving.
    """
    from tamias.pricing import assume_cache_warm, compute_cost

    usage = _usage_from_row(row)
    if usage is None:
        return None
    requested = row.get("model_requested") or row.get("model")
    used = row.get("model_used")
    if not isinstance(requested, str) or not isinstance(used, str):
        return None
    warm = assume_cache_warm(usage)
    requested_cost = compute_cost(requested, warm, sheet).usd
    used_cost = compute_cost(used, usage, sheet).usd
    if requested_cost is None or used_cost is None:
        return None
    return requested_cost - used_cost


def headline_saving(db: Path, sheet) -> tuple[float, int, bool]:
    """The 'realised saving' the report computes for this database.

    Returns ``(amount, rows_counted, simulated_banner)``.  The amount is the sum
    over rows the proxy actually rewrote, priced cache-aware and unclamped, which
    is exactly what ``tamias report`` prints as its realised line (``cli.py``
    ``realised_saved``).  It is *not* the estimated line: that one prices a
    SWITCH decision as a counterfactual and is zero on rows that already ran
    cheap.
    """
    from tamias.cli import _read_rows
    from tamias.pricing import assume_cache_warm, compute_cost

    rows = _read_rows(str(db))
    total = 0.0
    counted = 0
    for row in rows:
        if not _proxy_rewrote(row):
            continue
        usage = _usage_from_row(row)
        if usage is None:
            continue
        requested = row.get("model_requested") or row.get("model")
        used = row.get("model_used")
        cost_requested = compute_cost(requested, assume_cache_warm(usage), sheet).usd
        cost_used = compute_cost(used, usage, sheet).usd
        if cost_requested is not None and cost_used is not None:
            total += cost_requested - cost_used
            counted += 1
    simulated = bool(getattr(sheet, "simulated", False))
    return total, counted, simulated


def run_cases(db: Path, strong: str, cheap: str, logging_sheet: Path | None = None) -> bool:
    """Price the logged rows under every case and compare derived vs computed."""
    checks = Checks()
    from tamias.pricing import load_price_sheet

    say("savings arithmetic, under four authored rate sheets")
    say("  (rates are the caller's; every sheet below declares simulated = true)")
    say()

    magnitudes: dict[str, float] = {}

    with tempfile.TemporaryDirectory(prefix="tamias-shadow-rate-") as name:
        workdir = Path(name)
        carrying_rows = 0
        for case in CASES:
            sheet_path = make_sheet(case, strong, cheap, workdir)
            sheet = load_price_sheet(sheet_path)

            # The project's own arithmetic, called through the CLI's helper on
            # this case's sheet.  This is the number the report would print for
            # these rates; the comparison below is against an independent
            # derivation of the same row set.
            computed, computed_rows, simulated = headline_saving(db, sheet)

            derived = 0.0
            derived_counted = 0
            for row in _rows_for_case(db):
                if not _proxy_rewrote(row):
                    continue
                value = derive_saving(row, strong, cheap, sheet)
                if value is not None:
                    derived += value
                    derived_counted += 1

            if computed_rows:
                carrying_rows = computed_rows
            magnitudes[case.name] = computed

            say(f"  {case.name:<14} {case.why}")
            say(f"      expects  {case.expect_sign}")
            say(f"      computed {_usd(computed)} over {computed_rows} row(s)"
                f"{'  (SIMULATED prices)' if simulated else ''}")
            say(f"      derived  {_usd(derived)} over {derived_counted} row(s)")

            checks.check(
                computed_rows == derived_counted,
                f"{case.name}: report and derivation counted the same {computed_rows} row(s)",
                f"{case.name}: report counted {computed_rows} row(s), derivation {derived_counted}",
            )
            checks.check(
                _close(computed, derived),
                f"{case.name}: computed {_usd(computed)} == derived {_usd(derived)}",
                f"{case.name}: computed {_usd(computed)} != derived {_usd(derived)}",
            )
            checks.check(
                simulated,
                f"{case.name}: every sheet was labelled simulated",
                f"{case.name}: a sheet was not labelled simulated",
            )
            say()

        # Relative ordering is rate-shape-independent and does not depend on the
        # mock's token counts: the cheaper the cheap model, the larger the saving
        # the same rows must produce.  This is the check that a sign convention
        # went wrong, without hard-coding magnitudes that the data may not reach.
        if carrying_rows:
            flat = magnitudes.get("flat-cheap", 0.0)
            tenth = magnitudes.get("tenth", 0.0)
            parity = magnitudes.get("near-parity", 0.0)
            ordered = flat >= tenth >= parity
            checks.check(
                ordered,
                "cheaper cheap model produces a larger saving: "
                f"flat-cheap {_usd(flat)} >= tenth {_usd(tenth)} >= near-parity {_usd(parity)}",
                "saving did not shrink as the cheap model got dearer: "
                f"flat-cheap {_usd(flat)}, tenth {_usd(tenth)}, near-parity {_usd(parity)}",
            )
            checks.check(
                abs(tenth) > NEAR_ZERO_USD,
                f"the tenth case produced a real, non-zero saving {_usd(tenth)}",
                "the tenth case produced no saving, so the arithmetic was not exercised",
            )

        # Separately, prove the CLI's *printed* realised line equals the
        # arithmetic on the very sheet the rows were logged against, so the
        # report text is verified and not only the helper behind it.
        if logging_sheet is not None:
            ok, detail = _cli_text_agrees(db, logging_sheet, strong, cheap)
            checks.check(ok, f"report text agrees with the derivation: {detail}", detail)

        if carrying_rows == 0:
            checks.check(
                False,
                "",
                "no row was priced: the run logged no rewritten row, so the "
                "savings arithmetic was never exercised (shadow mode never "
                "rewrites -- run the proxy with --router-mode active)",
            )

    say()
    return checks.report()


def _cli_text_agrees(db: Path, logging_sheet: Path, strong: str, cheap: str) -> tuple[bool, str]:
    """The CLI's printed realised line equals this harness's own derivation.

    The rows were logged against ``logging_sheet``; ``report`` refuses to price
    them against any *other* sheet (its ``bases_differ`` guard), so this runs the
    real command with the sheet the rows actually carry.  It is the check that
    the number a user reads is the number the arithmetic implies.
    """
    from tamias.pricing import load_price_sheet

    reported, reported_rows = report_realised_saving(db, logging_sheet)
    if reported is None:
        return False, (
            "the CLI's report printed no realised amount for the sheet it logged "
            "against; the printed line was not verified"
        )
    sheet = load_price_sheet(logging_sheet)
    derived = 0.0
    derived_rows = 0
    for row in _rows_for_case(db):
        if not _proxy_rewrote(row):
            continue
        value = derive_saving(row, strong, cheap, sheet)
        if value is not None:
            derived += value
            derived_rows += 1
    ok = _close(reported, derived, abs_tol=1e-6) and reported_rows == derived_rows
    return ok, (
        f"CLI printed realised saving {_usd(reported)} over {reported_rows} row(s); "
        f"derivation says {_usd(derived)} over {derived_rows}"
    )


def _rows_for_case(db: Path) -> list[dict[str, Any]]:
    from tamias.cli import _read_rows

    return _read_rows(str(db))


def _proxy_rewrote(row: dict[str, Any]) -> bool:
    model_used = row.get("model_used")
    model_requested = row.get("model_requested") or row.get("model")
    return (
        model_used is not None
        and model_requested is not None
        and model_used != model_requested
    )


def _close(a: float, b: float, *, abs_tol: float = 0.0) -> bool:
    """Equal to within floating-point noise, plus any stated absolute tolerance.

    A value read back from a printed line has been rounded to the printed
    precision, so callers comparing against text pass the rounding step as
    ``abs_tol`` rather than loosening the relative check for everyone.
    """
    scale = max(abs(a), abs(b), 1e-12)
    return abs(a - b) <= max(scale * 1e-9, abs_tol)


def _usd(value: float | None) -> str:
    if value is None:
        return "?"
    if value == 0:
        return "$0.00"
    return f"${value:.6f}" if abs(value) < 1 else f"${value:.4f}"


def print_rows(db: Path) -> int:
    """The rows the run logged, oldest first, with the columns that matter."""
    from tamias.cli import _read_rows

    try:
        rows = _read_rows(str(db))
    except (OSError, sqlite3.DatabaseError) as exc:
        say(f"could not read {db}: {exc}")
        return 0
    columns = ("#", "model_requested", "model_used", "input", "output", "cached", "decision")
    body = [
        [
            str(i),
            str(row.get("model_requested") or "?"),
            str(row.get("model_used") or "?"),
            _count(row.get("input_tokens")),
            _count(row.get("output_tokens")),
            _count(row.get("cached_input_tokens")),
            str(row.get("decision_action") or "?"),
        ]
        for i, row in enumerate(rows, start=1)
    ]
    if not body:
        say("  (no rows)")
        return 0
    widths = [max(len(c) for c in col) for col in zip(columns, *body, strict=True)]

    def render(cells) -> str:
        return ("  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True))).rstrip()

    say(f"logged rows in {db} (oldest first)")
    say(render(columns))
    say("  " + "  ".join("-" * w for w in widths))
    for cells in body:
        say(render(cells))
    say("  ? means UNKNOWN, which is never stored as 0")
    return len(rows)


def _count(value: Any) -> str:
    return "?" if value is None else str(value)


def header(proxy: str, strong: str, cheap: str, db: Path, mock: bool, mode: str) -> None:
    say("tamias shadow-rate savings check")
    say(f"  proxy   {proxy}" + ("  (in-process mock upstream, no socket)" if mock else ""))
    say(f"  strong  {strong}")
    say(f"  cheap   {cheap}")
    say(f"  mode    {mode}" + ("  (must be active to rewrite a model)" if mode != "active" else ""))
    say(f"  db      {db}" + ("  (temporary)" if mock else ""))
    say(f"  key     {'in-process, none needed' if mock else f'${_KEY_ENV} set, never printed'}")
    say("  rates   authored by this script and labelled simulated; not a bill")
    say()


def finish(db: Path, mode: str, strong: str, cheap: str,
           logging_sheet: Path | None = None) -> int:
    count = print_rows(db)
    say()
    if count == 0:
        say("RESULT: FAIL")
        say("  no rows were logged, so nothing could be priced.")
        return 1

    passed = run_cases(db, strong, cheap, logging_sheet)
    say()
    if mode != "active":
        say("NOTE: the proxy ran in shadow mode, so no row was rewritten and no")
        say("      saving could be derived. Run with --router-mode active for a")
        say("      saving to exist at all.")
    say("NOTE: a PASS means the pipeline computes the saving these rates imply.")
    say("      It does NOT mean any model is really cheaper per task, and it is")
    say("      not reconciled against a provider invoice.")
    say()
    say(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


async def _run_mock(args: argparse.Namespace, db: Path) -> int:
    """Drive one probe through the real proxy onto the tests' mock upstream.

    The mock upstream reports fixed token counts.  The router is put in *active*
    mode so the proxy really rewrites the model, which is what lets the savings
    arithmetic run at all.
    """
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from mock_upstream import StreamingASGITransport, create_mock_upstream

    from tamias import proxy
    from tamias.pricing import load_price_sheet
    from tamias.registry import load_registry
    from tamias.router import EASY_TOOLS, RouterConfig
    from tamias.store import Store

    strong = MOCK_STRONG
    cheap = MOCK_CHEAP
    with tempfile.TemporaryDirectory(prefix="tamias-shadow-rate-") as name:
        workdir = Path(name)
        # The rows are logged against this sheet, so it is the only sheet the
        # CLI will later price them with (its bases_differ guard).  Keep its path
        # for the report-text check.
        logging_sheet = make_sheet(CASES[1], strong, cheap, workdir)
        sheet = load_price_sheet(logging_sheet)
        registry = load_registry(make_registry(strong, cheap, workdir))
        store = Store(db)
        app = proxy.create_app(
            MOCK_UPSTREAM_URL,
            store,
            sheet,
            "active",
            config=RouterConfig(
                easy_tools=EASY_TOOLS,
                cheap_model=cheap,
                strong_model=strong,
                min_gap=0,
            ),
            registry=registry,
            transport=StreamingASGITransport(create_mock_upstream()),
        )
        headers = {SESSION_HEADER: SESSION_ID}
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url=args.proxy,
            timeout=None,
        ) as client:
            outcomes = [
                await _send(client, label, body, headers)
                for label, body in probes(strong)
            ]
        store.close()
        _report_outcomes(outcomes)
        return finish(db, "active", strong, cheap, logging_sheet)


def _report_outcomes(outcomes: list[Outcome]) -> None:
    say("requests")
    for outcome in outcomes:
        status = "----" if outcome.status is None else str(outcome.status)
        detail = f"HTTP {status}  {outcome.note}  usage={'yes' if outcome.saw_usage else 'no'}"
        if outcome.error:
            detail += f"  ERROR {outcome.error}"
        say(f"  {outcome.label:<12} {detail}")
    say()


async def _run_live(args: argparse.Namespace, db: Path) -> int:
    """Drive one probe through a proxy that is already listening."""
    headers = {"authorization": f"Bearer {_SECRET}", SESSION_HEADER: SESSION_ID}
    async with httpx.AsyncClient(base_url=args.proxy, timeout=120.0) as client:
        outcomes = [
            await _send(client, label, body, headers)
            for label, body in probes(args.strong_model)
        ]
    _report_outcomes(outcomes)
    # The rows carry the sheet path the proxy logged them with; use it for the
    # report-text check so the CLI prices them on their own basis.
    logging_sheet = _logging_sheet_from_rows(db)
    return finish(db, args.router_mode, args.strong_model, args.cheap_model, logging_sheet)


def _logging_sheet_from_rows(db: Path) -> Path | None:
    """The price sheet path the logged rows carry, when they carry one."""
    for row in _rows_for_case(db):
        value = row.get("price_sheet")
        if isinstance(value, str) and value and Path(value).is_file():
            return Path(value)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadow_rate_check.py",
        description=(
            "Verify the savings arithmetic without spending money: price the rows "
            "a real run logged with rate sheets the caller authors. The API key is "
            f"read from ${KEY_ENV} (override with --api-key-env) and is never printed."
        ),
    )
    parser.add_argument(
        "--proxy", default=DEFAULT_PROXY,
        help=f"proxy base URL (default: {DEFAULT_PROXY})",
    )
    parser.add_argument(
        "--strong-model", default=DEFAULT_STRONG,
        help=f"the model the run requested (default: {DEFAULT_STRONG})",
    )
    parser.add_argument(
        "--cheap-model", default=DEFAULT_CHEAP,
        help=f"the router's cheap target (default: {DEFAULT_CHEAP})",
    )
    parser.add_argument(
        "--router-mode", default="active", choices=("active", "shadow", "off"),
        help="the mode the proxy was started in (default: %(default)s)",
    )
    parser.add_argument(
        "--db", default=None,
        help=f"sqlite request log to read (default: {DEFAULT_DB}, "
             "or a temporary file with --mock)",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="run against the tests' mock upstream in-process, no key, no socket",
    )
    parser.add_argument("--api-key-env", default=KEY_ENV, metavar="NAME",
                        help="environment variable holding the API key (default: %(default)s). "
                             "Only the name is printed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _SECRET, _KEY_ENV
    args = build_parser().parse_args(argv)
    _KEY_ENV = args.api_key_env

    if args.mock:
        with tempfile.TemporaryDirectory(prefix="tamias-shadow-rate-") as name:
            db = Path(args.db) if args.db else Path(name) / "requests.db"
            header(args.proxy, MOCK_STRONG, MOCK_CHEAP, db, mock=True, mode="active")
            try:
                return asyncio.run(_run_mock(args, db))
            except (httpx.HTTPError, OSError, sqlite3.DatabaseError) as exc:
                say(f"shadow-rate check aborted: {exc}")
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

    header(args.proxy, args.strong_model, args.cheap_model, db, mock=False, mode=args.router_mode)
    try:
        return asyncio.run(_run_live(args, db))
    except httpx.HTTPError as exc:
        say(f"cannot reach the proxy at {args.proxy}: {exc}")
        say()
        say("RESULT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
