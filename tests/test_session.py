"""Session identity and session-state tests. Pure: no socket is opened."""

from __future__ import annotations

import hashlib
import json

from tamias import proxy, session

SALT = b"\x00" * 16
OTHER_SALT = b"\x01" * 16
PREFIX = session.SESSION_ID_PREFIX
HEX = session.SESSION_ID_HEX_CHARS


def body_of(*messages: dict[str, str], **extra: object) -> dict[str, object]:
    return {"model": "gpt-mock", "messages": list(messages), **extra}


SYSTEM = {"role": "system", "content": "you are a coding agent"}
DEVELOPER = {"role": "developer", "content": "prefer the small tool"}
USER = {"role": "user", "content": "fix the failing test"}


def digest_of(value: str, salt: bytes) -> str:
    return PREFIX + hashlib.sha256(
        salt + json.dumps([value], sort_keys=True, default=str).encode()
    ).hexdigest()[:HEX]


def test_explicit_header_is_hashed_never_verbatim() -> None:
    """A client-supplied session id is a digest, exactly like a derived one."""
    session_id = session.derive_session_id(
        {session.SESSION_HEADER: "sess-42"}, body_of(SYSTEM, USER), SALT
    )

    assert session_id != "sess-42"
    assert session_id == digest_of("sess-42", SALT)
    assert "sess-42" not in session_id


def test_same_explicit_header_lands_on_the_same_session() -> None:
    """A client that labels its own session keeps one id across requests."""
    headers = {session.SESSION_HEADER: "sess-counter"}
    step_one = session.derive_session_id(headers, body_of(USER), SALT)
    step_two = session.derive_session_id(headers, body_of(USER), SALT)
    other = session.derive_session_id(
        {session.SESSION_HEADER: "sess-other"}, body_of(USER), SALT
    )

    assert step_one == step_two
    assert step_one != other


def test_malformed_explicit_headers_fall_back_to_the_derived_id() -> None:
    """An unshaped header is treated as absent, never stored or echoed."""
    for value in (
        None,
        "",
        " " * 10,
        "has space",
        'quote"mark',
        "a" * (session.SESSION_MAX_CHARS + 1),
    ):
        headers = {session.SESSION_HEADER: value}
        session_id = session.derive_session_id(headers, body_of(SYSTEM, USER), SALT)

        assert session_id.startswith(PREFIX)
        assert session_id != digest_of(str(value), SALT)

    assert session.sanitize_session_value("ok-1.2:3") == "ok-1.2:3"


def test_derived_id_is_prefixed_and_twelve_hex_chars() -> None:
    session_id = session.derive_session_id({}, body_of(SYSTEM, USER), SALT)

    assert session_id.startswith(PREFIX)
    suffix = session_id[len(PREFIX) :]
    assert len(suffix) == HEX
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
        session.derive_session_id({"authorization": "Bearer k"}, body, SALT)
        for body in (step_one, step_two, step_three)
    }

    assert len(ids) == 1


def test_different_conversations_get_different_ids() -> None:
    headers = {"authorization": "Bearer k"}
    base = body_of(SYSTEM, USER)

    differs_by_system = session.derive_session_id(headers, body_of(DEVELOPER, USER), SALT)
    differs_by_user = session.derive_session_id(
        headers, body_of(SYSTEM, {"role": "user", "content": "other"}), SALT
    )
    differs_by_auth = session.derive_session_id({"authorization": "Bearer other"}, base, SALT)

    ids = {
        session.derive_session_id(headers, base, SALT),
        differs_by_system,
        differs_by_user,
        differs_by_auth,
    }

    assert len(ids) == 4


def test_id_matches_the_documented_digest() -> None:
    body = body_of(SYSTEM, USER)
    headers = {"authorization": "Bearer k"}

    expected = (
        PREFIX
        + hashlib.sha256(
            SALT
            + json.dumps(
                ["Bearer k", SYSTEM["content"], USER["content"]],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:HEX]
    )

    assert session.derive_session_id(headers, body, SALT) == expected


def test_id_depends_on_the_salt() -> None:
    body = body_of(SYSTEM, USER)

    assert session.derive_session_id({}, body, SALT) != session.derive_session_id(
        {}, body, OTHER_SALT
    )


def test_conversation_text_never_appears_in_the_derived_id() -> None:
    secret = "my-api-key-is-hunter2"

    session_id = session.derive_session_id({}, body_of({"role": "user", "content": secret}), SALT)

    assert secret not in session_id
    assert len(session_id) == len(PREFIX) + HEX


def test_empty_and_malformed_bodies_still_derive_an_id() -> None:
    for body in ({}, {"messages": "nope"}, {"messages": [{"role": "user"}]}):
        session_id = session.derive_session_id({}, body, SALT)  # type: ignore[arg-type]
        assert session_id.startswith(PREFIX)


def test_anthropic_wire_derives_from_its_own_shapes() -> None:
    """x-api-key, the top-level system field and block content all feed the id."""
    headers = {"x-api-key": "sk-ant-1"}
    base = {"model": "mock", "system": "be terse", "messages": [{"role": "user", "content": "hi"}]}
    block_body = {
        "model": "mock",
        "system": [{"type": "text", "text": "be terse"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": {"url": "x"}},
                ],
            }
        ],
    }

    first = session.derive_session_id(headers, base, SALT, wire=session.WIRE_ANTHROPIC)
    by_blocks = session.derive_session_id(headers, block_body, SALT, wire=session.WIRE_ANTHROPIC)

    assert first == by_blocks
    assert first != session.derive_session_id(
        headers, base, OTHER_SALT, wire=session.WIRE_ANTHROPIC
    )
    assert first != session.derive_session_id(
        {"x-api-key": "sk-ant-other"}, base, SALT, wire=session.WIRE_ANTHROPIC
    )


def test_responses_wire_derives_from_input_text() -> None:
    """A string input and its item form both land on the same session."""
    headers = {"authorization": "Bearer k"}
    string_body = {"model": "mock", "input": "explain the plan"}
    item_body = {
        "model": "mock",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "explain the plan"}],
            }
        ],
    }

    by_string = session.derive_session_id(headers, string_body, SALT, wire=session.WIRE_RESPONSES)
    by_item = session.derive_session_id(headers, item_body, SALT, wire=session.WIRE_RESPONSES)

    assert by_string == by_item
    assert by_string != session.derive_session_id(
        headers, {"model": "mock", "input": "not the plan"}, SALT, wire=session.WIRE_RESPONSES
    )


def test_unknown_wire_format_is_rejected() -> None:
    try:
        session.derive_session_id({}, body_of(USER), SALT, wire="carrier-pigeon")
    except ValueError as error:
        assert "carrier-pigeon" in str(error)
    else:
        raise AssertionError("an unknown wire format must not derive a session")


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

    assert record.as_session_state() == proxy.SessionState(
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