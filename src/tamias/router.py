"""Rule-based routing decisions for the tamias proxy.

``decide`` is pure: it reads only its arguments, never mutates ``body``, and
keeps no module-level mutable state.  Shadow mode (which only records the
decision) and active mode (which rewrites ``body["model"]``) therefore always
agree on what the decision was.

``applied_target`` and :func:`availability_switch` are the other half: the
single places that turn a decision into an action, so no handler has to
re-derive what a ``SWITCH`` means.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

from tamias.types import Decision, SessionState, ToolClass

EASY_TOOLS: frozenset[str] = frozenset({"shell", "bash", "read", "grep", "ls", "glob"})
ERROR_MARKERS: tuple[str, ...] = ("Traceback", "FAILED", "error:")

#: A client's "leave this request alone" signal.  Active routing is opt-in and
#: bounded by three gates, but it still rewrites a model the *caller* named; a
#: caller that knows the step is a hard one must be able to say so without
#: turning the router off.  It is per-request and tamias-local: every route
#: drops it before forwarding, exactly like the session header.
PIN_HEADER = "x-tamias-pin"

#: What counts as "yes".  Anything else is ignored, so a stray
#: ``x-tamias-pin: maybe`` can never silently disable routing.
PIN_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def pin_requested(headers: Mapping[str, str]) -> bool:
    """Does the client ask for this request to stay on the model it named?"""
    value = headers.get(PIN_HEADER, "")
    return value.strip().lower() in PIN_TRUE_VALUES

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

    ``min_gap`` is how many requests must pass between two *switches*.  It
    does not limit a hold: a session already running on the chosen target keeps
    it, because bouncing back to the caller's model every other turn would
    halve the saving and double the prompt-cache rebuilds.
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
    cheap_pool: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile not in ("generic", "legacy"):
            raise ValueError(
                f"profile must be 'generic' or 'legacy', got {self.profile!r}"
            )
        object.__setattr__(self, "easy_tools", frozenset(self.easy_tools))
        object.__setattr__(self, "edit_tools", frozenset(self.edit_tools))
        object.__setattr__(self, "shell_tools", frozenset(self.shell_tools))
        pool = tuple(model for model in self.cheap_pool if model)
        object.__setattr__(self, "cheap_pool", pool)
        if not self.cheap_model and pool:
            object.__setattr__(self, "cheap_model", pool[0])
        if self.error_markers is None:
            default_markers = ERROR_MARKERS if self.profile == "legacy" else GENERIC_ERROR_MARKERS
            object.__setattr__(self, "error_markers", default_markers)
        else:
            object.__setattr__(self, "error_markers", tuple(self.error_markers))

    @property
    def pool(self) -> tuple[str, ...]:
        """Every model a SWITCH may pick, primary first.

        An empty ``cheap_pool`` is the single-target configuration, so the
        primary ``cheap_model`` is the whole pool.  Callers that validate or
        report on targets use this rather than the two fields separately.
        """
        if self.cheap_pool:
            return self.cheap_pool
        return (self.cheap_model,) if self.cheap_model else ()


DEFAULT_CONFIG = RouterConfig()


def _stay(reason: str) -> Decision:
    """Build a STAY decision.  ``target_model`` is None: no rewrite happens."""
    return Decision(action="STAY", target_model=None, reason=reason)


def _switch(target: str, reason: str, *, availability: bool = False) -> Decision:
    """Build a SWITCH decision naming the model that actually passed the gates."""
    return Decision(
        action="SWITCH",
        target_model=target,
        reason=reason,
        availability=availability,
    )


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


def _tool_result_view(messages: list[dict[str, Any]], last: dict[str, Any]) -> dict[str, Any]:
    """Rewrite an Anthropic tool-result turn into the shape the rules read.

    ``/v1/messages`` delivers a tool result as a ``user`` turn whose content
    blocks are ``{"type": "tool_result", ...}``, with the tool name on the
    preceding assistant ``tool_use`` block.  The routing rules key off a
    trailing ``tool`` turn, so without this the Anthropic route would classify
    every tool step as the user still planning and never switch at all.

    Error markers are preserved: ``is_error`` is the Anthropic way of saying
    the tool failed, which is exactly the signal that asks for the strong
    model, so it is carried as a boolean flag rather than faked into the text.
    """
    blocks = last.get("content")
    if not isinstance(blocks, list):
        return last
    results = [
        block
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    if not results:
        return last
    parts: list[str] = []
    name: str | None = None
    failed = False
    for result in results:
        content = result.get("content")
        if result.get("is_error"):
            failed = True
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(_text_of(content))
        tool_use_id = result.get("tool_use_id")
        if name is None and tool_use_id is not None:
            name = _tool_use_name(messages, tool_use_id)
    normalized: dict[str, Any] = {"role": "tool", "content": "\n".join(parts)}
    if failed:
        normalized["is_error"] = True
    if name is not None:
        normalized["name"] = name
    return normalized


def _tool_use_name(messages: list[dict[str, Any]], tool_use_id: str) -> str | None:
    """The name of the ``tool_use`` block with ``tool_use_id``, if any."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == tool_use_id
            ):
                name = block.get("name")
                return name if isinstance(name, str) else None
    return None


