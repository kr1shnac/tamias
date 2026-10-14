"""The optional user config file and provider presets (O1)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tamias import cli
from tamias.config import (
    CONFIG_ENV_VAR,
    PROVIDER_PRESETS,
    Config,
    ConfigError,
    default_config_path,
    load_config,
    pick,
    preset_names,
    preset_upstream,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_default_config_is_empty_not_an_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    cfg = load_config(None)
    assert cfg.upstream is None
    assert cfg.path == tmp_path / "absent.toml"


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")


def test_config_accepts_effort_mode(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path / "c.toml", 'effort_mode = "meter"\n'))
    assert cfg.effort_mode == "meter"


def test_default_path_uses_env_else_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "x.toml"))
    assert default_config_path() == tmp_path / "x.toml"
    monkeypatch.delenv(CONFIG_ENV_VAR)
    assert default_config_path() == Path.home() / ".tamias" / "config.toml"


def test_loads_and_coerces_all_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.toml",
        """
upstream = "https://example.test/api"
prices = "sheet.toml"
db = "log.db"
router_mode = "off"
cheap_model = "cheap"
strong_model = "strong"
effort_style = "openai"
host = "0.0.0.0"
port = 9000
min_gap = 3
inject_usage = true
""",
    )
    cfg = load_config(path)
    assert cfg.upstream == "https://example.test/api"
    assert cfg.port == 9000 and cfg.min_gap == 3
    assert cfg.inject_usage is True
    assert cfg.router_mode == "off"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", 'upstrem = "typo"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "upstrem" in str(exc.value)


def test_bad_type_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", 'port = "8000"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "port" in str(exc.value)


def test_unknown_provider_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", 'provider = "mystery"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_resolved_upstream_prefers_explicit_over_provider(tmp_path: Path) -> None:
    cfg = Config(upstream="https://explicit.test", provider="openai")
    assert cfg.resolved_upstream() == "https://explicit.test"
    cfg2 = Config(provider="openai")
    assert cfg2.resolved_upstream() == PROVIDER_PRESETS["openai"]["upstream"]


def test_preset_upstream_known_and_unknown() -> None:
    assert preset_upstream("openrouter") == PROVIDER_PRESETS["openrouter"]["upstream"]
    assert "openrouter" in preset_names()
    with pytest.raises(ConfigError):
        preset_upstream("nope")


def test_pick_precedence() -> None:
    assert pick("cli", "config", "default") == "cli"
    assert pick(None, "config", "default") == "config"
    assert pick("", "config", "default") == "config"
    assert pick(None, None, "default") == "default"


def _namespace(**kwargs) -> argparse.Namespace:
    base = {
        "config": None,
        "upstream": None,
        "provider": None,
        "router_mode": "shadow",
        "_argv": [],
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_resolve_upstream_cli_beats_env_config_and_provider(monkeypatch) -> None:
    monkeypatch.setenv("TAMIAS_UPSTREAM", "https://env.test")
    cfg = Config(upstream="https://config.test", provider="openai")
    args = _namespace(upstream="https://cli.test", provider="openrouter")
    assert cli._resolve_upstream(args, cfg) == "https://cli.test"


def test_resolve_upstream_env_beats_config(monkeypatch) -> None:
    monkeypatch.setenv("TAMIAS_UPSTREAM", "https://env.test")
    cfg = Config(upstream="https://config.test")
    assert cli._resolve_upstream(_namespace(), cfg) == "https://env.test"


def test_resolve_upstream_config_provider_preset(monkeypatch) -> None:
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    cfg = Config(provider="openai")
    assert cli._resolve_upstream(_namespace(), cfg) == PRESET_OPENAI


def test_resolve_router_mode_validates_config(monkeypatch) -> None:
    cfg = Config(router_mode="active")
    with pytest.raises(ConfigError):
        cli._resolve_router_mode(_namespace(), cfg)
    assert cli._resolve_router_mode(_namespace(), Config(router_mode="off")) == "off"
    assert cli._resolve_router_mode(_namespace(), Config()) == "shadow"


def test_config_path_command(monkeypatch, tmp_path: Path, capsys) -> None:
    target = tmp_path / "cfg.toml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(target))
    assert cli.main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(target)


def test_config_show_command(monkeypatch, tmp_path: Path, capsys) -> None:
    path = _write(tmp_path / "cfg.toml", 'provider = "openai"\nport = 9000\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "provider = 'openai'" in out
    assert "port = 9000" in out


def test_config_providers_command(capsys) -> None:
    assert cli.main(["config", "providers"]) == 0
    out = capsys.readouterr().out
    assert "openrouter" in out and "openai" in out


def test_config_show_rejects_bad_file(monkeypatch, tmp_path: Path, capsys) -> None:
    path = _write(tmp_path / "cfg.toml", 'nonsense = 1\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    assert cli.main(["config", "show"]) == 1
    assert "unknown config key" in capsys.readouterr().err


def test_serve_requires_upstream_without_config_or_env(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serve", "--db", "x.db", "--prices", "p.toml"]) == 2
    assert "an upstream is required" in capsys.readouterr().err


def test_run_uses_config_upstream(monkeypatch, tmp_path: Path) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    cfg = _write(tmp_path / "config.toml", 'upstream = "https://from-config.test/base"\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    monkeypatch.chdir(tmp_path)

    command: list[str] = []

    class _NeverStarts:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode or 0

    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: (command.extend(args), _NeverStarts())[1],
    )
    monkeypatch.setattr(cli, "_wait_for_proxy", lambda *args, **kwargs: False)

    assert cli.main(["run", "--prices", str(sheet), "--", "fake-child"]) == 1
    assert command[command.index("--upstream") + 1] == "https://from-config.test/base"


PRESET_OPENAI = PROVIDER_PRESETS["openai"]["upstream"]


def test_serve_applies_host_port_and_router_mode_from_config(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    cfg = _write(
        tmp_path / "config.toml", 'host = "127.0.0.1"\nport = 9999\nrouter_mode = "off"\n'
    )
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port, **kw: captured.update(host=host, port=port),
    )

    code = cli.main(
        [
            "serve",
            "--upstream",
            "http://upstream.invalid",
            "--prices",
            str(sheet),
            "--db",
            str(tmp_path / "r.db"),
        ]
    )
    assert code == 0
    assert captured == {"host": "127.0.0.1", "port": 9999}
    assert "off mode" in capsys.readouterr().err


def test_serve_flag_beats_config_port(monkeypatch, tmp_path: Path) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    cfg = _write(tmp_path / "config.toml", "port = 9999\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(cfg))

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port, **kw: captured.update(port=port),
    )
    code = cli.main(
        [
            "serve",
            "--upstream",
            "http://upstream.invalid",
            "--prices",
            str(sheet),
            "--db",
            str(tmp_path / "r.db"),
            "--port",
            "8123",
        ]
    )
    assert code == 0
    assert captured == {"port": 8123}


def _serve_args(tmp_path: Path, sheet: Path, *extra: str) -> list[str]:
    return [
        "serve",
        "--upstream",
        "http://upstream.invalid",
        "--prices",
        str(sheet),
        "--db",
        str(tmp_path / "r.db"),
        *extra,
    ]


def test_serve_rejects_tls_cert_without_key(monkeypatch, tmp_path: Path, capsys) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    assert cli.main(_serve_args(tmp_path, sheet, "--tls-cert", "cert.pem")) == 2
    assert "must be given together" in capsys.readouterr().err


def test_serve_rejects_missing_tls_files(monkeypatch, tmp_path: Path, capsys) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    code = cli.main(
        _serve_args(tmp_path, sheet, "--tls-cert", "nope.pem", "--tls-key", "nope.key")
    )
    assert code == 2
    assert "file not found" in capsys.readouterr().err


def test_serve_passes_tls_to_uvicorn(monkeypatch, tmp_path: Path, capsys) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    cert = _write(tmp_path / "cert.pem", "cert")
    key = _write(tmp_path / "key.pem", "key")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: captured.update(kw)
    )

    code = cli.main(_serve_args(tmp_path, sheet, "--tls-cert", str(cert), "--tls-key", str(key)))
    assert code == 0
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)
    err = capsys.readouterr().err
    assert "https://127.0.0.1:8000" in err
    assert "TLS is ON" in err


def test_serve_without_tls_does_not_pass_ssl(monkeypatch, tmp_path: Path) -> None:
    sheet = _write(tmp_path / "prices.toml", 'date = "2026-10-06"\n[model]\ninput=1\noutput=1\n')
    captured: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    assert cli.main(_serve_args(tmp_path, sheet)) == 0
    assert "ssl_certfile" not in captured
