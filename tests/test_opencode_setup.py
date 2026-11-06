"""Tests for ``tamias opencode setup``.

The claim these tests defend: one command can point OpenCode at tamias without
destroying the config the user already has, and it says so when it cannot keep
something (JSONC comments) or cannot find the plugin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tamias import cli
from tamias.cli import main

PRICES = """
date = "2026-01-05"

["free-model"]
input = 0.0
output = 0.0
"""


def setup_args(config: Path, prices: Path, *extra: str) -> list[str]:
    return [
        "opencode",
        "setup",
        "--config",
        str(config),
        "--prices",
        str(prices),
        "--port",
        "8123",
        *extra,
    ]


def write_prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES, encoding="utf-8")
    return path


def test_setup_creates_a_config_with_the_provider_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "opencode.json"

    assert main(setup_args(config, write_prices(tmp_path))) == 0

    document = json.loads(config.read_text())
    tamias = document["provider"]["tamias"]
    assert tamias["name"] == "tamias"
    assert tamias["options"]["baseURL"] == "http://127.0.0.1:8123/v1"
    assert tamias["options"]["apiKey"], "OpenCode hides a provider with no credential"
    assert "free-model" in tamias["models"]


def test_setup_keeps_every_other_key_in_the_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"other": {"type": "remote", "url": "https://example.test"}},
                "provider": {"someone-else": {"npm": "x", "name": "other"}},
            }
        ),
        encoding="utf-8",
    )

    assert main(setup_args(config, write_prices(tmp_path))) == 0

    document = json.loads(config.read_text())
    assert document["$schema"] == "https://opencode.ai/config.json"
    assert "other" in document["mcp"]
    assert "someone-else" in document["provider"]
    assert "tamias" in document["provider"]


def test_setup_strips_jsonc_comments_but_keeps_a_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "opencode.jsonc"
    # A config that already holds a credential is usually readable by the
    # owner only, but nothing enforces that; the backup must not widen it.
    config.write_text(
        "{\n  // a comment that JSON does not allow\n  \"$schema\": \"https://opencode.ai/config.json\",\n}\n",
        encoding="utf-8",
    )
    config.chmod(0o644)

    assert main(setup_args(config, write_prices(tmp_path))) == 0

    assert json.loads(config.read_text())["$schema"] == "https://opencode.ai/config.json"
    backups = list(tmp_path.glob("opencode.jsonc.bak-*"))
    assert len(backups) == 1
    assert "// a comment" in backups[0].read_text()
    assert backups[0].stat().st_mode & 0o777 == 0o600, "a backup may hold a secret"
    error = capsys.readouterr().err
    assert "not preserved" in error
    assert "may contain provider credentials" in error


def test_a_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "opencode.json"

    assert main(setup_args(config, write_prices(tmp_path), "--dry-run")) == 0

    assert not config.exists()
    assert "would write" in capsys.readouterr().out


def test_plugin_directory_prefers_the_copy_inside_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel has no source checkout beside it; the bundled copy must win.

    `pip install tamias` puts the pop-up at ``tamias/plugin`` inside
    site-packages.  If lookup only walked parent directories it would find the
    interpreter's own tree, not the plugin, and setup would quietly skip it.
    """
    module = tmp_path / "site-packages" / "tamias"
    (module / "plugin" / "src").mkdir(parents=True)
    (module / "plugin" / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "__file__", str(module / "cli.py"))

    assert cli._plugin_directory(None) == module / "plugin"


def test_an_explicit_plugin_path_beats_the_bundled_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "site-packages" / "tamias"
    (module / "plugin").mkdir(parents=True)
    (module / "plugin" / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(module / "cli.py"))
    chosen = tmp_path / "elsewhere"
    chosen.mkdir()

    assert cli._plugin_directory(str(chosen)) == chosen


def test_plugin_directory_is_none_when_no_copy_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "site-packages" / "tamias"
    module.mkdir(parents=True)
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "__file__", str(module / "cli.py"))

    assert cli._plugin_directory(None) is None


