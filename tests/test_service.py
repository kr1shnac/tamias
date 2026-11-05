"""Tests for the systemd user-service installer.

The installer writes a unit file; it never starts anything unless ``--start``
is explicit.  These tests pin the generated ExecStart (absolute paths, the
routing flags, systemd-safe quoting), the overwrite guard, the active-mode
prerequisites, and the refusal to remove a unit tamias did not write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias import cli
from tamias.cli import SERVICE_MARKER, main, render_service_unit

UNIT = Path("systemd") / "user" / "tamias.service"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    prices = tmp_path / "prices.toml"
    prices.write_text(
        'date = "2026-01-01"\n'
        "\n[free-a]\ninput = 0.0\noutput = 0.0\n"
        "\n[free-b]\ninput = 0.0\noutput = 0.0\n",
        encoding="utf-8",
    )
    registry = tmp_path / "models.toml"
    registry.write_text(
        '[[models]]\nid = "free-a"\nprovider = "opencode"\n'
        "context_window = 100000\ntools = true\n"
        '\n[[models]]\nid = "free-b"\nprovider = "opencode"\n'
        "context_window = 100000\ntools = true\n",
        encoding="utf-8",
    )
    return {
        "tmp": tmp_path,
        "config": tmp_path / "config",
        "prices": prices,
        "registry": registry,
        "db": tmp_path / "requests.db",
    }


def install_args(paths: dict[str, Path], *extra: str) -> list[str]:
    return [
        "service",
        "install",
        "--db",
        str(paths["db"]),
        "--prices",
        str(paths["prices"]),
        "--registry",
        str(paths["registry"]),
        "--cheap-model",
        "free-a,free-b",
        "--exec",
        "/opt/tamias",
        *extra,
    ]


def test_install_writes_a_systemd_user_unit(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(install_args(paths)) == 0

    target = paths["config"] / UNIT
    text = target.read_text(encoding="utf-8")
    assert text.startswith(SERVICE_MARKER)
    assert "Description=tamias model router for OpenCode" in text
    assert "ExecStart=/opt/tamias serve --provider opencode" in text
    assert f"--db {paths['db'].resolve()}" in text
    assert f"--prices {paths['prices'].resolve()}" in text
    assert f"--registry {paths['registry'].resolve()}" in text
    assert "--router-mode active" in text
    assert "--min-gap 0" in text
    assert "--cheap-model free-a,free-b" in text
    assert "Restart=on-failure" in text

    out = capsys.readouterr().out
    assert f"wrote {target}" in out
    assert "systemctl --user enable --now tamias.service" in out


def test_install_can_route_across_a_provider_catalogue(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    providers = paths["tmp"] / "providers.toml"
    providers.write_text(
        '[[providers]]\nname = "one"\nbase_url = "https://one.example/v1"\n'
        "api_key_env = \"ONE_KEY\"\n",
        encoding="utf-8",
    )

    assert main(install_args(paths, "--providers", str(providers))) == 0
    text = (paths["config"] / UNIT).read_text(encoding="utf-8")

    assert f"--providers {providers.resolve()}" in text
    assert "--provider opencode" not in text
    assert "systemctl --user enable --now tamias.service" in capsys.readouterr().out


def test_install_refuses_a_missing_provider_catalogue(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    absent = paths["tmp"] / "absent-providers.toml"
    assert main(install_args(paths, "--providers", str(absent))) == 2
    assert "provider catalogue does not exist" in capsys.readouterr().err


def test_install_refuses_to_overwrite_without_force(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(install_args(paths)) == 0
    assert main(install_args(paths)) == 2

    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "--force" in captured.err


def test_force_replaces_the_unit(paths: dict[str, Path]) -> None:
    target = paths["config"] / UNIT
    target.parent.mkdir(parents=True)
    target.write_text("old unit\n", encoding="utf-8")

    assert main(install_args(paths, "--force")) == 0
    assert SERVICE_MARKER in target.read_text(encoding="utf-8")


def test_dry_run_prints_the_unit_without_writing_it(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(install_args(paths, "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "ExecStart=/opt/tamias serve" in out
    assert not (paths["config"] / UNIT).exists()


def test_active_install_requires_a_target_and_registry(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "service",
        "install",
        "--db",
        str(paths["db"]),
        "--prices",
        str(paths["prices"]),
        "--exec",
        "/opt/tamias",
    ]
    assert main(common) == 2
    assert "needs --cheap-model" in capsys.readouterr().err

    assert main([*common, "--cheap-model", "free-a"]) == 2
    assert "needs --registry" in capsys.readouterr().err


def test_shadow_install_needs_no_target_or_registry(
    paths: dict[str, Path],
) -> None:
    args = [
        "service",
        "install",
        "--db",
        str(paths["db"]),
        "--prices",
        str(paths["prices"]),
        "--exec",
        "/opt/tamias",
        "--router-mode",
        "shadow",
    ]
    assert main(args) == 0
    text = (paths["config"] / UNIT).read_text(encoding="utf-8")
    assert "--router-mode shadow" in text
    assert "--cheap-model" not in text
    assert "--registry" not in text


def test_missing_input_files_are_a_usage_error(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    args = install_args(paths)
    args[args.index("--prices") + 1] = str(paths["tmp"] / "absent.toml")
    assert main(args) == 2
    assert "price sheet does not exist" in capsys.readouterr().err


def test_paths_with_spaces_are_quoted_for_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config dir"))
    prices = tmp_path / "my prices" / "prices.toml"
    prices.parent.mkdir()
    prices.write_text('date = "2026-01-01"\n', encoding="utf-8")
    db = tmp_path / "my data" / "requests.db"
    db.parent.mkdir()
    unit = render_service_unit(
        "/opt/tamias",
        db=str(db),
        prices=str(prices),
        registry=None,
        cheap_model="",
        port=8000,
        min_gap=0,
        provider="opencode",
        router_mode="shadow",
    )
    assert f"--prices '{prices}'" in unit
    assert f"--db '{db}'" in unit


def test_percent_is_escaped_for_systemd() -> None:
    unit = render_service_unit(
        "/opt/tamias",
        db="/tmp/a%b/requests.db",
        prices="/tmp/prices.toml",
        registry=None,
        cheap_model="",
        port=8000,
        min_gap=0,
        provider="opencode",
        router_mode="shadow",
    )
    assert "/tmp/a%%b/requests.db" in unit


def test_control_characters_are_rejected() -> None:
    with pytest.raises(ValueError, match="control character"):
        render_service_unit(
            "/opt/tamias",
            db="/tmp/a\nb/requests.db",
            prices="/tmp/prices.toml",
            registry=None,
            cheap_model="",
            port=8000,
            min_gap=0,
            provider="opencode",
            router_mode="shadow",
        )


def test_start_runs_systemctl_after_writing(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **_: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert main(install_args(paths, "--start")) == 0
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tamias.service"],
    ]


def test_uninstall_removes_the_generated_unit(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(install_args(paths)) == 0
    assert main(["service", "uninstall", "--keep-running"]) == 0
    assert not (paths["config"] / UNIT).exists()
    assert "removed" in capsys.readouterr().out


def test_uninstall_refuses_an_unmanaged_unit(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    target = paths["config"] / UNIT
    target.parent.mkdir(parents=True)
    target.write_text("someone else's unit\n", encoding="utf-8")

    assert main(["service", "uninstall", "--keep-running"]) == 2
    assert target.exists()
    assert "was not written by tamias" in capsys.readouterr().err

    assert main(["service", "uninstall", "--keep-running", "--force"]) == 0
    assert not target.exists()


def test_uninstall_dry_run_keeps_the_unit(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(install_args(paths)) == 0
    assert main(["service", "uninstall", "--dry-run"]) == 0
    assert (paths["config"] / UNIT).exists()
    assert "would remove" in capsys.readouterr().out


def test_non_linux_is_a_usage_error(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert main(install_args(paths)) == 2
    assert "Linux-only" in capsys.readouterr().err


def test_active_install_refuses_a_target_outside_the_registry(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    args = install_args(paths)
    args[args.index("--cheap-model") + 1] = "free-a,not-there"
    assert main(args) == 2
    assert "not a tool-capable entry" in capsys.readouterr().err
    assert not (paths["config"] / UNIT).exists()


def test_active_install_refuses_an_unpriced_target(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    paths["registry"].write_text(
        paths["registry"].read_text(encoding="utf-8")
        + '\n[[models]]\nid = "free-c"\nprovider = "opencode"\n'
        "context_window = 100000\ntools = true\n",
        encoding="utf-8",
    )
    args = install_args(paths)
    args[args.index("--cheap-model") + 1] = "free-a,free-c"
    assert main(args) == 2
    assert "is not priced" in capsys.readouterr().err
    assert not (paths["config"] / UNIT).exists()
