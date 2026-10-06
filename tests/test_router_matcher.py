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

from tamias.router import EASY_TOOLS, RouterConfig, classify_tool, decide
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
