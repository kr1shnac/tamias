"""Tests for the rule-based router."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from tamias.router import RouterConfig, SessionState, decide  # noqa: E402

CONFIG = RouterConfig(
    easy_tools={"shell", "bash", "read", "grep", "ls", "glob"},
    cheap_model="cheap-1",
    strong_model="strong-1",
    min_gap=3,
)

TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cached_input_tokens": 0,
    "cache_write_tokens": 0,
}


def state(index: int = 5, model: str = "strong-1", session: str = "s1") -> SessionState:
    return SessionState(session_id=session, request_index=index, current_model=model)


def body_with_tool(tool_name: str, content: str, call_id: str = "call_1") -> dict:
    return {
        "model": "strong-1",
        "messages": [
            {"role": "user", "content": "run the tests"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": content},
        ],
    }


def test_user_turn_stays_for_planning() -> None:
    body = {"model": "strong-1", "messages": [{"role": "user", "content": "add a test"}]}
    decision = decide(body, state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None
    assert decision.reason == "user turn: planning"


def test_user_turn_stays_even_with_tools_in_history() -> None:
    body = body_with_tool("shell", "ok")
    body["messages"].append({"role": "user", "content": "now refactor it"})
    decision = decide(body, state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.reason == "user turn: planning"


@pytest.mark.parametrize(
    "content",
    [
        "Traceback (most recent call last):\n  KeyError: 'x'",
        "FAILED tests/test_cli.py::test_one",
        "error: command not found",
        "ok\nthen FAILED at the end",
    ],
)
def test_tool_error_markers_stay_on_strong_model(content: str) -> None:
    decision = decide(body_with_tool("shell", content), state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None
    assert decision.reason == "tool error: needs strong model"


def test_tool_error_rule_precedes_easy_tool_rule() -> None:
    # "read" is an easy tool, but the output is an error, so rule 2 wins.
    decision = decide(body_with_tool("read", "error: nope"), state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.reason == "tool error: needs strong model"


def test_error_markers_are_case_sensitive() -> None:
    # Only the exact markers count; "Error:" must not trip the rule.
    decision = decide(body_with_tool("shell", "Error: nope"), state(), CONFIG)
    assert decision.action == "SWITCH"


def test_easy_tool_switches_to_cheap_model_after_gap() -> None:
    decision = decide(body_with_tool("grep", "3 matches"), state(index=5), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-1"
    assert "grep" in decision.reason


def test_tool_name_resolved_from_preceding_assistant_tool_calls() -> None:
    body = body_with_tool("bash", "ok", call_id="call_x")
    # The tool message itself carries no name: only the tool_call_id does.
    assert "name" not in body["messages"][-1]
    decision = decide(body, state(), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-1"
    assert "bash" in decision.reason


def test_tool_name_falls_back_to_message_name() -> None:
    body = body_with_tool("glob", "ok")
    del body["messages"][1]  # no preceding assistant message at all
    body["messages"][-1]["name"] = "glob"
    decision = decide(body, state(), CONFIG)
    assert decision.action == "SWITCH"
    assert "glob" in decision.reason


def test_unmatched_tool_call_id_does_not_match_other_calls() -> None:
    body = body_with_tool("read", "ok", call_id="call_1")
    body["messages"][-1]["tool_call_id"] = "call_missing"
    body["messages"][-1]["name"] = "read"
    decision = decide(body, state(), CONFIG)
    # The id does not match, so the name field is the fallback and it still routes.
    assert decision.action == "SWITCH"
    assert "read" in decision.reason


def test_hysteresis_holds_the_chosen_target_in_place() -> None:
    """Once the session is on the target, repeat easy turns do not churn.

    The hold is a SWITCH to the same model: STAY would forward the caller's
    model and drop the session off the target every other turn.
    """
    body = body_with_tool("ls", "a.txt")
    on_cheap = state(index=99, model="cheap-1")
    decision = decide(body, on_cheap, CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-1"
    assert decision.reason == "already on cheap-1: held"


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, "STAY"), (2, "STAY"), (3, "SWITCH"), (4, "SWITCH")],
)
def test_min_gap_boundary(index: int, expected: str) -> None:
    decision = decide(body_with_tool("shell", "ok"), state(index=index), CONFIG)
    assert decision.action == expected


def test_a_held_session_stays_on_the_target() -> None:
    """A held turn is a switch, not a stay, at any min_gap.

    Regression: the hold used to emit STAY, so a session bounced back to the
    caller's model on every other easy turn -- alternating cheap/strong,
    halving the saving and rebuilding the prompt cache on each turn.
    """
    config = RouterConfig(cheap_model="cheap-1", strong_model="strong-1", min_gap=3)
    decision = decide(body_with_tool("read", "x"), state(index=4, model="cheap-1"), config)
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-1"
    assert "held" in decision.reason


def test_held_sessions_do_not_ping_pong() -> None:
    """The whole session, driven the way proxy.py drives it.

    ``plan`` asks decide with the model the client named; ``advance`` records
    the model that actually ran. A client that keeps naming its strong model
    must still end up holding the cheap one for the rest of the run, because
    ``min_gap`` limits switches rather than holds.
    """
    from tamias.router import applied_target

    config = RouterConfig(cheap_model="cheap-1", strong_model="strong-1", min_gap=3)
    index, current = 0, "strong-1"
    ran: list[str] = []
    for _ in range(8):
        decision = decide(
            body_with_tool("read", "x"),
            SessionState(session_id="s1", request_index=index, current_model=current),
            config,
        )
        routed = applied_target(
            decision, requested="strong-1", enabled=decision.action == "SWITCH"
        )
        current = routed or "strong-1"
        ran.append(current)
        index += 1
    # Three requests of gap before the first downgrade, then held for the rest
    # of the run. Before the fix this was strong/cheap/strong/cheap...
    assert ran == ["strong-1"] * 3 + ["cheap-1"] * 5, ran


def test_hard_tool_stays_on_strong_model() -> None:
    decision = decide(body_with_tool("edit_file", "wrote 1 file"), state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None


def test_default_rule_stays_for_assistant_turn() -> None:
    body = {"model": "strong-1", "messages": [{"role": "assistant", "content": "done"}]}
    decision = decide(body, state(), CONFIG)
    assert decision.action == "STAY"
    assert "strong-1" in decision.reason


def test_empty_messages_default_stay() -> None:
    decision = decide({"model": "strong-1", "messages": []}, state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None


def test_decide_does_not_mutate_body() -> None:
    body = body_with_tool("read", "file contents")
    before = copy.deepcopy(body)
    first = decide(body, state(), CONFIG)
    second = decide(body, state(), CONFIG)
    assert body == before
    assert first == second


def test_decide_does_not_mutate_body_on_every_branch() -> None:
    bodies = [
        {"model": "strong-1", "messages": [{"role": "user", "content": "hi"}]},
        body_with_tool("shell", "Traceback (most recent call last):"),
        body_with_tool("read", "contents"),
        {"model": "strong-1", "messages": [{"role": "assistant", "content": "ok"}]},
    ]
    for body in bodies:
        before = copy.deepcopy(body)
        for index in (0, 1, 3, 10):
            for model in ("strong-1", "cheap-1"):
                decide(body, state(index=index, model=model), CONFIG)
        assert body == before


def test_config_defaults() -> None:
    default = RouterConfig()
    assert default.easy_tools == frozenset({"shell", "bash", "read", "grep", "ls", "glob"})
    assert default.min_gap == 3


def test_positional_config_order_matches_contract() -> None:
    config = RouterConfig({"grep"}, "c", "s", 5)
    assert config.easy_tools == frozenset({"grep"})
    assert config.cheap_model == "c"
    assert config.strong_model == "s"
    assert config.min_gap == 5


def test_decide_uses_default_config_when_omitted() -> None:
    body = body_with_tool("read", "contents")
    assert decide(body, state(), RouterConfig(cheap_model="cheap-1", min_gap=1)).action == (
        "SWITCH"
    )


# ------------------------------------------------------------- cheap model pool ---

POOL = RouterConfig(
    easy_tools={"shell", "bash", "read", "grep", "ls", "glob"},
    cheap_pool=("cheap-a", "cheap-b", "cheap-c"),
    strong_model="strong-1",
    min_gap=0,
)


def test_pool_switch_picks_a_member_of_the_pool() -> None:
    decision = decide(body_with_tool("read", "contents"), state(index=0), POOL)
    assert decision.action == "SWITCH"
    assert decision.target_model in POOL.pool


def test_pool_target_is_stable_for_a_session_across_request_indexes() -> None:
    targets = {
        decide(body_with_tool("read", "contents"), state(index=index), POOL).target_model
        for index in (0, 1, 5, 50)
    }
    assert len(targets) == 1


def test_pool_spreads_different_sessions_across_models() -> None:
    targets = {
        decide(
            body_with_tool("read", "contents"), state(index=0, session=f"session-{n}"), POOL
        ).target_model
        for n in range(20)
    }
    assert len(targets) > 1


def test_pool_never_chooses_the_requested_model() -> None:
    body = body_with_tool("read", "contents")
    body["model"] = "cheap-a"
    decision = decide(body, state(index=0), POOL)
    assert decision.action == "SWITCH"
    assert decision.target_model in {"cheap-b", "cheap-c"}


def test_pool_with_only_the_requested_model_stays() -> None:
    config = RouterConfig(cheap_pool=("strong-1",), min_gap=0)
    decision = decide(body_with_tool("read", "contents"), state(index=0), config)
    assert decision.action == "STAY"
    assert decision.reason == "no cheaper pool target than the requested model"


def test_pool_hysteresis_holds_the_chosen_target_in_place() -> None:
    """Once the session is on the target, repeat easy turns do not churn."""
    config = RouterConfig(
        cheap_pool=("cheap-a", "cheap-b"), strong_model="strong-1", min_gap=3
    )
    decision = decide(
        body_with_tool("read", "contents"),
        state(index=10, model="cheap-a"),
        config,
        ranked=("cheap-a", "cheap-b"),
    )
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-a"
    assert decision.reason == "already on cheap-a: held"


def test_pool_moves_off_a_dearer_member_to_the_cheaper_target() -> None:
    """Being on *a* pool member is not the same as being on the chosen one."""
    config = RouterConfig(
        cheap_pool=("cheap-a", "cheap-b"), strong_model="strong-1", min_gap=0
    )
    decision = decide(
        body_with_tool("read", "contents"),
        state(index=10, model="cheap-b"),
        config,
        ranked=("cheap-a", "cheap-b"),
        costs={"cheap-a": 0.1, "cheap-b": 1.0},
    )
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-a"


def test_pool_refuses_a_target_that_is_not_cheaper() -> None:
    config = RouterConfig(cheap_pool=("cheap-a",), strong_model="strong-1", min_gap=0)
    decision = decide(
        body_with_tool("read", "contents"),
        state(index=10, model="strong-1"),
        config,
        ranked=("cheap-a",),
        costs={"cheap-a": 2.0, "strong-1": 2.0},
    )
    assert decision.action == "STAY"
    assert decision.target_model is None
    assert decision.reason == "no cheaper pool target than the requested model"


def test_pool_switches_when_the_target_is_cheaper() -> None:
    config = RouterConfig(cheap_pool=("cheap-a",), strong_model="strong-1", min_gap=0)
    decision = decide(
        body_with_tool("read", "contents"),
        state(index=0, model="strong-1"),
        config,
        ranked=("cheap-a",),
        costs={"cheap-a": 0.0, "strong-1": 10.0},
    )
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-a"


def test_single_model_configuration_still_targets_cheap_model() -> None:
    decision = decide(body_with_tool("read", "contents"), state(index=5), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == "cheap-1"


def test_pool_primary_defaults_to_first_member() -> None:
    config = RouterConfig(cheap_pool=("cheap-a", "cheap-b"))
    assert config.cheap_model == "cheap-a"
    assert config.pool == ("cheap-a", "cheap-b")


def test_empty_pool_falls_back_to_single_cheap_model() -> None:
    config = RouterConfig(cheap_model="cheap-1")
    assert config.pool == ("cheap-1",)
