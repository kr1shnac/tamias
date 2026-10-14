"""Tests for :mod:`tamias.manifest` -- the signed company policy (D12).

The property under test is that only a policy that was signed by the holder of
the private key, for the right project, and before its expiry, verifies; every
other mutation of the document is refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("cryptography")

from tamias import manifest as m  # noqa: E402

POLICY = {
    "version": "1",
    "use_case": {"name": "codefix", "read_only": False},
    "tools": {"allow": [], "deny": ["run_shell"]},
}
PROJECT = "acme"
EXPIRES = "2099-12-31T00:00:00Z"


def _signed(**overrides):
    key, _ = m.generate_keypair()
    kwargs = {"expires": EXPIRES}
    kwargs.update(overrides)
    return m.sign_manifest(POLICY, PROJECT, key, **kwargs)


# --- round trip --------------------------------------------------------------


def test_sign_then_verify_round_trips():
    signed = _signed()
    assert m.verify_manifest(signed) is signed
    assert signed["manifest_version"] == m.MANIFEST_VERSION
    assert signed["algorithm"] == "ed25519"
    assert signed["project"] == PROJECT
    assert signed["policy"] == POLICY


def test_verify_binds_to_the_expected_project():
    signed = _signed()
    assert m.verify_manifest(signed, expected_project=PROJECT) is signed
    with pytest.raises(m.ManifestError):
        m.verify_manifest(signed, expected_project="other")


def test_raw_private_key_bytes_sign():
    key, _ = m.generate_keypair()
    from cryptography.hazmat.primitives import serialization

    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    signed = m.sign_manifest(POLICY, PROJECT, raw, expires=EXPIRES)
    assert m.verify_manifest(signed)


# --- tampering is refused ----------------------------------------------------


def test_tampered_policy_is_refused():
    signed = _signed()
    signed["policy"]["tools"]["deny"] = []
    with pytest.raises(m.ManifestError, match="signature does not verify"):
        m.verify_manifest(signed)


def test_tampered_signature_is_refused():
    signed = _signed()
    signed["signature"] = "A" * 88
    with pytest.raises(m.ManifestError):
        m.verify_manifest(signed)


def test_tampered_public_key_is_refused():
    signed = _signed()
    other, other_public = m.generate_keypair()
    signed["public_key"] = m._b64encode(other_public)
    with pytest.raises(m.ManifestError):
        m.verify_manifest(signed)


def test_changing_the_expiry_invalidates_the_signature():
    signed = _signed()
    signed["expires"] = "2098-01-01T00:00:00Z"
    with pytest.raises(m.ManifestError, match="signature does not verify"):
        m.verify_manifest(signed)


def test_changing_the_project_invalidates_the_signature():
    signed = _signed()
    signed["project"] = "evil"
    with pytest.raises(m.ManifestError, match="signature does not verify"):
        m.verify_manifest(signed)


def test_changing_issued_at_invalidates_the_signature():
    signed = _signed()
    signed["issued_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(m.ManifestError, match="signature does not verify"):
        m.verify_manifest(signed)


# --- expiry ------------------------------------------------------------------


def test_expired_manifest_is_refused():
    signed = _signed(expires="2020-01-01T00:00:00Z")
    with pytest.raises(m.ManifestError, match="expired"):
        m.verify_manifest(signed)


def test_manifest_valid_until_its_instant_then_expired():
    moment = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)
    signed = _signed(expires="2030-06-01T12:00:00Z")
    # The expiry instant itself counts as expired (<= now).
    with pytest.raises(m.ManifestError, match="expired"):
        m.verify_manifest(signed, now=moment)
    just_before = moment - timedelta(seconds=1)
    assert m.verify_manifest(signed, now=just_before)


def test_naive_now_is_treated_as_utc():
    signed = _signed(expires="2030-06-01T12:00:00Z")
    assert m.verify_manifest(signed, now=datetime(2030, 6, 1, 11, 0, 0))


# --- trust anchor pinning ----------------------------------------------------


def test_pinned_public_key_matches_and_mismatches():
    key, public = m.generate_keypair()
    signed = m.sign_manifest(POLICY, PROJECT, key, expires=EXPIRES)
    assert m.verify_manifest(signed, trusted_public_key=public)
    assert m.verify_manifest(signed, trusted_public_key=m.public_key_pem(public))
    assert m.verify_manifest(signed, trusted_public_key=m._b64encode(public))
    _, other = m.generate_keypair()
    with pytest.raises(m.ManifestError, match="does not match the trusted key"):
        m.verify_manifest(signed, trusted_public_key=other)


# --- schema shape ------------------------------------------------------------


@pytest.mark.parametrize("field", m.REQUIRED_FIELDS)
def test_missing_field_is_refused(field):
    signed = _signed()
    del signed[field]
    with pytest.raises(m.ManifestError, match=f"missing {field!r}"):
        m.verify_manifest(signed)


def test_unsupported_version_is_refused():
    signed = _signed()
    signed["manifest_version"] = 99
    with pytest.raises(m.ManifestError, match="not supported"):
        m.verify_manifest(signed)


def test_unsupported_algorithm_is_refused():
    signed = _signed()
    signed["algorithm"] = "hmac"
    with pytest.raises(m.ManifestError, match="unsupported manifest algorithm"):
        m.verify_manifest(signed)


def test_non_object_manifest_is_refused():
    with pytest.raises(m.ManifestError, match="JSON object"):
        m.verify_manifest(["not", "an", "object"])


def test_policy_without_a_use_case_name_is_refused_on_both_sides():
    with pytest.raises(m.ManifestError, match=r"\[use_case\]"):
        m.sign_manifest({"version": "1"}, PROJECT, m.generate_keypair()[0], expires=EXPIRES)
    signed = _signed()
    signed["policy"] = {"version": "1"}
    with pytest.raises(m.ManifestError, match=r"\[use_case\]"):
        m.verify_manifest(signed)


def test_empty_policy_is_refused():
    with pytest.raises(m.ManifestError, match="non-empty"):
        m.sign_manifest({}, PROJECT, m.generate_keypair()[0], expires=EXPIRES)


def test_unknown_algorithm_cannot_be_signed():
    with pytest.raises(m.ManifestError, match="unsupported manifest algorithm"):
        m.sign_manifest(POLICY, PROJECT, m.generate_keypair()[0], expires=EXPIRES, algorithm="rsa")


def test_bad_expiry_is_refused_at_sign_time():
    with pytest.raises(m.ManifestError, match="ISO-8601"):
        m.sign_manifest(POLICY, PROJECT, m.generate_keypair()[0], expires="tomorrow")


# --- canonical form ----------------------------------------------------------


def test_canonical_bytes_ignore_key_order():
    assert m.canonical_bytes({"b": 1, "a": 2}) == m.canonical_bytes({"a": 2, "b": 1})


def test_canonical_bytes_are_stable_and_compact():
    assert m.canonical_bytes({"a": 1, "b": "x"}) == b'{"a":1,"b":"x"}'


def test_parse_time_accepts_z_offsets_and_naive():
    assert m.parse_time("2030-01-01T00:00:00Z", "expires").tzinfo is UTC
    assert m.parse_time("2030-01-01T00:00:00+00:00", "expires").tzinfo is not None
    assert m.parse_time("2030-01-01T00:00:00", "expires").tzinfo is UTC


def test_policy_use_case_reads_the_name():
    assert m.policy_use_case(POLICY) == "codefix"
    assert m.policy_use_case({}) == ""


# --- files -------------------------------------------------------------------


def test_dump_and_load_round_trip(tmp_path):
    signed = _signed()
    target = tmp_path / "manifest.json"
    text = m.dump_manifest(signed, target)
    assert target.read_text(encoding="utf-8") == text
    assert m.load_manifest(target) == signed
    assert json.loads(text) == signed


def test_load_manifest_rejects_non_json(tmp_path):
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(m.ManifestError, match="not valid JSON"):
        m.load_manifest(target)


def test_load_manifest_reports_a_missing_file(tmp_path):
    with pytest.raises(m.ManifestError, match="could not read manifest"):
        m.load_manifest(tmp_path / "absent.json")


def test_key_files_round_trip(tmp_path):
    key, public = m.generate_keypair()
    private_path = tmp_path / "signing.pem"
    public_path = tmp_path / "trust.pem"
    private_path.write_text(m.private_key_pem(key), encoding="utf-8")
    public_path.write_text(m.public_key_pem(public), encoding="utf-8")
    loaded = m.load_private_key(private_path)
    signed = m.sign_manifest(POLICY, PROJECT, loaded, expires=EXPIRES)
    assert m.verify_manifest(signed, trusted_public_key=m.load_public_key(public_path))


def test_load_private_key_reports_a_missing_file(tmp_path):
    with pytest.raises(m.ManifestError, match="could not read private key"):
        m.load_private_key(tmp_path / "absent.pem")
