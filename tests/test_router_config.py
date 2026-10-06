"""Loading router tunables from TOML: strict keys, plain data, no execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.router import EASY_TOOLS, ERROR_MARKERS, RouterConfig, decide
from tamias.router_config import load_router_config
from tamias.types import SessionState

CHEAP = "cheap-1"
STRONG = "strong-1"


def state(index: int = 5, model: str = STRONG) -> SessionState:
    return SessionState(session_id="s1", request_index=index, current_model=model)


def body_with_tool(tool_name: str, content: str) -> dict:
    return {
        "model": STRONG,
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": content},
        ],
    }


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "router.toml"
    path.write_text(text, encoding="utf-8")
    return path


VALID_FILE = """\
easy_tools = ["whisk", "notebook"]
edit_tools = ["format_code"]
shell_tools = ["npm_run"]
error_markers = ["Traceback", "FAILED", "error:", "Exception", "non-zero exit code", "Error:"]
big_output_chars = 4096
min_gap = 7
"""


def test_valid_file_loads_every_key(tmp_path: Path) -> None:
    config = load_router_config(write(tmp_path, VALID_FILE))

    assert config.easy_tools == EASY_TOOLS | {"whisk", "notebook"}
    assert config.edit_tools == frozenset({"format_code"})
    assert config.shell_tools == frozenset({"npm_run"})
    assert config.error_markers == (
        "Traceback",
        "FAILED",
        "error:",
        "Exception",
        "non-zero exit code",
        "Error:",
    )
    assert config.big_output_chars == 4096
    assert config.min_gap == 7
    assert config.profile == "generic"
    assert config.cheap_model == ""
    assert config.strong_model == ""


def test_empty_file_loads_the_defaults(tmp_path: Path) -> None:
    assert load_router_config(write(tmp_path, "")) == RouterConfig()


def test_legacy_default_markers_are_the_v1_three(tmp_path: Path) -> None:
    assert RouterConfig(profile="legacy").error_markers == ERROR_MARKERS
    assert load_router_config(write(tmp_path, "")).error_markers is not None


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_router_config(tmp_path / "absent.toml")


def test_invalid_toml_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid TOML"):
        load_router_config(write(tmp_path, 'easy_tools = ["unterminated]\n'))


@pytest.mark.parametrize(
    "text",
    [
        'profile = "legacy"\n',
        'cheap_model = "cheap"\n',
        'strong_model = "strong"\n',
        'unknown_key = 1\n',
        '[extra]\nvalue = 1\n',
    ],
    ids=["profile", "cheap_model", "strong_model", "unknown_key", "table"],
)
def test_unknown_keys_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError, match="unknown key"):
        load_router_config(write(tmp_path, text))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('easy_tools = "read"\n', "easy_tools must be an array of strings, got str"),
        ('easy_tools = [1, 2]\n', "easy_tools[0] must be a string, got int"),
        ('easy_tools = [""]\n', "easy_tools[0] must not be empty"),
        ('error_markers = "Traceback"\n', "error_markers must be an array of strings, got str"),
        ("error_markers = [3]\n", "error_markers[0] must be a string, got int"),
        ('[shell_tools]\nnpm = "run"\n', "shell_tools must be an array of strings, got dict"),
        ('min_gap = "3"\n', "min_gap must be an integer, got str"),
        ("min_gap = -1\n", "min_gap must be at least 0, got -1"),
        ("min_gap = 2.5\n", "min_gap must be an integer, got float"),
        ("big_output_chars = 0\n", "big_output_chars must be at least 1, got 0"),
        ("big_output_chars = -5\n", "big_output_chars must be at least 1, got -5"),
        ("big_output_chars = true\n", "big_output_chars must be an integer, got bool"),
    ],
)
def test_mistyped_values_are_rejected_with_a_clear_error(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        load_router_config(write(tmp_path, text))
    assert message in str(excinfo.value)


def test_loading_never_executes_file_content(tmp_path: Path) -> None:
    payload = "__import__('os').system('touch pwned')"
    config = load_router_config(write(tmp_path, f'error_markers = ["{payload}"]\n'))
    assert config.error_markers == (payload,)
    assert not (tmp_path / "pwned").exists()


def test_loaded_exact_names_reach_decide(tmp_path: Path) -> None:
    config = load_router_config(
        write(tmp_path, 'easy_tools = ["whisk"]\nshell_tools = ["gem"]\nmin_gap = 1\n')
    )
    assert decide(body_with_tool("whisk", "ok"), state(), config).action == "SWITCH"
    assert decide(body_with_tool("gem", "ok"), state(), config).action == "SWITCH"


def test_loaded_edit_tools_override_the_token_matcher(tmp_path: Path) -> None:
    config = load_router_config(write(tmp_path, 'edit_tools = ["cat"]\nmin_gap = 1\n'))
    assert decide(body_with_tool("cat", "contents"), state(), config).action == "STAY"
    control = RouterConfig(min_gap=1)
    assert decide(body_with_tool("cat", "contents"), state(), control).action == "SWITCH"


def test_loaded_error_markers_reach_the_error_rule(tmp_path: Path) -> None:
    config = load_router_config(write(tmp_path, 'error_markers = ["Boom"]\nmin_gap = 1\n'))
    decision = decide(body_with_tool("read", "Boom happened"), state(), config)
    assert decision.action == "STAY"
    assert decision.reason == "tool error: needs strong model"
    control = RouterConfig(min_gap=1)
    assert decide(body_with_tool("read", "Boom happened"), state(), control).action == "SWITCH"


def test_loaded_min_gap_reaches_hysteresis(tmp_path: Path) -> None:
    config = load_router_config(write(tmp_path, "min_gap = 7\n"))
    assert decide(body_with_tool("read", "ok"), state(index=6), config).action == "STAY"
    assert decide(body_with_tool("read", "ok"), state(index=7), config).action == "SWITCH"
