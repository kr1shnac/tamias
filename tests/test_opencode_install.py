"""Tests for ``tamias opencode install``.

The installer writes a Markdown command file and prints a provider block.  It
never edits ``opencode.json``.  These tests pin the file it writes, the model
map it derives from the registry or price sheet, and the refusal to overwrite
without ``--force``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.cli import main

PRICES = """
date = "2026-01-05"

[free-model]
input = 0.0
output = 0.0

[paid-model]
input = 3.0
output = 15.0
"""


def install_args(project: Path, db: Path, prices: Path, *extra: str) -> list[str]:
    return [
        "opencode",
        "install",
        "--db",
        str(db),
        "--prices",
        str(prices),
        "--port",
        "8123",
        "--project",
        str(project),
        *extra,
    ]


def test_project_install_writes_the_slash_command_and_model_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    code = main(install_args(tmp_path, db, prices))

    assert code == 0
    target = tmp_path / ".opencode" / "commands" / "tamias.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "description: Show tamias savings, tokens and routing status" in text
    assert "!`" in text
    assert " report --db " in text
    assert str(db) in text
    assert str(prices) in text
    assert "Never invent a dollar figure when the report says UNKNOWN" in text

    out = capsys.readouterr().out
    assert f"wrote {target}" in out
    assert "http://127.0.0.1:8123/v1" in out
    assert "--router-mode active" in out
    assert "--min-gap 0" in out
    # Only free models are offered when no registry is given, so a large paid
    # sheet does not flood OpenCode's picker.
    assert '"models":{"free-model":{"name":"free-model"}}' in out
    assert "paid-model" not in out


def test_models_come_from_the_registry_when_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")
    registry = tmp_path / "models.toml"
    registry.write_text(
        """
[[models]]
id = "registry-model"
provider = "opencode"
context_window = 100000
tools = true
""",
        encoding="utf-8",
    )

    code = main(
        install_args(
            tmp_path,
            db,
            prices,
            "--registry",
            str(registry),
            "--cheap-model",
            "registry-model",
        )
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"models":{"registry-model":{"name":"registry-model"}}' in out
    assert "free-model" not in out


def test_paths_with_spaces_are_shell_quoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "my project"
    db = project / "requests.db"
    prices = project / "prices.toml"
    prices.parent.mkdir(parents=True)
    prices.write_text(PRICES, encoding="utf-8")

    assert main(install_args(project, db, prices)) == 0

    text = (project / ".opencode" / "commands" / "tamias.md").read_text(encoding="utf-8")
    assert "--db '" in text
    assert "my project/requests.db'" in text


def test_install_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    assert main(install_args(tmp_path, db, prices)) == 0
    assert main(install_args(tmp_path, db, prices)) == 2

    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "--force" in captured.err


def test_force_replaces_an_existing_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")
    target = tmp_path / ".opencode" / "commands" / "tamias.md"
    target.parent.mkdir(parents=True)
    target.write_text("old command\n", encoding="utf-8")

    assert main(install_args(tmp_path, db, prices, "--force")) == 0

    assert "old command" not in target.read_text(encoding="utf-8")
    assert " report --db " in target.read_text(encoding="utf-8")


def test_no_models_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text('date = "2026-01-05"\n', encoding="utf-8")

    assert main(install_args(tmp_path, db, prices)) == 2

    captured = capsys.readouterr()
    assert "no models to offer" in captured.err
    assert not (tmp_path / ".opencode" / "commands" / "tamias.md").exists()


def test_global_install_honours_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    args = [
        "opencode",
        "install",
        "--db",
        str(db),
        "--prices",
        str(prices),
    ]
    assert main(args) == 0

    target = tmp_path / "cfg" / "opencode" / "commands" / "tamias.md"
    assert target.is_file()


def test_cheap_model_accepts_a_comma_separated_pool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    code = main(
        install_args(
            tmp_path,
            db,
            prices,
            "--cheap-model",
            "free-model, other-free,free-model",
        )
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"free-model":{"name":"free-model"}' in out
    assert '"other-free":{"name":"other-free"}' in out
    assert '"free-model, other-free' not in out
    assert "--cheap-model free-model,other-free" in out


def test_inr_rate_is_embedded_in_the_slash_command(
    tmp_path: Path,
) -> None:
    db = tmp_path / "requests.db"
    prices = tmp_path / "prices.toml"
    prices.write_text(PRICES, encoding="utf-8")

    assert main(install_args(tmp_path, db, prices, "--inr-rate", "85")) == 0

    text = (tmp_path / ".opencode" / "commands" / "tamias.md").read_text(
        encoding="utf-8"
    )
    assert " report --db " in text
    assert "--inr-rate 85" in text
