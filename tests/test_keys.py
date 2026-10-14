"""Company key store: minting, lookup, revocation and the model allowlist.

Identity only - nothing here enforces a budget, and the tests say so by never
asserting one.  What they do pin is that the secret is never stored in the
clear, that a revoked key stops authenticating, and that an empty allowlist
means "no restriction" rather than "no access".
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tamias.keys import KEY_PREFIX, Key, KeysError, KeyStore, hash_token, mint_key


def test_minted_key_id_is_a_public_prefix_of_the_token() -> None:
    key_id, token = mint_key()
    assert key_id.startswith(KEY_PREFIX)
    assert token.startswith(key_id + "-")
    assert len(token) > len(key_id)


def test_minted_tokens_are_distinct() -> None:
    assert len({mint_key()[1] for _ in range(50)}) == 50


def test_hash_is_stable_and_never_the_token() -> None:
    token = "vk-abcdef01-secret"
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64


def test_issue_returns_identity_and_the_one_time_token() -> None:
    store = KeyStore(":memory:")
    try:
        key, token = store.issue(label="alice", project="acme", models=["m-1", "m-2"])
        assert isinstance(key, Key)
        assert key.label == "alice"
        assert key.project == "acme"
        assert key.models == ("m-1", "m-2")
        assert key.revoked_at is None
        assert not key.revoked
        assert not key.unrestricted
        assert token.startswith(key.key_id + "-")
    finally:
        store.close()


def test_the_secret_is_not_stored_in_the_clear() -> None:
    store = KeyStore(":memory:")
    try:
        _, token = store.issue(label="alice")
        row = store._conn.execute("SELECT * FROM api_keys").fetchone()
        assert row["token_hash"] == hash_token(token)
        assert token not in tuple(str(value) for value in row)
    finally:
        store.close()


def test_authenticate_accepts_only_the_real_token() -> None:
    store = KeyStore(":memory:")
    try:
        key, token = store.issue(label="alice")
        found = store.authenticate(token)
        assert found is not None and found.key_id == key.key_id
        assert store.authenticate(token + "x") is None
        assert store.authenticate("vk-deadbeef-nope") is None
        assert store.authenticate("") is None
        assert store.authenticate("not-a-vk-token") is None
    finally:
        store.close()


def test_an_empty_allowlist_means_unrestricted_not_forbidden() -> None:
    store = KeyStore(":memory:")
    try:
        key, token = store.issue(label="bob")
        assert key.models == ()
        assert key.unrestricted
        assert store.authenticate(token) is not None
    finally:
        store.close()


def test_duplicate_models_are_collapsed_and_trimmed() -> None:
    store = KeyStore(":memory:")
    try:
        key, _ = store.issue(models=[" m-1 ", "m-1", "m-2"])
        assert key.models == ("m-1", "m-2")
    finally:
        store.close()


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_a_blank_model_is_rejected(bad: str) -> None:
    store = KeyStore(":memory:")
    try:
        with pytest.raises(KeysError):
            store.issue(models=[bad])
    finally:
        store.close()


def test_a_project_must_be_a_bounded_identifier() -> None:
    store = KeyStore(":memory:")
    try:
        with pytest.raises(KeysError):
            store.issue(project="not a project")
        assert store.issue(project="work-1.alpha")[0].project == "work-1.alpha"
        assert store.issue(project=None)[0].project is None
    finally:
        store.close()


def test_revoke_is_idempotent_and_unknown_keys_report_false() -> None:
    store = KeyStore(":memory:")
    try:
        key, token = store.issue(label="alice")
        assert store.revoke(key.key_id) is True
        assert store.revoke(key.key_id) is False
        assert store.revoke("vk-00000000") is False
        assert store.authenticate(token) is None
        stored = store.get(key.key_id)
        assert stored is not None and stored.revoked is True
    finally:
        store.close()


def test_a_key_carries_no_field_that_could_hold_the_token() -> None:
    store = KeyStore(":memory:")
    try:
        key, _ = store.issue(label="alice")
        assert not hasattr(key, "token")
        assert not any("token" in field for field in Key.__slots__)
    finally:
        store.close()


def test_list_and_get_find_keys_by_id() -> None:
    store = KeyStore(":memory:")
    try:
        first, _ = store.issue(label="alice", project="acme", models=["m-1"])
        second, _ = store.issue(label="bob")
        assert {k.key_id for k in store.list()} == {first.key_id, second.key_id}
        assert store.get(first.key_id).label == "alice"
        assert store.get("vk-00000000") is None
    finally:
        store.close()


def test_keys_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "keys.db"
    store = KeyStore(path)
    key, token = store.issue(label="alice", project="acme", models=["m-1"])
    store.close()

    reopened = KeyStore(path)
    try:
        assert reopened.authenticate(token) is not None
        assert reopened.get(key.key_id).models == ("m-1",)
    finally:
        reopened.close()


def test_the_key_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "keys.db"
    store = KeyStore(path)
    store.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_store_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "keys.db"
    store = KeyStore(path)
    try:
        store.issue(label="alice")
        assert path.exists()
    finally:
        store.close()
