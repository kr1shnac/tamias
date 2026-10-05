"""Hardening tests for the router: purity, the rules, and its worst-case cost.

``decide`` is on the hot path of every request and its decision is written to the
request log, so three things have to hold no matter what arrives: it never
mutates the body it is handed, it always returns a decision whose parts agree
with each other (a STAY has no target, a SWITCH targets the cheap model), and it
never quotes the conversation -- a reason that echoed a prompt would put prompt
text into the log and into every report drawn from it.

Those invariants are asserted over hundreds of fixed-seed random conversations
rather than a handful of examples, alongside the hand-picked shapes that random
generation rarely reaches and the 2,000-message history that bounds its cost.
Nothing here fixes ``src/``; a test that exposes a real defect is marked
``xfail(strict=True)`` and carries a BUG id that also appears in
``docs/audit/hardening-findings.md``.
"""

from __future__ import annotations

import copy
import random
import time
from typing import Any

import pytest

from tamias.router import EASY_TOOLS, RouterConfig, decide
from tamias.types import Decision, SessionState

CHEAP = "cheap-model"
STRONG = "strong-model"
MIN_GAP = 3
CONFIG = RouterConfig(
    easy_tools=EASY_TOOLS, cheap_model=CHEAP, strong_model=STRONG, min_gap=MIN_GAP
)

RANDOM_CASES = 300
RANDOM_SEED = 42
TIME_BUDGET_S = 1.0
BIG_HISTORY = 2_000

# Distinctive enough that finding one inside a decision reason can only mean the
# conversation was copied into it.
MARKER = "MARKER-must-not-appear-in-a-decision"


# --- shapes that must not crash ----------------------------------------------


@pytest.mark.parametrize(
    "last",
    [
        pytest.param(
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [{"type": "text", "text": "Traceback"}],
            },
            id="tool_error_blocks",
        ),
        pytest.param({"role": "tool", "content": None}, id="tool_no_content"),
        pytest.param(
            {"role": "tool", "content": "just a string", "name": "read"}, id="tool_string_named"
        ),
        pytest.param({"role": "tool", "content": "ok", "tool_call_id": 7}, id="tool_numeric_id"),
        pytest.param(
            {"role": "assistant", "content": None, "tool_calls": None}, id="assistant_null_calls"
        ),
        pytest.param(
            {"role": "assistant", "content": None, "tool_calls": []}, id="assistant_empty_calls"
        ),
        pytest.param(
            {"role": "assistant", "content": [{"type": "image"}]}, id="assistant_non_text_block"
        ),
        pytest.param(
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}, id="user_block_content"
        ),
        pytest.param({"role": "user", "content": 42}, id="user_numeric_content"),
        pytest.param({"role": "system", "content": "sys"}, id="system_tail"),
        pytest.param({"role": "nonsense", "content": "x"}, id="unknown_role"),
        pytest.param({}, id="empty_message"),
    ],
)
def test_odd_message_shapes_still_produce_a_decision(last: dict[str, Any]) -> None:
    """Whatever the last message looks like, ``decide`` answers consistently."""
    body = {"model": STRONG, "messages": [{"role": "user", "content": "hi"}, last]}
    before = copy.deepcopy(body)

    decision = decide(body, SessionState("s", 5, STRONG), CONFIG)

    assert decision.action in ("STAY", "SWITCH")
    assert isinstance(decision.reason, str) and decision.reason
    assert body == before, "decide mutated the body it was given"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"model": STRONG}, id="no_messages"),
        pytest.param({"model": STRONG, "messages": "not a list"}, id="messages_not_a_list"),
        pytest.param({"model": STRONG, "messages": {}}, id="messages_is_a_dict"),
        pytest.param({"model": STRONG, "messages": []}, id="messages_empty"),
        pytest.param({"model": STRONG, "messages": [None, 7, "x"]}, id="messages_not_dicts"),
        pytest.param({"messages": [{"role": "user", "content": "hi"}]}, id="no_model"),
        pytest.param({}, id="empty_body"),
    ],
)
def test_unusable_bodies_still_produce_a_decision(body: dict[str, Any]) -> None:
    """A body the router cannot read is still a request that needs an answer."""
    before = copy.deepcopy(body)

    decision = decide(body, SessionState("s", 0, ""), CONFIG)

    assert decision.action in ("STAY", "SWITCH")
    assert body == before


