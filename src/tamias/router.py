"""Rule-based routing decisions for the tamias proxy.

``decide`` is pure: it reads only its arguments, never mutates ``body``, and
keeps no module-level mutable state.  Shadow mode (which only records the
decision) and active mode (which rewrites ``body["model"]``) therefore always
agree on what the decision was.
"""

import re
from dataclasses import dataclass, replace
from typing import Any

from tamias.types import Decision, SessionState, ToolClass

EASY_TOOLS: frozenset[str] = frozenset({"shell", "bash", "read", "grep", "ls", "glob"})
ERROR_MARKERS: tuple[str, ...] = ("Traceback", "FAILED", "error:")

GENERIC_ERROR_MARKERS: tuple[str, ...] = (
    "Traceback",
    "FAILED",
    "error:",
    "Exception",
    "non-zero exit code",
)
"""What the generic profile treats as a failed tool result.

The v1 three plus what real tool output prints when it fails.  ``Error:`` is
deliberately not in the default: ``test_error_markers_are_case_sensitive`` in
tests/test_router.py pins it as not-a-marker for the default config, so it is
opt-in through ``RouterConfig(error_markers=...)`` or the TOML key.
"""

READ_TOKENS: frozenset[str] = frozenset(
    {"read", "grep", "glob", "ls", "list", "find", "cat", "head", "tail", "search", "view", "stat"}
)
EDIT_TOKENS: frozenset[str] = frozenset(
    {"write", "edit", "patch", "create", "replace", "multiedit", "mkdir"}
)
SHELL_TOKENS: frozenset[str] = frozenset(
    {"bash", "shell", "sh", "run", "exec", "execute", "command", "terminal"}
)
NEUTRAL_TOKENS: frozenset[str] = frozenset({"todo", "plan", "task", "think"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NAME_SEPARATORS = re.compile(r"[_\-.]+")


def _tokens(name: str) -> list[str]:
    """Split a tool name into lower-case tokens.

    Splits on underscores, hyphens and dots first, then on camelCase
    boundaries, so ``str_replace_editor`` -> [str, replace, editor] and
    ``MultiEdit`` -> [multi, edit].
    """
    words: list[str] = []
    for chunk in _NAME_SEPARATORS.split(name):
        for word in _CAMEL_BOUNDARY.split(chunk):
            if word:
                words.append(word.lower())
    return words


def classify_tool(name: str) -> ToolClass:
    """Classify a tool name into the read, edit or shell family.

    A name carrying a neutral token (todo, plan, task, think) is always
    ``unknown``, and so is a name that matches two non-neutral families:
    ``read_shell`` says nothing about which family an agent meant.
    """
    tokens = set(_tokens(name))
    if not tokens or tokens & NEUTRAL_TOKENS:
        return "unknown"
    in_read = bool(tokens & READ_TOKENS)
    in_edit = bool(tokens & EDIT_TOKENS)
    in_shell = bool(tokens & SHELL_TOKENS)
    if in_read and not in_edit and not in_shell:
        return "read"
    if in_edit and not in_read and not in_shell:
        return "edit"
    if in_shell and not in_read and not in_edit:
        return "shell"
    return "unknown"


@dataclass(frozen=True)
class RouterConfig:
    """Tunables for :func:`decide`.

    ``easy_tools`` is normalised to a frozenset so instances stay hashable and
    usable as a ``decide`` default argument.

    ``profile`` selects the matching strategy: ``generic`` (the default)
    recognises tool names from any agent through :func:`classify_tool`;
    ``legacy`` is exactly the v1 behaviour, exact-name matching against
    ``easy_tools`` only.  Any other value is rejected.

    ``edit_tools`` and ``shell_tools`` are extra exact names for those
    families, ``error_markers`` are the substrings that make a tool result a
    failure (None means "profile default"), and ``big_output_chars`` caps how
    large a shell result may be and still be easy (None means no cap).
    """

    easy_tools: frozenset[str] = EASY_TOOLS
    cheap_model: str = ""
    strong_model: str = ""
    min_gap: int = 3
    profile: str = "generic"
    edit_tools: frozenset[str] = frozenset()
    shell_tools: frozenset[str] = frozenset()
    error_markers: tuple[str, ...] | None = None
    big_output_chars: int | None = None
    effort_policy: bool = False

    def __post_init__(self) -> None:
        if self.profile not in ("generic", "legacy"):
            raise ValueError(
                f"profile must be 'generic' or 'legacy', got {self.profile!r}"
            )
        object.__setattr__(self, "easy_tools", frozenset(self.easy_tools))
        object.__setattr__(self, "edit_tools", frozenset(self.edit_tools))
        object.__setattr__(self, "shell_tools", frozenset(self.shell_tools))
        if self.error_markers is None:
            default_markers = ERROR_MARKERS if self.profile == "legacy" else GENERIC_ERROR_MARKERS
            object.__setattr__(self, "error_markers", default_markers)
        else:
            object.__setattr__(self, "error_markers", tuple(self.error_markers))


DEFAULT_CONFIG = RouterConfig()


def _stay(reason: str) -> Decision:
    """Build a STAY decision.  ``target_model`` is None: no rewrite happens."""
    return Decision(action="STAY", target_model=None, reason=reason)


def _text_of(content: Any) -> str:
    """Flatten a message ``content`` field to searchable text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _tool_name(messages: list[Any], tool_message: dict[str, Any]) -> str | None:
    """Resolve the name of the tool that produced ``tool_message``.

    Prefers the preceding assistant message's ``tool_calls`` entry whose ``id``
    matches ``tool_call_id``; falls back to the tool message's own ``name``.
    """
    tool_call_id = tool_message.get("tool_call_id")
    if tool_call_id is not None:
        for message in reversed(messages[:-1]):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or call.get("id") != tool_call_id:
                    continue
                function = call.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                    if isinstance(name, str):
                        return name
            break  # only the nearest preceding assistant message is consulted
    name = tool_message.get("name")
    return name if isinstance(name, str) else None


def _tool_class(tool: str, config: RouterConfig) -> ToolClass:
    """Classify ``tool``, letting the exact-name sets override the matcher.

    ``shell_tools`` and ``edit_tools`` are the operator's way of pinning a
    name to a family the token matcher cannot see.
    """
    if tool in config.shell_tools:
        return "shell"
    if tool in config.edit_tools:
        return "edit"
    return classify_tool(tool)


def _is_easy(tool: str, config: RouterConfig) -> bool:
    """Whether ``tool`` is cheap mechanical work the cheap model can handle.

    An exact name in ``easy_tools`` always qualifies; under the ``legacy``
    profile that is the whole rule, exactly as in v1.  Under ``generic`` the
    matcher decides, and only the read and shell families route -- edit work
    and UNKNOWN names are what the strong model is for.
    """
    if tool in config.easy_tools:
        return True
    if config.profile == "legacy":
        return False
    return _tool_class(tool, config) in ("read", "shell")


def decide(
    body: dict[str, Any],
    state: SessionState,
    config: RouterConfig = DEFAULT_CONFIG,
) -> Decision:
    """Decide whether this request should be re-routed to ``config.cheap_model``.

    Rules, evaluated in order:

    1. a trailing ``user`` message means the agent is planning -> STAY;
    2. a trailing ``tool`` message carrying an error marker -> STAY;
    3. a trailing ``tool`` message whose shell output is over
       ``config.big_output_chars`` -> STAY;
    4. a trailing ``tool`` message from an easy tool -- under ``generic`` that
       is any read- or shell-family name -- with enough requests since the
       last switch -> SWITCH to ``config.cheap_model``;
    5. anything else -> STAY.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    last: dict[str, Any] = {}
    if messages and isinstance(messages[-1], dict):
        last = messages[-1]
    requested_model = body.get("model")
    role = last.get("role")

    # Rule 1: the user is talking; nothing about the last turn is routable.
    if role == "user":
        decision = _stay("user turn: planning")

    elif role == "tool":
        # Rule 2: a failed tool call is exactly what needs the strong model.
        text = _text_of(last.get("content"))
        markers = config.error_markers if config.error_markers is not None else ERROR_MARKERS
        if any(marker in text for marker in markers):
            decision = _stay("tool error: needs strong model")
        else:
            # Rule 3: cheap mechanical work.
            tool = _tool_name(messages, last)
            if tool is not None:
                tool_class = _tool_class(tool, config)
                if (
                    tool_class == "shell"
                    and config.big_output_chars is not None
                    and len(text) >= config.big_output_chars
                ):
                    decision = _stay(
                        f"shell output too large: {len(text)} >= {config.big_output_chars} chars"
                    )
                elif _is_easy(tool, config):
                    # Hysteresis: while we are already on the cheap model the gap
                    # since the last switch is zero, so we stay put until it grows
                    # again.
                    gap = 0 if state.current_model == config.cheap_model else state.request_index
                    if gap >= config.min_gap:
                        decision = Decision(
                            action="SWITCH",
                            target_model=config.cheap_model,
                            reason=f"easy tool: {tool}",
                            target_effort="low" if config.effort_policy else None,
                        )
                    else:
                        decision = _stay(
                            f"hysteresis: {gap} of {config.min_gap} requests since last switch"
                        )
                else:
                    decision = _stay(f"no rule matched: keep {requested_model}")
            else:
                decision = _stay(f"no rule matched: keep {requested_model}")
    else:
        decision = _stay(
            f"no rule matched: keep {requested_model}"
            if isinstance(requested_model, str) and requested_model
            else "no rule matched"
        )

    if config.effort_policy and decision.target_effort is None:
        is_error = role == "tool" and any(
            marker in _text_of(last.get("content")) for marker in markers
        )
        if role == "user" or is_error:
            decision = replace(decision, target_effort="high")
        elif role == "tool":
            tool = _tool_name(messages, last)
            if tool is not None and _is_easy(tool, config):
                decision = replace(decision, target_effort="low")
    return decision
