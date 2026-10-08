"""One session-identity implementation shared by every tamias route.

A session id is a privacy-preserving grouping key for the request log.  It has
a fixed shape -- ``auto-`` followed by twelve hex characters -- so a reader can
tell at a glance that it is a digest and cannot reverse it into conversation
text or a credential.

Whatever the wire format, a client-supplied ``x-tamias-session`` value is
checked against a constrained shape and then hashed with the per-process salt,
exactly like a derived id: it is never stored or forwarded verbatim.  Without
the header, the id is derived from the conversation's identity inputs (the
authorization (or ``x-api-key``) header, the first system/developer message and
the first user message).  Every step of one conversation repeats those values,
so every step of one conversation lands on one session; two different
conversations differ in at least one of them, so they get different sessions.

Only the digest is kept.  The inputs are hashed and never stored, so the
session id cannot be reversed back into conversation text.  The salt is created
once per process, so a derived id identifies the same conversation for this
process's lifetime but is recomputed -- and so reset -- whenever the process
restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

SESSION_HEADER = "x-tamias-session"
SESSION_ID_PREFIX = "auto-"
SESSION_ID_HEX_CHARS = 12

# The largest opaque identifier a client may supply; anything longer is
# treated as absent and the conversation derives its own id instead.
SESSION_MAX_CHARS = 64
_SESSION_VALUE = re.compile(r"[A-Za-z0-9_.:-]+\Z")

# A per-process salt, shared by every route in the process, so that the same
# explicit header or conversation hashes identically on each route.  Created
# at import time; overridden in tests for deterministic digests.
_SALT = os.urandom(16)

WIRE_CHAT = "chat"
WIRE_RESPONSES = "responses"
WIRE_ANTHROPIC = "anthropic"

SYSTEM_ROLES = frozenset({"system", "developer"})
USER_ROLES = frozenset({"user"})


def new_salt() -> bytes:
    """A fresh 128-bit salt, for a new process or a test."""
    return os.urandom(16)


def sanitize_session_value(value: object) -> str | None:
    """A client-supplied session id, or None when it is not a safe identifier.

    The value must be a non-empty string of at most :data:`SESSION_MAX_CHARS`
    characters from ``[A-Za-z0-9_.:-]``.  Anything else -- including None, the
    empty string, embedded whitespace or control characters -- is treated as if
    the header had not been sent.  Whatever passes this check is still hashed
    before it is stored, so the shape check is belt-and-braces rather than the
    line of defense.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) > SESSION_MAX_CHARS:
        return None
    if not _SESSION_VALUE.match(value):
        return None
    return value


def _flat_text(content: Any) -> str:
    """The flat text of a content field, or "" when it carries none.

    Handles a plain string, a list of strings, and the block objects used by
    the Anthropic and Responses formats (``{"type": "text", "text": ...}``).
    An absent message and an unrecognised shape both hash to the same value.
    """
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


def _first_content(messages: list[Any], roles: frozenset[str]) -> str:
    """The flat text of the first message whose role is in ``roles``."""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in roles:
            continue
        text = _flat_text(message.get("content"))
        if text:
            return text
    return ""


def _anthropic_system(system: Any) -> str:
    """The top-level Anthropic ``system`` field as a single string."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _responses_user_text(body: dict[str, Any]) -> str:
    """The first user turn of a Responses ``input``, as flat text."""
    input_ = body.get("input")
    if isinstance(input_, str):
        return input_
    if not isinstance(input_, list):
        return ""
    for item in input_:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        text = _flat_text(item.get("content"))
        if text:
            return text
    return ""


def identity(headers: dict[str, str], body: dict[str, Any], wire: str) -> str:
    """The JSON string the wires all digest: ``[credential, system, user]``.

    The credential is the ``authorization`` header, falling back to
    ``x-api-key`` for the Anthropic wire, then the empty string.  The system
    and user slots come from the wire's own shapes, so the three messages a
    conversation repeats in each step are always the same three strings.
    """
    authorization = (
        headers.get("authorization")
        or headers.get("Authorization")
        or headers.get("x-api-key")
        or headers.get("X-Api-Key")
        or ""
    )
    if wire == WIRE_CHAT:
        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []
        system_text = _first_content(messages, SYSTEM_ROLES)
        user_text = _first_content(messages, USER_ROLES)
    elif wire == WIRE_ANTHROPIC:
        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []
        system_text = _anthropic_system(body.get("system"))
        user_text = _first_content(messages, USER_ROLES)
    elif wire == WIRE_RESPONSES:
        system_text = ""
        user_text = _responses_user_text(body)
    else:
        raise ValueError(f"unknown wire format: {wire!r}")
    return json.dumps(
        [authorization, system_text, user_text],
        sort_keys=True,
        default=str,
    )


def derive_session_id(
    headers: dict[str, str],
    body: dict[str, Any],
    salt: bytes | None = None,
    *,
    wire: str = WIRE_CHAT,
) -> str:
    """The session a request belongs to, as a salted digest.

    An explicit ``x-tamias-session`` header wins when present and well-formed;
    it is hashed before it is stored.  Otherwise the id is derived from the
    wire-specific identity inputs.  Either way the result starts with
    :data:`SESSION_ID_PREFIX` and is followed by
    :data:`SESSION_ID_HEX_CHARS` hex characters.
    """
    if salt is None:
        salt = _SALT
    explicit = sanitize_session_value(headers.get(SESSION_HEADER))
    if explicit is not None:
        digest = hashlib.sha256(
            salt + json.dumps([explicit], sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    else:
        digest = hashlib.sha256(salt + identity(headers, body, wire).encode("utf-8")).hexdigest()
    return SESSION_ID_PREFIX + digest[:SESSION_ID_HEX_CHARS]