"""Generic tool-name matching: what class a name lands in, and how that routes.

The v1 router knew six exact tool names.  ``classify_tool`` normalises a name
to lower-case tokens (split on underscores, hyphens, dots and camelCase) and
matches them against the read/edit/shell sets, so an agent's ``read_file``,
``Bash`` or ``run_shell`` routes like its legacy equivalent.  A name that
touches two non-neutral sets, or any neutral token (todo, plan, task, think),
is UNKNOWN and never routes.
"""

from __future__ import annotations

import copy

import pytest

from tamias.router import (
    EASY_TOOLS,
    ERROR_MARKERS,
    GENERIC_ERROR_MARKERS,
    RouterConfig,
    classify_tool,
    decide,
)
from tamias.types import SessionState

CHEAP = "cheap-1"
STRONG = "strong-1"

CONFIG = RouterConfig(cheap_model=CHEAP, strong_model=STRONG, min_gap=3)

READ_NAMES = ["read_file", "list_directory", "search_in_files", "Read", "Grep", "Glob", "LS"]
SHELL_NAMES = ["run_shell", "Bash", "shell"]
EDIT_NAMES = [
    "create_directory",
    "write_file",
    "edit_file",
    "Edit",
    "MultiEdit",
    "apply_patch",
    "str_replace_editor",
]
NEUTRAL_NAMES = ["TodoWrite", "update_plan"]


def state(index: int = 5, model: str = STRONG) -> SessionState:
    return SessionState(session_id="s1", request_index=index, current_model=model)


def body_with_tool(tool_name: str, content: str, call_id: str = "call_1") -> dict:
    return {
        "model": STRONG,
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


# --- classification -----------------------------------------------------------

CLASSIFICATIONS = [
    ("run_shell", "shell"),
    ("read_file", "read"),
    ("list_directory", "read"),
    ("search_in_files", "read"),
    ("create_directory", "edit"),
    ("write_file", "edit"),
    ("edit_file", "edit"),
    ("Bash", "shell"),
    ("Read", "read"),
    ("Edit", "edit"),
    ("MultiEdit", "edit"),
    ("Grep", "read"),
    ("Glob", "read"),
    ("LS", "read"),
    ("apply_patch", "edit"),
    ("shell", "shell"),
    ("str_replace_editor", "edit"),
    ("TodoWrite", "unknown"),
]


@pytest.mark.parametrize(("name", "expected"), CLASSIFICATIONS)
def test_required_tool_names_classify(name: str, expected: str) -> None:
    assert classify_tool(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ReadFile", "read"),
        ("read-file", "read"),
        ("read.file", "read"),
        ("READ", "read"),
        ("searchInFiles", "read"),
        ("my_ls", "read"),
        ("WriteFile", "edit"),
        ("apply-patch", "edit"),
        ("strReplaceEditor", "edit"),
        ("RunShell", "shell"),
        ("run.shell", "shell"),
        ("sh", "shell"),
        ("update_plan", "unknown"),
        ("TaskList", "unknown"),
        ("think_step", "unknown"),
    ],
)
def test_normalisation_splits_case_and_separators(name: str, expected: str) -> None:
    assert classify_tool(name) == expected


@pytest.mark.parametrize("name", ["read_shell", "edit_grep", "bash_grep", "run_read"])
def test_two_non_neutral_sets_are_unknown(name: str) -> None:
    assert classify_tool(name) == "unknown"


@pytest.mark.parametrize("name", ["TodoWrite", "update_plan", "todo", "plan", "task", "think"])
def test_neutral_tokens_are_always_unknown(name: str) -> None:
    assert classify_tool(name) == "unknown"


@pytest.mark.parametrize("name", ["", "-", "...", "_", "123", "whisk"])
def test_unrecognised_names_are_unknown(name: str) -> None:
    assert classify_tool(name) == "unknown"


# --- routing ------------------------------------------------------------------


