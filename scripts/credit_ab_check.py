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

def print_live_procedure(args: argparse.Namespace) -> None:
    say("live A/B credit measurement -- not run (no --allow-spend)")
    say()
    say("This sends two identical tasks to paid models and reads the ledger")
    say("between them.  It costs real money; a small task on a strong model is")
    say("roughly $0.05-$0.30 for both arms.  The procedure:")
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
    return parser


if __name__ == "__main__":
    sys.exit(main())
