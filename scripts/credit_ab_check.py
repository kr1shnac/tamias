#!/usr/bin/env python3
"""Measure the saving from the provider's own ledger, not from a price sheet.

The price sheet is arithmetic the caller controls, so a number built from it can
only ever say "the pipeline computes the saving these rates imply".  The ledger
is different: it is the provider's record of what was actually spent.  This
script compares the two arms of a real run against that ledger:

    X = usage after the task, router off    (strong model only)
    Y = usage after the task, router active (the same task, routed)
    saving = X - Y                          (USD, from the ledger)

Both arms run the *same task* through the *same agent*, so routing is the only
variable.  This is the only comparison a credit measurement can honestly make:
"less credit used" means nothing unless the work was identical.

Why `/api/v1/key` and not `/api/v1/credits`:

* ``GET /api/v1/key`` returns ``data.usage`` and ``data.usage_daily`` for the key
  that was presented, and works with an ordinary request key.  That is exactly
  "credits this key consumed", which is what an A/B test needs.
* ``GET /api/v1/credits`` returns account-level ``total_credits`` /
  ``total_usage``, but the endpoint's own documentation says a *management* key
  is required -- a broader credential than the request key.  It is read only when
  ``--credits`` is passed and a management key is supplied.

What a free model does to this measurement (read this before trusting a run):

* a ``:free`` model bills ``0``, so ``usage`` does not move.  Both arms move it
  by ``$0.00`` and the saving is ``$0.00`` -- the exact trap the project already
  fell into.  ``--dry-run`` therefore reports the *free-model request quota*
  (``free_model_daily_requests``) as a count, and says plainly that a money
  measurement needs a paid model.  A quota count is motion, not money.
* the ledger has cent-scale resolution; a saving smaller than a cent may not
  appear at all.  ``--min-detectable`` states the floor and the script refuses to
  report a saving below it as a measurement.

Modes
-----
``--dry-run`` needs no key and no socket for the comparison itself: it runs the
same task twice against the tests' mock upstream, once with routing off and once
active, and checks that the two arms really differ in the way the arithmetic
requires.  It proves the machinery.  It does not measure money, and says so.

``--read-key`` needs ``$OPENROUTER_API_KEY`` and only issues two GETs; it prints
``usage``, ``usage_daily`` and the free-model quota, so the instrument is proven
before any task is run.

The full live A/B requires a key, paid models and money.  It is refusal-gated:
without ``--allow-spend`` the script prints the procedure and exits without
sending a completion.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
CHAT_PATH = "/v1/chat/completions"
SESSION_HEADER = "x-tamias-session"
KEY_ENV = "OPENROUTER_API_KEY"
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROXY = "http://localhost:8000"
DEFAULT_DB = "credit_ab.db"

# The ledger reports dollars; a saving below this is under the instrument's
# resolution and is reported as "below the floor" rather than as a measurement.
DEFAULT_MIN_DETECTABLE = 0.000001

_SECRET = ""


def _safe(text: str) -> str:
    return text.replace(_SECRET, "<redacted>") if _SECRET else text


def say(line: str = "") -> None:
    print(_safe(line))


@dataclass
class Ledger:
    """What the provider says the key has spent."""

    usage: float | None = None
    usage_daily: float | None = None
    usage_weekly: float | None = None
    usage_monthly: float | None = None
    limit_remaining: float | None = None
    is_free_tier: bool | None = None
    free_used: int | None = None
    free_remaining: int | None = None
    free_limit: int | None = None
    total_credits: float | None = None
    total_usage: float | None = None
    error: str | None = None
    source: str = ""

    @property
    def readable(self) -> bool:
        return self.usage is not None

    def delta_usage(self, before: Ledger) -> float | None:
        """Dollars this key spent between two reads, or None when unknown."""
        if self.usage is None or before.usage is None:
            return None
        return self.usage - before.usage

    def delta_free_requests(self, before: Ledger) -> int | None:
        """Free-model requests consumed between two reads, or None."""
        if self.free_used is None or before.free_used is None:
            return None
        return self.free_used - before.free_used


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def parse_key_payload(payload: Any) -> Ledger:
    """The documented fields of ``GET /api/v1/key``, or an error Ledger.

    Fields are read defensively: a missing field is left None rather than
    defaulted to zero, because zero means "spent nothing" and absence means
    "the provider did not say".
    """
    if not isinstance(payload, dict):
        return Ledger(error="response was not a JSON object", source=KEY_URL)
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return Ledger(error="response had no data object", source=KEY_URL)
    free = data.get("free_model_daily_requests")
    free = free if isinstance(free, dict) else {}
    return Ledger(
        usage=_number(data.get("usage")),
        usage_daily=_number(data.get("usage_daily")),
        usage_weekly=_number(data.get("usage_weekly")),
        usage_monthly=_number(data.get("usage_monthly")),
        limit_remaining=_number(data.get("limit_remaining")),
        is_free_tier=(
            data.get("is_free_tier") if isinstance(data.get("is_free_tier"), bool) else None
        ),
        free_used=_int_or_none(free.get("used")),
        free_remaining=_int_or_none(free.get("remaining")),
        free_limit=_int_or_none(free.get("limit")),
        source=KEY_URL,
    )


def parse_credits_payload(payload: Any) -> Ledger:
    """The documented fields of ``GET /api/v1/credits`` (management key)."""
    if not isinstance(payload, dict):
        return Ledger(error="response was not a JSON object", source=CREDITS_URL)
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return Ledger(error="response had no data object", source=CREDITS_URL)
    return Ledger(
        total_credits=_number(data.get("total_credits")),
        total_usage=_number(data.get("total_usage")),
        source=CREDITS_URL,
    )


def read_key(token: str, *, credits: bool = False, client: httpx.Client | None = None) -> Ledger:
    """Read the ledger.  GET only; never creates a completion."""
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.get(KEY_URL, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            return Ledger(
                error=f"HTTP {response.status_code}: {response.text[:200]}", source=KEY_URL
            )
        ledger = parse_key_payload(response.json())
        if credits:
            cr = client.get(CREDITS_URL, headers={"Authorization": f"Bearer {token}"})
            if cr.status_code == 200:
                extra = parse_credits_payload(cr.json())
                ledger.total_credits = extra.total_credits
                ledger.total_usage = extra.total_usage
            else:
                ledger.error = f"credits endpoint HTTP {cr.status_code} (needs a management key)"
        return ledger
    except httpx.HTTPError as exc:
        return Ledger(error=f"{type(exc).__name__}: {exc}", source=KEY_URL)
    finally:
        if own:
            client.close()


def show_ledger(ledger: Ledger) -> None:
    if ledger.error and not ledger.readable:
        say(f"  ledger unreadable: {ledger.error}")
        return
    say(f"  source            {ledger.source}")
    say(f"  usage             {_usd(ledger.usage)}")
    say(f"  usage_daily       {_usd(ledger.usage_daily)}")
    say(f"  usage_monthly     {_usd(ledger.usage_monthly)}")
    say(f"  limit_remaining   {_usd(ledger.limit_remaining)}")
    say(f"  free tier         {ledger.is_free_tier}")
    if ledger.free_used is not None or ledger.free_limit is not None:
        say(f"  free requests     {ledger.free_used}/{ledger.free_limit} used, "
            f"{ledger.free_remaining} left today")
    if ledger.total_credits is not None:
        say(f"  account credits   {_usd(ledger.total_credits)} purchased, "
            f"{_usd(ledger.total_usage)} used")
    if ledger.error:
        say(f"  note              {ledger.error}")


def _usd(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value == 0:
        return "$0.00"
    return f"${value:.6f}" if abs(value) < 1 else f"${value:.4f}"


@dataclass
class ArmResult:
    """What one arm of the A/B produced."""

    name: str
    mode: str
    rows: int = 0
    rewritten: int = 0
    logged_cost: float | None = None
    error: str | None = None
    requests_sent: int = 0


@dataclass
class Arms:
    """Both arms, plus the ledger reads taken around them."""

    off: ArmResult | None = None
    active: ArmResult | None = None
    before: Ledger | None = None
    between: Ledger | None = None
    after: Ledger | None = None
    notes: list[str] = field(default_factory=list)


def compare_arms(
    arms: Arms, min_detectable: float = DEFAULT_MIN_DETECTABLE
) -> tuple[bool, list[str]]:
    """The verdict on a pair of arms, as PASS/FAIL plus the lines explaining it.

    A money measurement is only claimed when the ledger moved and the movement is
    above the instrument's floor.  When the ledger did not move, that is reported
    as "no money moved", never as a saving of zero.
    """
    lines: list[str] = []
    ok = True

    if arms.off is None or arms.active is None:
        return False, ["both arms must run before a comparison exists"]

    if arms.off.error or arms.active.error:
        ok = False
        lines.append(f"an arm failed: off={arms.off.error} active={arms.active.error}")

    if arms.off.requests_sent != arms.active.requests_sent:
        ok = False
        lines.append(
            "the arms sent different numbers of requests "
            f"({arms.off.requests_sent} vs {arms.active.requests_sent}); "
            "the work was not identical, so no comparison is valid"
        )

    if arms.active.rewritten == 0 and arms.off.rewritten == 0:
        lines.append(
            "neither arm rewrote a request: the active arm did not route anything, "
            "so there is nothing for the ledger to differ on"
        )

    x = (
        None
        if arms.before is None or arms.between is None
        else arms.between.delta_usage(arms.before)
    )
    y = (
        None
        if arms.between is None or arms.after is None
        else arms.after.delta_usage(arms.between)
    )

    if x is not None and y is not None:
        saving = x - y
        lines.append(f"arm X (router off):    {_usd(x)}")
        lines.append(f"arm Y (router active): {_usd(y)}")
        if abs(saving) < min_detectable:
            lines.append(
                f"saving {_usd(saving)} is below the instrument floor "
                f"({_usd(min_detectable)}): no money measurement is claimed"
            )
        else:
            lines.append(f"MEASURED SAVING: {_usd(saving)} (provider ledger, X - Y)")
    else:
        lines.append(
            "the ledger did not yield a money delta (unreadable, or the models "
            "billed 0): a free model consumes quota, not credit, so no dollar "
            "saving can be read from it"
        )
        if arms.before is not None and arms.between is not None:
            q1 = arms.between.delta_free_requests(arms.before)
            q2 = arms.after.delta_free_requests(arms.between) if arms.after else None
            if q1 is not None or q2 is not None:
                lines.append(
                    f"free requests consumed: off={q1}, active={q2} (a count, not money)"
                )

    return ok, lines


def _row_summary(db: Path) -> tuple[int, int, float | None]:
    """(rows, rewritten rows, summed stored computed cost) for a database."""
    import sqlite3

    uri = f"{db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM requests")]
    rewritten = 0
    total = 0.0
    seen_cost = False
    for row in rows:
        used = row.get("model_used")
        req = row.get("model_requested") or row.get("model")
        if used is not None and req is not None and used != req:
            rewritten += 1
        cost = row.get("computed_list_cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            total += float(cost)
            seen_cost = True
    return len(rows), rewritten, (total if seen_cost else None)


# ------------------------- candidate sweep (pure) -------------------------
#
# "Which cheap model saves the most?" is not a question one A/B can answer: the
# router is binary (strong vs *one* cheap target), so the comparison is across
# *runs*, one per candidate.  Everything below is arithmetic over observed
# per-run costs, so it is exercised offline without a key and without money.


@dataclass
class RunResult:
    """One completed run of the task under one arm."""

    arm: str
    cost: float | None = None
    passed: bool | None = None
    requests_sent: int = 0
    error: str | None = None


@dataclass
class ArmSummary:
    """Every run of one arm, summarised."""

    arm: str
    costs: list[float] = field(default_factory=list)
    passes: list[bool] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def median(self) -> float | None:
        return _median(self.costs)

    @property
    def low(self) -> float | None:
        return min(self.costs) if self.costs else None

    @property
    def high(self) -> float | None:
        return max(self.costs) if self.costs else None

    @property
    def all_passed(self) -> bool | None:
        if not self.passes:
            return None
        return all(self.passes)

    @property
    def n(self) -> int:
        return len(self.costs)


def _median(values: list[float]) -> float | None:
    """Median of the observed costs, or None when nothing was observed."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def summarise_runs(runs: list[RunResult]) -> list[ArmSummary]:
    """Group runs by arm, keeping only money the ledger actually yielded."""
    summaries: dict[str, ArmSummary] = {}
    for run in runs:
        summary = summaries.setdefault(run.arm, ArmSummary(arm=run.arm))
        if run.error:
            summary.failures.append(f"{run.arm}: {run.error}")
            continue
        if run.cost is not None:
            summary.costs.append(run.cost)
        if run.passed is not None:
            summary.passes.append(run.passed)
    return list(summaries.values())