def _bundled_plugin(tmp_path: Path) -> Path:
    module = tmp_path / "site-packages" / "tamias"
    plugin = module / "plugin"
    (plugin / "src").mkdir(parents=True)
    (plugin / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    (plugin / "package.json").write_text('{"name":"tamias-opencode-plugin"}')
    (plugin / "tsconfig.json").write_text("{}")
    (plugin / "src" / "index.ts").write_text("export const x = 1\n")
    return plugin


def test_a_bundled_copy_without_peers_is_installed_with_bun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pip install tamias` ships TSX only; the host resolves peers beside it.

    OpenCode 2.2.5 refuses a plugin directory with no `node_modules` next to it
    ("Cannot find package 'solid-js'"), so registering the wheel copy as-is
    leaves a permanent `1 plugin failed` toast.  Setup must install the peers
    into a directory tamias owns and register that instead.
    """
    plugin = _bundled_plugin(tmp_path)
    monkeypatch.setattr(cli, "__file__", str(plugin.parent / "cli.py"))
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "PLUGIN_INSTALL_DIR", tmp_path / "data" / "plugin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bun")
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001 - subprocess shape
        calls.append(list(command))
        (cli.PLUGIN_INSTALL_DIR / "node_modules" / cli.PLUGIN_RUNTIME_PEER).mkdir(
            parents=True, exist_ok=True
        )
        return Done()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    resolved, note = cli._prepare_plugin(None)

    assert note is None
    assert resolved == cli.PLUGIN_INSTALL_DIR
    assert (resolved / "tui.tsx").is_file(), "the plugin source must be copied"
    assert (resolved / "package.json").is_file()
    assert calls and calls[0][0] == "bun" and calls[0][1] == "install"


def test_a_bundled_copy_without_bun_says_so_and_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _bundled_plugin(tmp_path)
    monkeypatch.setattr(cli, "__file__", str(plugin.parent / "cli.py"))
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(cli, "PLUGIN_INSTALL_DIR", tmp_path / "data" / "plugin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    resolved, note = cli._prepare_plugin(None)

    assert resolved is None
    assert "Bun" in note
    assert not cli.PLUGIN_INSTALL_DIR.exists(), "no half-installed plugin"


def test_a_directory_that_already_has_peers_is_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _bundled_plugin(tmp_path)
    (plugin / "node_modules" / cli.PLUGIN_RUNTIME_PEER).mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(plugin.parent / "cli.py"))
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)

    resolved, note = cli._prepare_plugin(None)

    assert resolved == plugin
    assert note is None


def test_an_explicit_plugin_without_peers_is_still_registered_but_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "tamias-opencode-plugin"
    chosen.mkdir()
    (chosen / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.delenv("TAMIAS_PLUGIN_DIR", raising=False)

    resolved, note = cli._prepare_plugin(str(chosen))

    assert resolved == chosen, "the operator named it; honour it"
    assert "bun install" in note


def test_setup_registers_the_plugin_in_the_tui_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = tmp_path / "tamias-opencode-plugin"
    plugin.mkdir()
    (plugin / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    config = tmp_path / "opencode.json"
    assert (
        main(setup_args(config, write_prices(tmp_path), "--plugin", str(plugin))) == 0
    )

    cli_path = tmp_path / "config" / "opencode" / "cli.json"
    assert json.loads(cli_path.read_text())["plugins"] == [str(plugin)]


def test_setup_does_not_add_the_same_plugin_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = tmp_path / "tamias-opencode-plugin"
    plugin.mkdir()
    (plugin / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cli_path = tmp_path / "config" / "opencode" / "cli.json"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text(json.dumps({"plugins": [str(plugin)]}), encoding="utf-8")
    config = tmp_path / "opencode.json"

    assert main(setup_args(config, write_prices(tmp_path), "--plugin", str(plugin))) == 0

    assert json.loads(cli_path.read_text())["plugins"] == [str(plugin)]


def test_install_keeps_tamias_for_the_plugin_and_names_the_report_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "tamias-opencode-plugin"
    plugin.mkdir()
    (plugin / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert (
        main(
            [
                "opencode",
                "install",
                "--db",
                str(tmp_path / "requests.db"),
                "--prices",
                str(write_prices(tmp_path)),
                "--plugin",
                str(plugin),
            ]
        )
        == 0
    )

    commands = tmp_path / "config" / "opencode" / "commands"
    assert (commands / "tamias-report.md").is_file()
    assert not (commands / "tamias.md").exists()


def test_install_without_the_plugin_keeps_the_tamias_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_plugin_directory", lambda explicit: None)

    assert (
        main(
            [
                "opencode",
                "install",
                "--db",
                str(tmp_path / "requests.db"),
                "--prices",
                str(write_prices(tmp_path)),
            ]
        )
        == 0
    )

    commands = tmp_path / "config" / "opencode" / "commands"
    assert (commands / "tamias.md").is_file()
    assert not (commands / "tamias-report.md").exists()


def test_setup_warns_when_a_legacy_command_shadows_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = tmp_path / "tamias-opencode-plugin"
    plugin.mkdir()
    (plugin / "tui.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    legacy = tmp_path / "config" / "opencode" / "commands" / "tamias.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old command\n", encoding="utf-8")

    assert (
        main(
            setup_args(
                tmp_path / "opencode.json",
                write_prices(tmp_path),
                "--plugin",
                str(plugin),
            )
        )
        == 0
    )

    assert "also registers /tamias" in capsys.readouterr().err


def test_setup_refuses_when_there_is_no_model_to_offer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prices = tmp_path / "prices.toml"
    prices.write_text('date = "2026-01-05"\n\n["paid"]\ninput = 1.0\noutput = 2.0\n')

    assert main(setup_args(tmp_path / "opencode.json", prices)) == 2
    assert not (tmp_path / "opencode.json").exists()


def test_the_serve_command_names_the_provider_catalogue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One endpoint in front of every provider is the v1 shape.

    The single-upstream `--provider opencode` preset points at Zen alone, which
    is not what a multi-provider setup is meant to serve.
    """
    providers = tmp_path / "providers.toml"
    providers.write_text('[[providers]]\nname = "nvidia"\n', encoding="utf-8")

    assert (
        main(
            setup_args(
                tmp_path / "opencode.json",
                write_prices(tmp_path),
                "--providers",
                str(providers),
            )
        )
        == 0
    )

    out = capsys.readouterr().out
    assert f"--providers {providers}" in out
    assert "--provider opencode" not in out


def test_without_a_catalogue_the_serve_command_falls_back_to_the_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert main(setup_args(tmp_path / "opencode.json", write_prices(tmp_path))) == 0

    assert "--provider opencode" in capsys.readouterr().out
