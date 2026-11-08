"""Tests for ``tamias opencode uninstall``.

The claim these tests defend: uninstall removes only what tamias registered — its
provider block, its plugin entries and its slash commands — and leaves every
other provider, plugin and command exactly where it was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tamias.cli import main


def plugin_dir(tmp_path: Path, name: str = "tamias-opencode-plugin") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "package.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (path / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    return path


def write_config(path: Path, *providers: str) -> None:
    path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    name: {"name": name} for name in providers
                },
            }
        ),
        encoding="utf-8",
    )


def uninstall(*extra: str) -> int:
    return main(["opencode", "uninstall", *extra])


def test_uninstall_removes_only_tamias_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("TAMIAS_HOME", str(tmp_path / "home"))

    config = tmp_path / "opencode.json"
    write_config(config, "tamias", "someone-else")

    cli_path = config_home / "opencode" / "cli.json"
    cli_path.parent.mkdir(parents=True)
    explicit = plugin_dir(tmp_path)
    staged = tmp_path / "home" / "plugin"
    staged.mkdir(parents=True)
    (staged / "package.json").write_text(
        json.dumps({"name": "tamias-opencode-plugin"}), encoding="utf-8"
    )
    other = plugin_dir(tmp_path, "someone-elses-plugin")
    cli_path.write_text(
        json.dumps({"plugins": [str(explicit), str(staged), str(other)]}),
        encoding="utf-8",
    )

    commands = config_home / "opencode" / "commands"
    commands.mkdir(parents=True)
    for name in ("tamias.md", "tamias-report.md", "someone-else.md"):
        (commands / name).write_text("command\n", encoding="utf-8")

    assert uninstall("--config", str(config), "--plugin", str(explicit)) == 0

    document = json.loads(config.read_text())
    assert "tamias" not in document["provider"]
    assert "someone-else" in document["provider"]
    assert json.loads(cli_path.read_text())["plugins"] == [str(other)]
    assert not (commands / "tamias.md").exists()
    assert not (commands / "tamias-report.md").exists()
    assert (commands / "someone-else.md").is_file()
    assert not staged.exists()
    assert list(tmp_path.glob("opencode.json.bak-*"))
    assert list((config_home / "opencode").glob("cli.json.bak-*"))


def test_a_dry_run_reports_without_removing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("TAMIAS_HOME", str(tmp_path / "home"))

    config = tmp_path / "opencode.json"
    write_config(config, "tamias")
    before = config.read_bytes()
    cli_path = config_home / "opencode" / "cli.json"
    cli_path.parent.mkdir(parents=True)
    plugin = plugin_dir(tmp_path)
    cli_path.write_text(json.dumps({"plugins": [str(plugin)]}), encoding="utf-8")
    commands = config_home / "opencode" / "commands"
    commands.mkdir(parents=True)
    (commands / "tamias.md").write_text("command\n", encoding="utf-8")

    assert uninstall("--config", str(config), "--plugin", str(plugin), "--dry-run") == 0

    assert config.read_bytes() == before
    assert json.loads(cli_path.read_text())["plugins"] == [str(plugin)]
    assert (commands / "tamias.md").is_file()
    assert "would remove" in capsys.readouterr().out


def test_keep_plugin_unregisters_but_leaves_the_staged_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TAMIAS_HOME", str(tmp_path / "home"))
    staged = tmp_path / "home" / "plugin"
    staged.mkdir(parents=True)
    (staged / "package.json").write_text(
        json.dumps({"name": "tamias-opencode-plugin"}), encoding="utf-8"
    )
    cli_path = tmp_path / "config" / "opencode" / "cli.json"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(json.dumps({"plugins": [str(staged)]}), encoding="utf-8")

    assert uninstall("--config", str(tmp_path / "missing.json"), "--keep-plugin") == 0

    assert staged.is_dir()
    assert "plugins" not in json.loads(cli_path.read_text())


def test_nothing_installed_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TAMIAS_HOME", str(tmp_path / "home"))
    config = tmp_path / "opencode.json"
    write_config(config, "someone-else")

    assert uninstall("--config", str(config)) == 0

    assert "nothing from tamias is registered" in capsys.readouterr().out
    assert json.loads(config.read_text())["provider"] == {
        "someone-else": {"name": "someone-else"}
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_jsonc_comments_are_backed_up_not_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TAMIAS_HOME", str(tmp_path / "home"))
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        '{\n  // a comment JSON does not allow\n  "provider": {"tamias": {}}\n}\n',
        encoding="utf-8",
    )

    assert uninstall("--config", str(config)) == 0

    assert "provider" not in json.loads(config.read_text())
    backup = next(tmp_path.glob("opencode.jsonc.bak-*"))
    assert "// a comment" in backup.read_text()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "not preserved" in capsys.readouterr().err