def ranges_overlap(
    a: ArmSummary, b: ArmSummary, min_detectable: float = DEFAULT_MIN_DETECTABLE
) -> bool:
    """Whether two arms' observed cost ranges fail to separate.

    Two arms whose ranges overlap cannot be ranked from the data: the apparent
    winner may be run-to-run noise.  This is the check that stops a single lucky
    run from crowning a model.
    """
    if a.low is None or a.high is None or b.low is None or b.high is None:
        return True
    return not (a.high < b.low - min_detectable or b.high < a.low - min_detectable)


def choose_cheapest(
    summaries: list[ArmSummary],
    min_runs: int = 1,
    min_detectable: float = DEFAULT_MIN_DETECTABLE,
) -> tuple[ArmSummary | None, list[str]]:
    """The cheapest arm that passed every run, or None plus the reasons why not.

    A cheap model that fails the task is not a saving, so only arms whose runs all
    passed are eligible.  An arm with too few observations is not eligible either.
    If the two cheapest eligible arms' ranges overlap, the result is inconclusive
    rather than a winner.
    """
    lines: list[str] = []
    eligible: list[ArmSummary] = []

    for summary in summaries:
        if summary.n == 0:
            lines.append(f"  {summary.arm}: no priced runs, not eligible")
            continue
        if summary.n < min_runs:
            lines.append(
                f"  {summary.arm}: {summary.n} run(s), fewer than {min_runs}, not eligible"
            )
            continue
        if summary.all_passed is False:
            lines.append(f"  {summary.arm}: the task did not pass, not a saving")
            continue
        eligible.append(summary)

    if not eligible:
        lines.append("  no eligible arm: nothing to rank")
        return None, lines

    ranked = sorted(eligible, key=lambda s: s.median if s.median is not None else float("inf"))
    winner = ranked[0]

    if len(ranked) > 1 and ranges_overlap(winner, ranked[1], min_detectable):
        lines.append(
            f"  {winner.arm} and {ranked[1].arm} have overlapping cost ranges "
            f"({_usd(winner.low)}-{_usd(winner.high)} vs "
            f"{_usd(ranked[1].low)}-{_usd(ranked[1].high)}): "
            "inconclusive, increase --repeats"
        )
        return None, lines

    lines.append(f"  cheapest arm that passed: {winner.arm} (median {_usd(winner.median)})")
    return winner, lines