def _routing_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The message list as the routing rules understand it.

    OpenAI-shaped messages are already in that shape and pass through.  The
    last Anthropic tool-result turn is normalized, so the same router decision
    is reached for the same agent step whatever wire format carried it.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    normalized = [message for message in messages if isinstance(message, dict)]
    if normalized and normalized[-1].get("role") == "user":
        normalized[-1] = _tool_result_view(normalized, normalized[-1])
    return normalized


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


def _stable_index(session_id: str, size: int) -> int:
    """A deterministic index from a session id.

    ``hash()`` is salted per process, so it would give this same session id a
    different index in another process.  SHA-256 keeps the mapping stable for
    the id it is given; the id itself is tamias's salted session digest.
    """
    digest = sha256(session_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % size


def _pool_target(
    state: SessionState,
    config: RouterConfig,
    requested: str,
    ranked: Collection[str] | None = None,
    costs: Mapping[str, float | None] | None = None,
) -> str:
    """The pool member this session should use, or ``""`` when none is better.

    ``ranked`` is an already-ordered candidate list from the three routing gates
    (cheapest capable first); the first entry that is *cheaper* than the model
    the caller asked for wins, so a limited or newly-cheaper model changes the
    answer without any state.  A switch to an equal-cost model would only throw
    away the warm prompt cache, and a switch to a dearer one would lose money,
    so both are refused when ``costs`` can price them.

    Without ``ranked``, a session keeps one target (stable hash) while different
    sessions spread across the pool.

    The requested model is skipped either way: switching to the model already
    running would be a no-op and could not save anything.
    """
    pool = tuple(ranked) if ranked is not None else config.pool
    if not pool:
        return ""
    if ranked is not None:
        requested_cost = costs.get(requested) if costs else None
        for candidate in pool:
            if candidate == requested:
                continue
            if costs is not None:
                candidate_cost = costs.get(candidate)
                if (
                    requested_cost is not None
                    and candidate_cost is not None
                    and candidate_cost >= requested_cost
                ):
                    continue
            return candidate
        return ""
    start = _stable_index(state.session_id, len(pool))
    for offset in range(len(pool)):
        candidate = pool[(start + offset) % len(pool)]
        if candidate != requested:
            return candidate
    return ""


def _limited_target(
    ranked: Collection[str],
    qualities: dict[str, float] | None,
    requested: str,
) -> str:
    """The best replacement for a model the provider has locked out.

    Quality is held where it can be: the replacement must be at least as capable
    as the model the caller asked for.  Only when no such model passed the gates
    is the strongest available fallback used, because a slightly weaker answer
    beats a guaranteed failure.
    """
    if not ranked:
        return ""
    floor = (qualities or {}).get(requested)
    options = [model for model in ranked if model != requested]
    if not options:
        return ""
    if floor is not None and qualities:
        at_least = [model for model in options if qualities.get(model, 0.0) >= floor]
        if at_least:
            return at_least[0]
    return options[0]


def decide(
    body: dict[str, Any],
    state: SessionState,
    config: RouterConfig = DEFAULT_CONFIG,
    *,
    ranked: Collection[str] | None = None,
    limited: Collection[str] = (),
    qualities: dict[str, float] | None = None,
    costs: Mapping[str, float | None] | None = None,
) -> Decision:
    """Decide whether this request should be re-routed to a pool member.

    Rules, evaluated in order:

    0. the requested model is cooling down after a provider limit -> SWITCH to
       the most capable model that passed the gates, because running it again
       can only fail;
    1. a trailing ``user`` message means the agent is planning -> STAY;
    2. a trailing ``tool`` message carrying an error marker -> STAY;
    3. a trailing ``tool`` message whose shell output is over
       ``config.big_output_chars`` -> STAY;
    4. a trailing ``tool`` message from an easy tool -- under ``generic`` that
       is any read- or shell-family name -- with enough requests since the
       last switch -> SWITCH to a member of ``config.pool``.  A session already
       running on the target *holds* it: that hold is a SWITCH, not a STAY,
       because STAY forwards the caller's own model and would drop the session
       off the cheap model on every other turn;
    5. anything else -> STAY.

    ``ranked`` is the ordered output of the three routing gates (cheapest
    capable first); when given it replaces the static pool order.  ``limited``
    names models the limit ledger has locked out, ``qualities`` maps a model id
    to its capability score so rule 0 can avoid a silent downgrade, and
    ``costs`` maps a model id to its blended USD/Mtok price so rule 4 can
    refuse a switch that would not be cheaper than the requested model.
    """
    messages = _routing_messages(body)
    last: dict[str, Any] = {}
    if messages:
        last = messages[-1]
    requested_model = body.get("model")
    role = last.get("role")
    markers = config.error_markers if config.error_markers is not None else ERROR_MARKERS
    # Requests since the last *applied* switch, not since the session began.  A
    # conversation alternates planning turns (which stay on the caller's model)
    # with mechanical ones, so counting from zero would let a long-lived session
    # switch on every easy tool the moment it passed min_gap once -- the exact
    # prompt-cache thrash the hysteresis exists to prevent.  With no switch yet,
    # the session's own age is the honest answer.
    last_switch = getattr(state, "last_switch_index", None)
    since_switch = (
        state.request_index - last_switch - 1 if last_switch is not None else state.request_index
    )

    # Rule 0: a model inside its cooldown cannot answer, so the step type stops
    # mattering.  This is checked before every other rule so a limited model is
    # never kept just because the last turn looked hard.
    if isinstance(requested_model, str) and requested_model in limited:
        target = _limited_target(ranked or (), qualities, requested_model)
        if target:
            decision = _switch(
                target,
                f"requested model limited: {requested_model}",
                availability=True,
            )
        else:
            decision = _stay(f"requested model limited and no alternative: {requested_model}")

    # Rule 1: the user is talking; nothing about the last turn is routable.
    elif role == "user":
        decision = _stay("user turn: planning")

    elif role == "tool":
        # Rule 2: a failed tool call is exactly what needs the strong model.
        # ``is_error`` is the structured form of the same signal (Anthropic
        # tool results carry it), so it is honoured alongside the text markers.
        text = _text_of(last.get("content"))
        if last.get("is_error") is True or any(marker in text for marker in markers):
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
                    pool = tuple(ranked) if ranked is not None else config.pool
                    target = _pool_target(
                        state,
                        config,
                        requested_model if isinstance(requested_model, str) else "",
                        ranked=ranked,
                        costs=costs,
                    )
                    if pool and not target:
                        decision = _stay(
                            "no cheaper pool target than the requested model"
                        )
                    elif ranked is not None and not ranked:
                        # Every model failed a gate: the limit ledger locked all
                        # of them out.  Recording a SWITCH with no target would
                        # claim a saving that cannot exist.
                        decision = _stay("no model passed the routing gates")
                    else:
                        # A session already running on the target the gates
                        # picked *holds* it.  "Hold" means keeping that model,
                        # not going back to the one the client named: STAY
                        # forwards the caller's model, which undid the switch on
                        # every other turn, halving the saving and invalidating
                        # the prompt cache twice as often (see
                        # ``test_a_held_session_stays_on_the_target`` and the
                        # session walk-through in ``test_held_sessions_do_not_ping_pong``).
                        # ``min_gap`` limits *switches*, not holds, so it does not
                        # apply here.  Being on *some other* pool member is not
                        # the same thing: an expensive member must still move to
                        # the cheaper target the gates picked.
                        if target and state.current_model == target:
                            decision = Decision(
                                action="SWITCH",
                                target_model=target,
                                reason=f"already on {target}: held",
                                target_effort="low" if config.effort_policy else None,
                            )
                        elif since_switch >= config.min_gap:
                            decision = Decision(
                                action="SWITCH",
                                target_model=target,
                                reason=f"easy tool: {tool}",
                                target_effort="low" if config.effort_policy else None,
                            )
                        else:
                            decision = _stay(
                                f"hysteresis: {since_switch} of"
                                f" {config.min_gap} requests since last switch"
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


def availability_switch(
    previous: Decision,
    *,
    requested: str,
    target: str,
    reason: str,
    enabled: bool,
    allowed: Collection[str] | None = None,
) -> tuple[Decision, str | None]:
    """The decision a provider refusal justifies, and the model it may run on.

    A 429, a 404 or a 5xx is not a routing decision the operator opted into: it
    is the *provider* refusing the turn, so the retry it justifies is applied on
    the same terms as an availability-forced switch -- which is why ``enabled``
    is the caller's answer, not this function's.  In ``off`` mode nothing is ever
    rewritten, so the caller passes ``enabled=False`` and the refusal stands.

    What it must still respect is the caller's allowlist.  Without that check a
    single provider outage would silently move a restricted key onto a model it
    never named and its operator never granted, which is exactly what the
    allowlist exists to prevent -- and the log would show an ordinary SWITCH, so
    nothing would look wrong afterwards.

    Returns the decision to log and the model to run, or ``(previous, None)``
    when the refusal stands.  Suppressing the switch rather than applying and
    then refusing it is deliberate: the caller gets the provider's own answer.
    """
    decision = replace(previous, action="SWITCH", target_model=target, reason=reason)
    applied = applied_target(
        decision,
        requested=requested,
        enabled=enabled,
        allowed=allowed,
    )
    if applied is None:
        return previous, None
    return decision, applied


def applied_target(
    decision: Decision,
    *,
    requested: str,
    enabled: bool,
    allowed: Collection[str] | None = None,
) -> str | None:
    """The model to actually run on, or ``None`` to keep the requested one.

    The single place that turns a decision into an action, so the chat and
    messages handlers cannot disagree about what ``SWITCH`` means.

    ``enabled`` is the routing mode's answer: only ``active`` applies a decision,
    so ``shadow`` and ``off`` always return ``None`` and the request is forwarded
    exactly as it arrived.  ``allowed`` is the caller's model allowlist, or
    ``None`` when the caller has none.  A switch to a model the caller may not
    use is **suppressed** -- the request stays on the model the caller asked for
    -- rather than applied and then refused, so a restricted key is never failed
    on a model it never named.

    Returns the target only when it differs from ``requested``; a no-op switch
    would otherwise rewrite the body for nothing.
    """
    if not enabled or decision.action != "SWITCH" or not decision.target_model:
        return None
    if decision.target_model == requested:
        return None
    if allowed is not None and decision.target_model not in allowed:
        return None
    return decision.target_model


#: The router mode, so a reader can tell a served decision from a recorded one.
ROUTING_HEADER_MODE = "x-tamias-router-mode"
#: What the router decided for this request: ``SWITCH`` or ``STAY``.
ROUTING_HEADER_DECISION = "x-tamias-decision"
#: The model the client named in the request body.
ROUTING_HEADER_REQUESTED = "x-tamias-model-requested"
#: The model that actually ran.
ROUTING_HEADER_USED = "x-tamias-model-used"
#: The reason the router gave, the same one the log row carries.
ROUTING_HEADER_REASON = "x-tamias-reason"

#: A header value is one bounded line; a reason never needs more than this.
MAX_HEADER_VALUE = 512


def _header_value(value: str) -> str:
    """A string as one safe header line.

    Reasons and model ids are the gateway's own text, but a header is a framing
    construct: a newline in a value would end the response's header block.  The
    gateway never writes one, and this makes sure a future reason cannot.
    """
    return " ".join(value.split())[:MAX_HEADER_VALUE]


def routing_response_headers(
    *,
    mode: str,
    decision: Decision,
    requested: str | None,
    used: str | None,
) -> dict[str, str]:
    """The routing decision as response headers.

    The response body cannot carry this: the upstream only ever sees the model
    the gateway sent it, so the model that actually ran is knowledge the
    gateway holds alone.  ``requested`` is the model the client named and
    ``used`` the model that ran -- on a STAY they are the same model, and under
    ``shadow`` or ``off`` they are always the same model because nothing was
    applied, so ``mode`` is what tells a served SWITCH from a recorded one.
    """
    headers = {
        ROUTING_HEADER_MODE: _header_value(mode),
        ROUTING_HEADER_DECISION: decision.action,
    }
    if requested:
        headers[ROUTING_HEADER_REQUESTED] = _header_value(requested)
    if used:
        headers[ROUTING_HEADER_USED] = _header_value(used)
    reason = _header_value(decision.reason or "")
    if reason:
        headers[ROUTING_HEADER_REASON] = reason
    return headers