def test_a_trailing_user_turn_always_stays() -> None:
    """The user is talking, so the model in use stays.

    Switching mid-plan would hand a fresh conversation to a model that has not
    seen the plan, which is the failure mode this rule exists to prevent.
    """
    body = {"model": STRONG, "messages": [{"role": "user", "content": f"deploy {MARKER}?"}]}
    for index in (0, 1, 2, 3, 10, 999):
        assert decide(body, SessionState("s", index, CHEAP), CONFIG).action == "STAY"


@pytest.mark.parametrize(
    "text",
    ["Traceback (most recent call last)", "FAILED: tests", "error: could not connect"],
)
def test_a_failing_tool_turn_always_stays(text: str) -> None:
    """An error from an easy tool is exactly when the strong model is needed."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": text},
    ]
    decision = decide(
        {"model": STRONG, "messages": messages}, SessionState("s", 50, STRONG), CONFIG
    )
    assert decision.action == "STAY", decision.reason


def test_hysteresis_holds_off_switching_once_on_the_cheap_model() -> None:
    """While the session is already on the cheap model, stay there.

    Without this the cheap model would flap on every easy tool turn, and the
    min-gap guarantee the flag promises would mean nothing.
    """
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "all good"},
    ]
    body = {"model": STRONG, "messages": messages}
    for index in range(MIN_GAP + 3):
        decision = decide(body, SessionState("s", index, CHEAP), CONFIG)
        assert decision.action == "STAY", f"flapped to {decision.target_model} at index {index}"


def test_switching_waits_for_the_configured_gap() -> None:
    """Before ``min_gap`` requests have passed, an easy tool turn keeps the model."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "all good"},
    ]
    body = {"model": STRONG, "messages": messages}

    for index in range(MIN_GAP):
        assert decide(body, SessionState("s", index, STRONG), CONFIG).action == "STAY"

    decision = decide(body, SessionState("s", MIN_GAP, STRONG), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == CHEAP


# --- invariants over random conversations -------------------------------------


def _random_body(rng: random.Random) -> dict[str, Any]:
    """A random conversation that leans on the shapes a real agent produces."""
    messages: list[Any] = []
    for _ in range(rng.randint(0, 4)):
        shape = rng.random()
        if shape < 0.35:
            messages.append({"role": "user", "content": f"{MARKER} {rng.randint(0, 99)}"})
        elif shape < 0.6:
            name = rng.choice(["read", "grep", "shell", "reasoning", "apply_patch"])
            messages.append(
                {
                    "role": "assistant",
                    "content": rng.choice(
                        [None, "working on it", [{"type": "text", "text": "plan"}]]
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{rng.randint(0, 5)}",
                            "type": "function",
                            "function": {"name": name},
                        }
                    ],
                }
            )
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{rng.randint(0, 5)}",
                    "content": rng.choice(
                        [None, "done", f"{MARKER} error: broken", "Traceback: boom", 17]
                    ),
                    "name": rng.choice([None, "read", "shell"]),
                }
            )
    return {"model": rng.choice([STRONG, CHEAP, "unknown-model", None]), "messages": messages}


def _assert_invariants(body: dict[str, Any], state: SessionState) -> Decision:
    """Every promise ``decide`` makes, checked once."""
    before = copy.deepcopy(body)

    decision = decide(body, state, CONFIG)
    repeated = decide(body, state, CONFIG)

    assert isinstance(decision, Decision), type(decision)
    assert decision.action in ("STAY", "SWITCH"), decision.action
    assert isinstance(decision.reason, str) and decision.reason
    assert body == before, f"decide mutated the body: {body}"
    assert repeated == decision, "decide is not deterministic"

    # The parts of a decision have to agree with each other.
    if decision.action == "STAY":
        assert decision.target_model is None, decision
    else:
        assert decision.target_model == CHEAP, decision

    # A reason is written to the request log and to every report drawn from it,
    # so it may name a rule and a tool but never the conversation.
    assert MARKER not in decision.reason, decision.reason

    # Rule 1 and the hysteresis rule, as invariants rather than as examples.
    last = body.get("messages")[-1] if body.get("messages") else None
    if isinstance(last, dict) and last.get("role") == "user":
        assert decision.action == "STAY", decision
    if state.current_model == CHEAP:
        assert decision.action == "STAY", f"flapped away from the cheap model: {decision}"
    return decision


