"""Signed project manifests: the policy a company publishes, in signed form.

The agent's use-case policy decides what a run may touch.  If that policy is a
file the employee can edit, the fence is decorative: the agent reads whatever
the file says.  A manifest moves the fence to the gateway -- the policy travels
as a JSON document plus a detached signature, the gateway verifies the signature
before it will serve the policy, and a tampered or expired manifest is refused.

The signature is Ed25519 when the optional ``cryptography`` package is
installed (``pip install 'tamias[manifest]'``).  Asymmetric is the right default
here: the gateway holds only the *public* key, so compromising the gateway does
not let it mint a policy of its own.

The document is JSON with a canonical byte form -- sorted keys, no whitespace,
ASCII -- so a signer and a verifier anywhere reproduce the same bytes.  The
signature covers every field but ``signature`` itself::

    {
      "manifest_version": 1,
      "algorithm": "ed25519",
      "public_key": "<base64 raw Ed25519 public key>",
      "project": "acme",
      "issued_at": "2026-04-01T00:00:00Z",
      "expires": "2026-12-31T00:00:00Z",
      "policy": {"use_case": {"name": "codefix"}, ...},
      "signature": "<base64 Ed25519 signature over the region above>"
    }

``project`` may be the empty string for a manifest that is not bound to one
project.  ``expires`` is required: a manifest with no expiry is a policy that
can never be withdrawn, which is the failure mode signing exists to prevent.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The only manifest schema this build understands.
MANIFEST_VERSION = 1
#: Signature algorithms this build can verify.  ``ed25519`` is the default.
SUPPORTED_ALGORITHMS = ("ed25519",)
#: The field the signature does *not* cover; everything else is signed.
SIGNATURE_FIELD = "signature"
#: Fields a manifest must carry before it can even be checked.
REQUIRED_FIELDS = (
    "manifest_version",
    "algorithm",
    "public_key",
    "project",
    "issued_at",
    "expires",
    "policy",
)

_INSTALL_HINT = (
    "signed manifests need the optional 'cryptography' package: "
    "pip install 'tamias[manifest]'"
)


class ManifestError(Exception):
    """A manifest that cannot be trusted: malformed, unsigned, expired or mis-bound."""


def _require_crypto() -> tuple[Any, Any, Any]:
    """Import the Ed25519 primitives, or raise a message the operator can act on."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ManifestError(_INSTALL_HINT) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def _public_raw(key: Any) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _utc_iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object, field: str) -> datetime:
    """Read an ISO-8601 timestamp, rejecting anything a clock cannot agree on."""
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"manifest {field} must be an ISO-8601 timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ManifestError(
            f"manifest {field} is not an ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def canonical_bytes(region: dict[str, Any]) -> bytes:
    """The exact bytes a signer and a verifier both cover.

    ``sort_keys`` plus tight separators give a stable encoding for the value
    types a manifest may hold (strings, numbers, booleans, lists, nested
    objects), so no round-trip through TOML -- which has no canonical form --
    is needed.
    """
    return json.dumps(
        region, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def manifest_region(manifest: dict[str, Any]) -> dict[str, Any]:
    """Everything the signature covers: the manifest without its signature."""
    return {key: value for key, value in manifest.items() if key != SIGNATURE_FIELD}


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: object, field: str, *, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"manifest {field} must be base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError(f"manifest {field} is not valid base64") from exc
    if length is not None and len(raw) != length:
        raise ManifestError(
            f"manifest {field} must decode to {length} bytes, got {len(raw)}"
        )
    return raw


def validate_policy(policy: Any) -> None:
    """The agent's minimum policy contract, checked here too.

    The gateway will not sign or serve a fence the agent would refuse to honour,
    so a manifest cannot look valid while carrying a policy with no use case.
    """
    if not isinstance(policy, dict) or not policy:
        raise ManifestError("manifest policy must be a non-empty table")
    use_case = policy.get("use_case")
    if (
        not isinstance(use_case, dict)
        or not isinstance(use_case.get("name"), str)
        or not use_case["name"].strip()
    ):
        raise ManifestError("policy needs a [use_case] table with a name")


def policy_use_case(policy: dict[str, Any]) -> str:
    """The use-case name a policy carries, or ``""`` when it has none."""
    use_case = policy.get("use_case")
    if isinstance(use_case, dict) and isinstance(use_case.get("name"), str):
        return use_case["name"]
    return ""


def build_manifest(
    policy: dict[str, Any],
    project: str,
    *,
    public_key: str,
    expires: str,
    issued_at: str | None = None,
    algorithm: str = "ed25519",
) -> dict[str, Any]:
    """Assemble the unsigned region of a manifest (no signature yet)."""
    if not isinstance(project, str):
        raise ManifestError("manifest project must be a string")
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ManifestError(f"unsupported manifest algorithm: {algorithm!r}")
    validate_policy(policy)
    parse_time(expires, "expires")
    stamp = issued_at or _utc_iso(datetime.now(UTC))
    parse_time(stamp, "issued_at")
    return {
        "manifest_version": MANIFEST_VERSION,
        "algorithm": algorithm,
        "public_key": public_key,
        "project": project,
        "issued_at": stamp,
        "expires": expires,
        "policy": policy,
    }


def sign_manifest(
    policy: dict[str, Any],
    project: str,
    private_key: Any,
    *,
    expires: str,
    issued_at: str | None = None,
    algorithm: str = "ed25519",
) -> dict[str, Any]:
    """Sign ``policy`` for ``project``, returning the complete manifest.

    ``private_key`` is a ``cryptography`` Ed25519 private key or its raw 32-byte
    value.  The public key is derived, so the caller cannot sign with one key
    and advertise another.
    """
    Ed25519PrivateKey, _, _ = _require_crypto()
    if isinstance(private_key, (bytes, bytearray)):
        try:
            key = Ed25519PrivateKey.from_private_bytes(bytes(private_key))
        except ValueError as exc:
            raise ManifestError("raw private key must be 32 bytes") from exc
    else:
        key = private_key
    region = build_manifest(
        policy,
        project,
        public_key=_b64encode(_public_raw(key)),
        expires=expires,
        issued_at=issued_at,
        algorithm=algorithm,
    )
    signature = key.sign(canonical_bytes(region))
    return {**region, SIGNATURE_FIELD: _b64encode(signature)}


def _pin_bytes(trusted: bytes | bytearray | str) -> bytes:
    """Normalise a trust anchor: raw bytes, base64 text, or a PEM public key."""
    if isinstance(trusted, (bytes, bytearray)):
        raw = bytes(trusted)
        if len(raw) != 32:
            raise ManifestError("trusted public key must be 32 raw bytes")
        return raw
    text = trusted.strip()
    if text.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_public_key,
        )

        try:
            key = load_pem_public_key(text.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise ManifestError("trusted public key PEM could not be loaded") from exc
        return key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64decode(text, "trusted public key", length=32)


def verify_manifest(
    manifest: object,
    *,
    now: datetime | None = None,
    expected_project: str | None = None,
    trusted_public_key: bytes | bytearray | str | None = None,
) -> dict[str, Any]:
    """Return the manifest when it is trustworthy, else raise ManifestError.

    Checks, in order: shape, schema version, algorithm, expiry, project binding,
    a pinned public key when one is given, and finally the signature itself.
    An expired manifest is refused before the signature is even computed.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            raise ManifestError(f"manifest is missing {field!r}")
    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest_version {manifest['manifest_version']!r} is not supported "
            f"(this build understands {MANIFEST_VERSION})"
        )
    algorithm = manifest["algorithm"]
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ManifestError(f"unsupported manifest algorithm: {algorithm!r}")
    if not isinstance(manifest["project"], str):
        raise ManifestError("manifest project must be a string")
    validate_policy(manifest["policy"])

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    expires = parse_time(manifest["expires"], "expires")
    if expires <= moment:
        raise ManifestError(f"manifest expired at {manifest['expires']}")
    parse_time(manifest["issued_at"], "issued_at")

    if expected_project is not None and manifest["project"] != expected_project:
        raise ManifestError(
            f"manifest is for project {manifest['project']!r}, not "
            f"{expected_project!r}"
        )

    public_raw = _b64decode(manifest["public_key"], "public_key", length=32)
    if trusted_public_key is not None and _pin_bytes(trusted_public_key) != public_raw:
        raise ManifestError("manifest public key does not match the trusted key")

    signature = _b64decode(manifest[SIGNATURE_FIELD], "signature", length=64)
    _, Ed25519PublicKey, InvalidSignature = _require_crypto()
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(
            signature, canonical_bytes(manifest_region(manifest))
        )
    except InvalidSignature as exc:
        raise ManifestError("manifest signature does not verify") from exc
    return manifest


def generate_keypair() -> tuple[Any, bytes]:
    """A fresh Ed25519 private key and its raw 32-byte public key."""
    Ed25519PrivateKey, _, _ = _require_crypto()
    key = Ed25519PrivateKey.generate()
    return key, _public_raw(key)


def private_key_pem(key: Any) -> str:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(
        "ascii"
    )


def public_key_pem(raw: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return (
        Ed25519PublicKey.from_public_bytes(raw)
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def load_private_key(path: str | Path) -> Any:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ManifestError(f"could not read private key {path}: {exc}") from exc
    try:
        return load_pem_private_key(raw, password=None)
    except (ValueError, TypeError) as exc:
        raise ManifestError(f"could not load private key {path}: {exc}") from exc


def load_public_key(path: str | Path) -> bytes:
    """A PEM or base64 public key file, returned as its raw 32 bytes."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"could not read public key {path}: {exc}") from exc
    return _pin_bytes(text)


def load_manifest(path: str | Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"could not read manifest {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"manifest {path} must be a JSON object")
    return data


def dump_manifest(manifest: dict[str, Any], path: str | Path | None = None) -> str:
    """Serialise a manifest; write it when ``path`` is given, and return the text."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text