@pytest.mark.parametrize("name", READ_NAMES + SHELL_NAMES)
def test_generic_matcher_routes_read_and_shell_names(name: str) -> None:
    decision = decide(body_with_tool(name, "ok"), state(), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == CHEAP
    assert name in decision.reason


@pytest.mark.parametrize("name", EDIT_NAMES + NEUTRAL_NAMES)
def test_generic_matcher_keeps_edit_and_neutral_on_strong(name: str) -> None:
    decision = decide(body_with_tool(name, "ok"), state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None


def test_v1_easy_set_is_unchanged() -> None:
    assert EASY_TOOLS == frozenset({"shell", "bash", "read", "grep", "ls", "glob"})


@pytest.mark.parametrize("name", sorted(EASY_TOOLS))
def test_names_v1_classified_easy_still_switch(name: str) -> None:
    decision = decide(body_with_tool(name, "ok"), state(), CONFIG)
    assert decision.action == "SWITCH"
    assert decision.target_model == CHEAP


@pytest.mark.parametrize(
    "name", ["edit_file", "apply_patch", "write", "reasoning", "notebook", "assistant", "todo"]
)
def test_names_v1_left_unrouted_stay(name: str) -> None:
    decision = decide(body_with_tool(name, "ok"), state(), CONFIG)
    assert decision.action == "STAY"
    assert decision.target_model is None


def test_matcher_does_not_mutate_the_body() -> None:
    body = body_with_tool("run_shell", "ok")
    before = copy.deepcopy(body)
    first = decide(body, state(), CONFIG)
    second = decide(body, state(), CONFIG)
    assert body == before
    assert first == second


# --- profiles -----------------------------------------------------------------


def test_profile_defaults_to_generic() -> None:
    assert RouterConfig().profile == "generic"


def test_legacy_profile_is_accepted() -> None:
    assert RouterConfig(profile="legacy").profile == "legacy"


@pytest.mark.parametrize("bad", ["GENERIC", "Legacy", "strict", "", "v1"])
def test_unknown_profile_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="profile"):
        RouterConfig(profile=bad)


def config(profile: str) -> RouterConfig:
    return RouterConfig(profile=profile, cheap_model=CHEAP, strong_model=STRONG, min_gap=3)


@pytest.mark.parametrize("profile", ["generic", "legacy"])
def test_shared_rules_hold_under_both_profiles(profile: str) -> None:
    cfg = config(profile)

    user_turn = {"model": STRONG, "messages": [{"role": "user", "content": "add a test"}]}
    assert decide(user_turn, state(), cfg).action == "STAY"

    error = decide(body_with_tool("read", "Traceback (most recent call last):"), state(), cfg)
    assert error.action == "STAY"
    assert error.reason == "tool error: needs strong model"

    assert decide(body_with_tool("read", "ok"), state(index=2), cfg).action == "STAY"
    assert decide(body_with_tool("read", "ok"), state(index=3), cfg).action == "SWITCH"

    hysteresis = decide(body_with_tool("read", "ok"), state(index=9, model=CHEAP), cfg)
    assert hysteresis.action == "STAY"
    assert "hysteresis" in hysteresis.reason

    assert decide(body_with_tool("edit_file", "wrote 1 file"), state(), cfg).action == "STAY"


@pytest.mark.parametrize("name", ["Bash", "read_file", "run_shell", "search_in_files", "Edit"])
def test_legacy_profile_only_knows_exact_names(name: str) -> None:
    assert decide(body_with_tool(name, "ok"), state(), config("legacy")).action == "STAY"


def test_legacy_profile_routes_its_six_names() -> None:
    for name in sorted(EASY_TOOLS):
        decision = decide(body_with_tool(name, "ok"), state(), config("legacy"))
        assert decision.action == "SWITCH", name
        assert decision.target_model == CHEAP


def test_generic_profile_recognises_names_from_any_agent() -> None:
    for name in ("run_shell", "read_file", "Bash", "search_in_files"):
        decision = decide(body_with_tool(name, "ok"), state(), config("generic"))
        assert decision.action == "SWITCH", name
        assert decision.target_model == CHEAP


# --- shell output gating ------------------------------------------------------


def test_marker_defaults_differ_by_profile() -> None:
    assert RouterConfig().error_markers == GENERIC_ERROR_MARKERS
    assert RouterConfig(profile="legacy").error_markers == ERROR_MARKERS
    assert set(ERROR_MARKERS) < set(GENERIC_ERROR_MARKERS)
    assert "Error:" not in GENERIC_ERROR_MARKERS


@pytest.mark.parametrize("marker", GENERIC_ERROR_MARKERS)
def test_default_error_markers_block_every_tool(marker: str) -> None:
    for name in ("shell", "Bash", "read_file", "read"):
        decision = decide(body_with_tool(name, f"output\n{marker} detail"), state(), CONFIG)
        assert decision.action == "STAY", (name, marker)
        assert decision.reason == "tool error: needs strong model"


def test_legacy_profile_keeps_the_v1_markers() -> None:
    legacy = config("legacy")
    assert decide(body_with_tool("shell", "Exception: boom"), state(), legacy).action == "SWITCH"
    assert decide(body_with_tool("shell", "Exception: boom"), state(), CONFIG).action == "STAY"


def test_capital_error_colon_is_opt_in_not_default() -> None:
    assert decide(body_with_tool("shell", "Error: nope"), state(), CONFIG).action == "SWITCH"
    opt_in = RouterConfig(
        cheap_model=CHEAP,
        strong_model=STRONG,
        min_gap=3,
        error_markers=(*GENERIC_ERROR_MARKERS, "Error:"),
    )
    assert decide(body_with_tool("shell", "Error: nope"), state(), opt_in).action == "STAY"


def gated(limit: int) -> RouterConfig:
    return RouterConfig(cheap_model=CHEAP, strong_model=STRONG, min_gap=3, big_output_chars=limit)


def test_clean_shell_result_under_the_limit_switches() -> None:
    decision = decide(body_with_tool("run_shell", "ok"), state(), gated(100))
    assert decision.action == "SWITCH"
    assert decision.target_model == CHEAP


@pytest.mark.parametrize("size", [100, 5_000])
def test_shell_result_at_or_over_the_limit_stays(size: int) -> None:
    decision = decide(body_with_tool("Bash", "x" * size), state(), gated(100))
    assert decision.action == "STAY"
    assert decision.target_model is None
    assert "too large" in decision.reason


def test_size_limit_does_not_gate_non_shell_results() -> None:
    decision = decide(body_with_tool("read_file", "x" * 5_000), state(), gated(10))
    assert decision.action == "SWITCH"


def test_size_limit_only_applies_when_configured() -> None:
    assert RouterConfig().big_output_chars is None
    decision = decide(body_with_tool("shell", "x" * 100_000), state(), CONFIG)
    assert decision.action == "SWITCH"


def test_size_limit_is_honoured_on_the_legacy_profile_too() -> None:
    cfg = RouterConfig(profile="legacy", cheap_model=CHEAP, min_gap=3, big_output_chars=10)
    assert decide(body_with_tool("shell", "x" * 500), state(), cfg).action == "STAY"


def test_error_marker_rule_wins_over_the_size_gate() -> None:
    decision = decide(
        body_with_tool("Bash", "Traceback (most recent call last):"), state(), gated(10)
    )
    assert decision.action == "STAY"
    assert decision.reason == "tool error: needs strong model"


