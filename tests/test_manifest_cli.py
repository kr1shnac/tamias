"""``tamias admin manifest`` exercised through the CLI.

The command is the operator's side of D12: generate a signing key, sign a policy
TOML into a manifest, and verify one the way the gateway will.  The property is
that the private key never needs to reach the gateway and only a manifest that
verifies is accepted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("cryptography")

from tamias import cli  # noqa: E402

POLICY_TOML = """
version = "1"

[use_case]
name = "codefix"
read_only = false

[tools]
deny = ["run_shell"]
"""


def write_policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(POLICY_TOML, encoding="utf-8")
    return path


def keygen(tmp_path: Path) -> tuple[Path, Path]:
    private = tmp_path / "sign.pem"
    public = tmp_path / "trust.pem"
    code = cli.main(
        [
            "admin",
            "manifest",
            "keygen",
            "--private-out",
            str(private),
            "--public-out",
            str(public),
        ]
    )
    assert code == 0
    return private, public


def sign(tmp_path: Path, private: Path, *, project: str = "acme") -> Path:
    out = tmp_path / "manifest.json"
    code = cli.main(
        [
            "admin",
            "manifest",
            "sign",
            "--policy",
            str(write_policy(tmp_path)),
            "--key",
            str(private),
            "--project",
            project,
            "--expires",
            "2099-12-31T00:00:00Z",
            "-o",
            str(out),
        ]
    )
    assert code == 0
    return out


def test_keygen_writes_a_locked_down_private_key(tmp_path: Path) -> None:
    private, public = keygen(tmp_path)
    assert private.exists() and public.exists()
    assert private.stat().st_mode & 0o777 == 0o600
    assert "BEGIN PUBLIC KEY" in public.read_text(encoding="utf-8")


def test_sign_then_verify_round_trips_and_reports(tmp_path: Path, capsys) -> None:
    private, public = keygen(tmp_path)
    manifest = sign(tmp_path, private)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["project"] == "acme"
    assert document["policy"]["use_case"]["name"] == "codefix"
    capsys.readouterr()

    code = cli.main(
        [
            "admin",
            "manifest",
            "verify",
            str(manifest),
            "--key",
            str(public),
            "--project",
            "acme",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("ok: ")
    assert "project=acme" in out and "use_case=codefix" in out


def test_sign_to_stdout_when_no_output_is_given(tmp_path: Path, capsys) -> None:
    private, _ = keygen(tmp_path)
    capsys.readouterr()
    code = cli.main(
        [
            "admin",
            "manifest",
            "sign",
            "--policy",
            str(write_policy(tmp_path)),
            "--key",
            str(private),
            "--project",
            "acme",
            "--expires",
            "2099-12-31T00:00:00Z",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    document = json.loads(out[: out.rindex("}") + 1])
    assert document["project"] == "acme"


def test_verify_rejects_the_wrong_project(tmp_path: Path, capsys) -> None:
    private, _ = keygen(tmp_path)
    manifest = sign(tmp_path, private, project="acme")
    capsys.readouterr()
    assert cli.main(["admin", "manifest", "verify", str(manifest), "--project", "other"]) == 1
    assert "not 'other'" in capsys.readouterr().err


def test_verify_rejects_a_tampered_policy(tmp_path: Path, capsys) -> None:
    private, public = keygen(tmp_path)
    manifest = sign(tmp_path, private)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["policy"]["use_case"]["name"] = "evil"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()
    code = cli.main(["admin", "manifest", "verify", str(manifest), "--key", str(public)])
    assert code == 1
    assert "signature does not verify" in capsys.readouterr().err


def test_verify_rejects_another_signing_key(tmp_path: Path, capsys) -> None:
    private, _ = keygen(tmp_path)
    manifest = sign(tmp_path, private)
    # A pin to a *different* pair must refuse the manifest signer's key.
    from tamias import manifest as m

    _, other_public = m.generate_keypair()
    other = tmp_path / "different.pem"
    other.write_text(m.public_key_pem(other_public), encoding="utf-8")
    capsys.readouterr()
    code = cli.main(["admin", "manifest", "verify", str(manifest), "--key", str(other)])
    assert code == 1
    assert "does not match the trusted key" in capsys.readouterr().err


def write_prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text('date = "2026-10-14"\n\n[m]\ninput = 1.0\noutput = 2.0\n', encoding="utf-8")
    return path


def test_serve_refuses_a_manifest_without_authentication(tmp_path: Path, capsys) -> None:
    private, _ = keygen(tmp_path)
    manifest = sign(tmp_path, private)
    capsys.readouterr()
    code = cli.main(
        [
            "serve",
            "--prices",
            str(write_prices(tmp_path)),
            "--upstream",
            "http://example.invalid",
            "--manifest",
            str(manifest),
        ]
    )
    assert code == 2
    assert "needs authentication" in capsys.readouterr().err


def test_serve_refuses_a_tampered_manifest(tmp_path: Path, capsys) -> None:
    private, public = keygen(tmp_path)
    manifest = sign(tmp_path, private)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["policy"]["use_case"]["name"] = "evil"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()
    code = cli.main(
        [
            "serve",
            "--prices",
            str(write_prices(tmp_path)),
            "--upstream",
            "http://example.invalid",
            "--auth-token",
            "secret",
            "--manifest",
            str(manifest),
            "--manifest-key",
            str(public),
        ]
    )
    assert code == 2
    assert "refusing to start" in capsys.readouterr().err


def test_serve_refuses_manifest_key_without_manifest(tmp_path: Path, capsys) -> None:
    _private, public = keygen(tmp_path)
    capsys.readouterr()
    code = cli.main(
        [
            "serve",
            "--prices",
            str(write_prices(tmp_path)),
            "--upstream",
            "http://example.invalid",
            "--auth-token",
            "secret",
            "--manifest-key",
            str(public),
        ]
    )
    assert code == 2
    assert "--manifest-key needs --manifest" in capsys.readouterr().err