def test_invariants_hold_over_random_conversations() -> None:
    """Purity, self-consistency and silence about the content, 300 times over."""
    rng = random.Random(RANDOM_SEED)
    switched = 0

    for _ in range(RANDOM_CASES):
        body = _random_body(rng)
        state = SessionState(
            "session",
            rng.randint(0, 10),
            rng.choice([STRONG, CHEAP, ""]),
        )
        if _assert_invariants(body, state).action == "SWITCH":
            switched += 1

    # A run where nothing ever switches would pass every invariant above while
    # testing nothing, so the generator has to be shown to reach the SWITCH path.
    assert switched >= 10, f"only {switched} of {RANDOM_CASES} random conversations switched"


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(RouterConfig(cheap_model="", strong_model=STRONG), id="no_cheap_model"),
        pytest.param(
            RouterConfig(cheap_model=CHEAP, strong_model=CHEAP), id="same_model_both_ways"
        ),
        pytest.param(
            RouterConfig(cheap_model=CHEAP, strong_model=STRONG, min_gap=0), id="zero_gap"
        ),
        pytest.param(
            RouterConfig(cheap_model=CHEAP, strong_model=STRONG, min_gap=10_000), id="huge_gap"
        ),
        pytest.param(RouterConfig(easy_tools=frozenset()), id="no_easy_tools"),
    ],
)
def test_invariants_hold_for_every_config(config: RouterConfig) -> None:
    """The same promises hold when the operator has configured it differently.

    A cheap model that is also the strong one, no cheap model at all, and a
    min-gap of zero are all reachable from the command line.  With no cheap model
    a SWITCH can still come back -- carrying an empty target, which the proxy
    refuses to act on and warns about at startup -- so it is the target value
    that has to match the configuration, not the action.
    """
    rng = random.Random(7)
    for _ in range(40):
        body = _random_body(rng)
        state = SessionState("session", rng.randint(0, 20), rng.choice([STRONG, CHEAP]))
        before = copy.deepcopy(body)

        decision = decide(body, state, config)

        assert decision.action in ("STAY", "SWITCH")
        assert body == before
        assert MARKER not in decision.reason
        if decision.action == "STAY":
            assert decision.target_model is None, decision
        else:
            assert decision.target_model == config.cheap_model, decision


# --- cost ---------------------------------------------------------------------


def test_two_thousand_messages_stay_within_the_budget() -> None:
    """A long conversation must not slow the proxy down.

    The history is built to be the worst case: the tool message at the end can
    only be attributed by scanning backwards to the assistant message that made
    the call, which sits at the very front, so nothing can short-circuit the
    walk.  Everything the router does is per request, so a quadratic scan here
    would be felt on every single call.
    """
    messages: list[Any] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_first", "type": "function", "function": {"name": "read"}}],
        }
    ]
    for index in range(BIG_HISTORY - 2):
        messages.append({"role": "user", "content": f"chatter {index}"})
    messages.append({"role": "tool", "tool_call_id": "call_first", "content": "done"})
    body = {"model": STRONG, "messages": messages}
    assert len(messages) == BIG_HISTORY

    started = time.monotonic()
    decision = decide(body, SessionState("s", 99, STRONG), CONFIG)
    elapsed = time.monotonic() - started

    assert decision.action == "SWITCH" and decision.target_model == CHEAP
    assert elapsed < TIME_BUDGET_S, f"decide took {elapsed:.3f}s over {BIG_HISTORY} messages"
