"""Session identity and session-state tests. Pure: no socket is opened."""

from __future__ import annotations

import hashlib
import json

from tamias import proxy
from tamias.types import SessionState

SALT = b"\x00" * 16
OTHER_SALT = b"\x01" * 16


def body_of(*messages: dict[str, str], **extra: object) -> dict[str, object]:
    return {"model": "gpt-mock", "messages": list(messages), **extra}


SYSTEM = {"role": "system", "content": "you are a coding agent"}
DEVELOPER = {"role": "developer", "content": "prefer the small tool"}
USER = {"role": "user", "content": "fix the failing test"}


def test_header_wins_over_the_derived_id() -> None:
    body = body_of(SYSTEM, USER)

    assert proxy.derive_session_id({proxy.SESSION_HEADER: "sess-42"}, body, SALT) == "sess-42"
    # Even with a body that would otherwise derive its own id.
    assert (
        proxy.derive_session_id(
            {proxy.SESSION_HEADER: "sess-42", "authorization": "Bearer k"}, body, SALT
        )
        == "sess-42"
    )


def test_derived_id_is_prefixed_and_twelve_hex_chars() -> None:
    session_id = proxy.derive_session_id({}, body_of(SYSTEM, USER), SALT)

    assert session_id.startswith("auto-")
    suffix = session_id[len("auto-") :]
    assert len(suffix) == 12
    assert all(character in "0123456789abcdef" for character in suffix)


def test_same_conversation_keeps_the_same_id_across_steps() -> None:
    """Later steps of one conversation repeat the same identity inputs."""
    step_one = body_of(SYSTEM, USER)
    step_two = body_of(
        SYSTEM,
        USER,
        {"role": "assistant", "content": "looking"},
        {"role": "tool", "tool_call_id": "1", "content": "file body"},
    )
    step_three = body_of(
        SYSTEM,
        USER,
        {"role": "assistant", "content": "patching"},
        {"role": "user", "content": "now add a regression test"},
    )

    ids = {
        proxy.derive_session_id({"authorization": "Bearer k"}, body, SALT)
        for body in (step_one, step_two, step_three)
    }

    assert len(ids) == 1


def test_different_conversations_get_different_ids() -> None:
    headers = {"authorization": "Bearer k"}
    base = body_of(SYSTEM, USER)

    differs_by_system = proxy.derive_session_id(headers, body_of(DEVELOPER, USER), SALT)
    differs_by_user = proxy.derive_session_id(
        headers, body_of(SYSTEM, {"role": "user", "content": "other"}), SALT
    )
    differs_by_auth = proxy.derive_session_id({"authorization": "Bearer other"}, base, SALT)

    ids = {
        proxy.derive_session_id(headers, base, SALT),
        differs_by_system,
        differs_by_user,
        differs_by_auth,
    }

    assert len(ids) == 4


def test_id_matches_the_documented_digest() -> None:
    body = body_of(SYSTEM, USER)
    headers = {"authorization": "Bearer k"}

    expected = (
        "auto-"
        + hashlib.sha256(
            SALT
            + json.dumps(
                ["Bearer k", SYSTEM["content"], USER["content"]],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:12]
    )

    assert proxy.derive_session_id(headers, body, SALT) == expected


def test_id_depends_on_the_salt() -> None:
    body = body_of(SYSTEM, USER)

    assert proxy.derive_session_id({}, body, SALT) != proxy.derive_session_id({}, body, OTHER_SALT)


def test_conversation_text_never_appears_in_the_derived_id() -> None:
    secret = "my-api-key-is-hunter2"

    session_id = proxy.derive_session_id({}, body_of({"role": "user", "content": secret}), SALT)

    assert secret not in session_id
    assert len(session_id) == len("auto-") + 12


def test_empty_and_malformed_bodies_still_derive_an_id() -> None:
    for body in ({}, {"messages": "nope"}, {"messages": [{"role": "user"}]}):
        session_id = proxy.derive_session_id({}, body, SALT)  # type: ignore[arg-type]
        assert session_id.startswith("auto-")


def test_session_table_counts_requests_per_session() -> None:
    table = proxy.SessionTable()

    first = table.get_or_create("a", "gpt-mock")
    assert (first.request_index, first.last_switch_index) == (0, None)
    first.request_index += 1

    other = table.get_or_create("b", "gpt-mock")
    assert other.request_index == 0

    again = table.get_or_create("a", "gpt-mock")
    assert again.request_index == 1
    assert again is first


def test_session_record_projects_the_state_the_router_reads() -> None:
    record = proxy.SessionRecord(
        session_id="s1", request_index=4, last_switch_index=2, current_model="gpt-cheap"
    )

    assert record.as_session_state() == SessionState(
        session_id="s1", request_index=4, current_model="gpt-cheap"
    )


def test_session_table_evicts_the_least_recently_used_session() -> None:
    table = proxy.SessionTable(capacity=2)

    table.get_or_create("a", "gpt-mock")
    table.get_or_create("b", "gpt-mock")
    table.get_or_create("a", "gpt-mock")  # "a" is now the most recent
    table.get_or_create("c", "gpt-mock")  # evicts "b"

    assert "b" not in table
    assert "a" in table and "c" in table
    assert len(table) == 2


def test_session_table_default_capacity_is_one_thousand() -> None:
    table = proxy.SessionTable()

    for index in range(proxy._SESSION_CAP):
        table.get_or_create(f"s{index}", "gpt-mock")

    assert len(table) == proxy._SESSION_CAP == 1000

    table.get_or_create("one-too-many", "gpt-mock")

    assert len(table) == 1000
    assert "s0" not in table
    assert "s999" in table
