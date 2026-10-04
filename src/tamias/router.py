"""Rule-based routing decisions for the tamias proxy.

``decide`` is pure: it reads only its arguments, never mutates ``body``, and
keeps no module-level mutable state.  Shadow mode (which only records the
decision) and active mode (which rewrites ``body["model"]``) therefore always
agree on what the decision was.
"""

from dataclasses import dataclass
from typing import Any

from tamias.types import Decision, SessionState

EASY_TOOLS: frozenset[str] = frozenset({"shell", "bash", "read", "grep", "ls", "glob"})
ERROR_MARKERS: tuple[str, ...] = ("Traceback", "FAILED", "error:")


@dataclass(frozen=True)
class RouterConfig:
    """Tunables for :func:`decide`.

    ``easy_tools`` is normalised to a frozenset so instances stay hashable and
    usable as a ``decide`` default argument.
    """

    easy_tools: frozenset[str] = EASY_TOOLS
    cheap_model: str = ""
    strong_model: str = ""
    min_gap: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "easy_tools", frozenset(self.easy_tools))


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


def decide(
    body: dict[str, Any],
    state: SessionState,
    config: RouterConfig = DEFAULT_CONFIG,
) -> Decision:
    """Decide whether this request should be re-routed to ``config.cheap_model``.

    Rules, evaluated in order:

    1. a trailing ``user`` message means the agent is planning -> STAY;
    2. a trailing ``tool`` message carrying an error marker -> STAY;
    3. a trailing ``tool`` message from an easy tool, with enough requests
       since the last switch -> SWITCH to ``cheap_model``;
    4. anything else -> STAY.
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
        return _stay("user turn: planning")

    if role == "tool":
        # Rule 2: a failed tool call is exactly what needs the strong model.
        text = _text_of(last.get("content"))
        if any(marker in text for marker in ERROR_MARKERS):
            return _stay("tool error: needs strong model")

        # Rule 3: cheap mechanical work.
        tool = _tool_name(messages, last)
        if tool in config.easy_tools:
            # Hysteresis: while we are already on the cheap model the gap since
            # the last switch is zero, so we stay put until it grows again.
            gap = 0 if state.current_model == config.cheap_model else state.request_index
            if gap >= config.min_gap:
                return Decision(
                    action="SWITCH",
                    target_model=config.cheap_model,
                    reason=f"easy tool: {tool}",
                )
            return _stay(f"hysteresis: {gap} of {config.min_gap} requests since last switch")

    # Rule 4: default.
    if isinstance(requested_model, str) and requested_model:
        return _stay(f"no rule matched: keep {requested_model}")
    return _stay("no rule matched")