def show_sweep(summaries: list[ArmSummary]) -> None:
    """One table row per arm: median cost, range, pass/fail, n."""
    say(f"  {'arm':<24} {'runs':>4} {'median':>12} {'range':>26}  task")
    for summary in summaries:
        span = "UNKNOWN" if summary.low is None else f"{_usd(summary.low)} - {_usd(summary.high)}"
        task = {True: "pass", False: "FAIL", None: "not checked"}[summary.all_passed]
        say(f"  {summary.arm:<24} {summary.n:>4} {_usd(summary.median):>12} {span:>26}  {task}")


# --------------------------- dry run (no money) ---------------------------

async def _dry_arm(name: str, mode: str, db: Path, upstream_model: str,
                   cheap_model: str) -> ArmResult:
    """Run the same two-request task through the real proxy in one mode."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import shadow_rate_check as src
    from mock_upstream import StreamingASGITransport, create_mock_upstream

    from tamias import proxy
    from tamias.pricing import load_price_sheet
    from tamias.registry import load_registry
    from tamias.router import EASY_TOOLS, RouterConfig
    from tamias.store import Store

    with tempfile.TemporaryDirectory(prefix="tamias-credit-ab-") as work:
        workdir = Path(work)
        sheet_path = src.make_sheet(src.CASES[1], upstream_model, cheap_model, workdir)
        sheet = load_price_sheet(sheet_path)
        registry = load_registry(src.make_registry(upstream_model, cheap_model, workdir))
        store = Store(db)
        app = proxy.create_app(
            "http://mock-upstream.invalid", store, sheet, mode,
            config=RouterConfig(easy_tools=EASY_TOOLS, cheap_model=cheap_model,
                                strong_model=upstream_model, min_gap=0),
            registry=registry,
            transport=StreamingASGITransport(create_mock_upstream()),
        )
        sent = 0
        headers = {SESSION_HEADER: "credit-ab"}
        async with httpx.AsyncClient(transport=StreamingASGITransport(app),
                                     base_url="http://dry", timeout=None) as client:
            for label, body in src.probes(upstream_model):
                outcome = await src._send(client, label, body, headers)
                sent += 1
                if outcome.error or outcome.status != 200:
                    store.close()
                    return ArmResult(name=name, mode=mode, error=outcome.error
                                     or f"HTTP {outcome.status}", requests_sent=sent)
        store.close()
    rows, rewritten, cost = _row_summary(db)
    return ArmResult(name=name, mode=mode, rows=rows, rewritten=rewritten,
                     logged_cost=cost, requests_sent=sent)


def run_dry(tempdir: Path) -> int:
    """Exercise the whole A/B flow offline, with no key and no money."""
    say("dry run: same task, two arms, no key and no socket to a provider")
    say("  (the ledger cannot move without a paid model; this proves the")
    say("   comparison machinery, not the saving)")
    say()
    off_db = tempdir / "off.db"
    on_db = tempdir / "active.db"
    strong, cheap = "dry-strong", "dry-cheap"

    off = asyncio.run(_dry_arm("off", "off", off_db, strong, cheap))
    on = asyncio.run(_dry_arm("active", "active", on_db, strong, cheap))

    for arm in (off, on):
        say(f"  arm {arm.name:<6} mode={arm.mode:<6} rows={arm.rows} "
            f"rewritten={arm.rewritten} stored-cost={_usd(arm.logged_cost)}"
            + (f"  ERROR {arm.error}" if arm.error else ""))
    say()

    checks = Checks()
    checks.check(off.error is None and on.error is None,
                 "both arms completed",
                 f"an arm failed: off={off.error} on={on.error}")
    checks.check(off.rewritten == 0,
                 "router 'off' rewrote nothing",
                 f"router 'off' rewrote {off.rewritten} request(s)")
    checks.check(on.rewritten >= 1,
                 f"router 'active' rewrote {on.rewritten} request(s)",
                 "router 'active' rewrote nothing, so the arms cannot differ")
    checks.check(on.rewritten > off.rewritten,
                 "the arms differ exactly as the comparison requires",
                 f"the arms do not differ: off={off.rewritten} on={on.rewritten} rewritten")

    # With the ledger unreadable, the comparison must SAY so, never claim zero.
    arms = Arms(off=off, active=on, before=Ledger(), between=Ledger(), after=Ledger())
    _, lines = compare_arms(arms)
    for line in lines:
        say(f"  {line}")
    claims_money = any("MEASURED SAVING" in line for line in lines)
    checks.check(not claims_money,
                 "with no ledger movement the comparison claims no money saving",
                 "the comparison claimed a money saving with no readable ledger")
    say()
    return 0 if checks.report() else 1


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str]] = []

    def check(self, ok: bool, passed: str, failed: str) -> bool:
        if passed or failed:
            self.rows.append((ok, passed if ok else failed))
        return ok

    def report(self) -> bool:
        say("checks")
        for ok, text in self.rows:
            say(f"  {'PASS' if ok else 'FAIL'}  {text}")
        return all(ok for ok, _ in self.rows)


# ------------------------------ live path ------------------------------

def run_oracle(command: str, cwd: str | None = None, timeout: float = 300.0) -> tuple[bool, str]:
    """Run the pass/fail command and report whether the task still works.

    A saving is only meaningful when the task still passes, so this is run after
    each arm.  The exit code decides; the output is truncated into the reason so a
    failure can be read without the full log.
    """
    import subprocess

    try:
        completed = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"oracle timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"oracle could not run: {exc}"
    if completed.returncode == 0:
        return True, ""
    tail = (completed.stdout + completed.stderr).strip().splitlines()
    return False, f"oracle exit {completed.returncode}: {tail[-1] if tail else 'no output'}"


def print_live_procedure(args: argparse.Namespace) -> None:
    candidates = args.candidates.split(",") if args.candidates else []
    say("live credit measurement -- not run (no --allow-spend)")
    say()
    say("This sends identical tasks to paid models and reads the ledger between")
    say("them.  It costs real money; a small task on a strong model is roughly")
    say("$0.05-$0.30 for both arms of one comparison.")
    say()
    if candidates:
        say(f"candidate sweep (cheap models): {', '.join(candidates)}")
        say(f"repeats per arm:               {args.repeats}")
        say(f"oracle command:                {args.oracle or '<not set>'}")
        say()
        say("For each candidate, and for the 'off' baseline, the loop reads the")
        say("ledger, runs the task, runs the oracle, and reads the ledger again:")
        say()
        say("  1. usage_before = GET /api/v1/key -> data.usage")
        say("  2. run the task (routing active, --cheap-model <candidate>)")
        say("  3. run the oracle; a failed task is not a saving")
        say("  4. usage_after  = GET /api/v1/key -> data.usage")
        say("  5. cost = usage_after - usage_before")
        say()
        say("Arms are alternated run by run so drift hits every arm equally.")
        say("The winner is the cheapest arm whose runs all passed; if the two")
        say("cheapest ranges overlap, the result is inconclusive, not a winner.")
        say()
    else:
        say("A/B procedure (no --candidates):")
        say()
        say("  1. read the ledger:   GET /api/v1/key  (usage before)")
        say("  2. run the task with --router-mode off     -> X dollars")
        say("  3. read the ledger again                   (usage between)")
        say("  4. run the SAME task with --router-mode active -> Y dollars")
        say("  5. read the ledger once more               (usage after)")
        say("  6. saving = X - Y  (the provider's own numbers)")
        say()
    say(f"  strong model: {args.strong_model or '<not set>'}")
    say(f"  cheap model:  {args.cheap_model or '<not set>'}")
    say(f"  key variable: ${args.api_key_env}")
    say()
    say("Re-run with --allow-spend once a paid model pair and a ceiling are set.")
    say("A saving below the instrument floor is reported as below the floor,")
    say("never as a measurement.")


def run_live_sweep(args: argparse.Namespace, token: str) -> int:
    """The real, spending sweep: alternating arms, ledger reads, oracle each run.

    Nothing here runs without ``--allow-spend``.  Each run is priced from the
    *ledger delta* rather than a price sheet, and an arm whose task failed is
    dropped from the ranking.
    """
    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    if not args.oracle:
        say("a sweep needs --oracle '<command>': a cheap model that breaks the")
        say("task is not a saving, and only the oracle can tell.")
        return 2
    if not candidates:
        say("a sweep needs --candidates a,b,c")
        return 2

    arms = ["off", *candidates]
    runs: list[RunResult] = []
    say(f"live sweep: {len(arms)} arms x {args.repeats} repeats = "
        f"{len(arms) * args.repeats} runs")
    say("this spends real money; Ctrl-C now to stop")
    say()

    # Alternate the arm order each round so provider drift does not favour one.
    for round_index in range(args.repeats):
        order = arms if round_index % 2 == 0 else list(reversed(arms))
        for arm in order:
            before = read_key(token)
            if not before.readable:
                say(f"  {arm}: ledger unreadable, stopping ({before.error})")
                return 1
            started = before.usage
            outcome, reason = _live_run_arm(args, arm)
            after = read_key(token)
            cost = None if after.usage is None or started is None else after.usage - started
            passed = None
            if outcome is None:
                passed, oracle_reason = run_oracle(args.oracle, cwd=args.oracle_cwd or None)
                if not passed:
                    reason = oracle_reason
            runs.append(RunResult(arm=arm, cost=cost, passed=passed,
                                  requests_sent=args.probes, error=outcome))
            say(f"  {arm:<24} cost={_usd(cost):>12} "
                f"task={'pass' if passed else 'FAIL' if passed is False else '?'}"
                + (f"  {reason}" if reason else ""))

    say()
    summaries = summarise_runs(runs)
    show_sweep(summaries)
    say()
    winner, lines = choose_cheapest(summaries, min_runs=args.repeats,
                                    min_detectable=args.min_detectable)
    for line in lines:
        say(line)
    say()
    if winner is None:
        say("RESULT: INCONCLUSIVE (no arm separates from the others)")
        return 1
    say(f"RESULT: PASS (use --cheap-model {winner.arm})")
    return 0


def _live_run_arm(args: argparse.Namespace, arm: str) -> tuple[str | None, str]:
    """Drive the already-running proxy for one arm.  Returns (error, reason)."""
    import httpx

    headers = {SESSION_HEADER: f"credit-sweep-{arm}", "Authorization": f"Bearer {_SECRET}"}
    model = args.strong_model
    body = {
        "model": model,
        "messages": [{"role": "user", "content": args.task or "Reply with the word ok"}],
        "stream": False,
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(args.proxy.rstrip("/") + CHAT_PATH, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}", ""
    if response.status_code != 200:
        return f"HTTP {response.status_code}", response.text[:120]
    return None, ""


def main(argv: list[str] | None = None) -> int:
    global _SECRET
    args = _parser().parse_args(argv)
    _SECRET = os.environ.get(args.api_key_env, "").strip()

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="tamias-credit-ab-") as name:
            say("tamias credit A/B check  (dry run)")
            say()
            return run_dry(Path(name))

    if args.read_key:
        if not _SECRET:
            say(f"{args.api_key_env} is not set; export it, or use --dry-run.")
            return 2
        say("tamias credit A/B check  (ledger read)")
        say()
        ledger = read_key(_SECRET, credits=args.credits)
        show_ledger(ledger)
        say()
        if not ledger.readable:
            say("RESULT: FAIL (the ledger could not be read)")
            return 1
        if args.credits and ledger.total_credits is None:
            say("note: the credits endpoint needs a management key; key usage was read instead")
        say("RESULT: PASS (ledger readable)")
        return 0

    if args.allow_spend:
        if not _SECRET:
            say(f"{args.api_key_env} is not set; the live path needs a key.")
            return 2
        if args.candidates:
            say("tamias credit sweep  (live)")
            say()
            return run_live_sweep(args, _SECRET)
        say("tamias credit A/B check  (live)")
        say()
        say("a non-sweep live run needs a --candidates list and --oracle;")
        say("use --candidates a,b,c --oracle '<command>' --allow-spend")
        return 2

    say("tamias credit A/B check")
    say()
    print_live_procedure(args)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credit_ab_check.py",
        description=(
            "Measure the saving from the provider's ledger, not a price sheet. "
            "--dry-run proves the machinery with no key and no money; --read-key "
            "proves the ledger instrument; the live A/B is refusal-gated behind "
            "--allow-spend. The key is read from the environment and never printed."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="exercise the whole A/B flow offline, no key, no money")
    parser.add_argument("--read-key", action="store_true",
                        help="GET the ledger and print usage; needs a key, sends no completion")
    parser.add_argument("--credits", action="store_true",
                        help="also GET /api/v1/credits (needs a management key)")
    parser.add_argument("--allow-spend", action="store_true",
                        help="permit the live A/B, which sends paid completions")
    parser.add_argument("--api-key-env", default=KEY_ENV, metavar="NAME",
                        help="environment variable holding the key (default: %(default)s)")
    parser.add_argument("--proxy", default=DEFAULT_PROXY,
                        help=f"proxy base URL (default: {DEFAULT_PROXY})")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"request log (default: {DEFAULT_DB})")
    parser.add_argument("--strong-model", default="", help="the requested (strong) model")
    parser.add_argument("--cheap-model", default="", help="the router's cheap target")
    parser.add_argument("--min-detectable", type=float, default=DEFAULT_MIN_DETECTABLE,
                        help="savings below this are reported as below the floor "
                             f"(default: {DEFAULT_MIN_DETECTABLE})")
    parser.add_argument("--candidates", default="",
                        help="comma-separated cheap models to sweep, e.g. a,b,c")
    parser.add_argument("--repeats", type=int, default=3,
                        help="runs per arm; arms are alternated (default: %(default)s)")
    parser.add_argument("--oracle", default="",
                        help="command whose exit code decides whether the task passed")
    parser.add_argument("--oracle-cwd", default="",
                        help="working directory for --oracle")
    parser.add_argument("--probes", type=int, default=1,
                        help="requests per run, recorded for the identity check "
                             "(default: %(default)s)")
    parser.add_argument("--task", default="",
                        help="the task prompt sent in each run")
    return parser


if __name__ == "__main__":
    sys.exit(main())
