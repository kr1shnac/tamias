"""Secret delivery for the shared token (council run 20261020-1500 item [15]).

``--auth-token VALUE`` is the convenient form and the leaky one: argv shows up
in ``ps``, the shell history and CI logs.  ``$TAMIAS_AUTH_TOKEN`` already
avoided argv; ``$TAMIAS_AUTH_TOKEN_FILE`` adds a ``chmod 600`` file for
deployments that would rather not put the secret in the environment either.
These tests pin the precedence and the "no silent empty secret" behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias import cli  # noqa: E402


def _args(token: str | None = None):
    return cli.build_parser().parse_args(
        ["serve", "--upstream", "http://x", "--prices", "p.toml", "--db", "d.db"]
        + (["--auth-token", token] if token else [])
    )


def test_flag_wins(monkeypatch) -> None:
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN", "from-env")
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN_FILE", "/nonexistent")
    assert cli.resolve_auth_token(_args("from-flag")) == "from-flag"


def test_env_wins_over_file(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN", "from-env")
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN_FILE", str(token_file))
    assert cli.resolve_auth_token(_args()) == "from-env"


def test_file_form_is_read_and_trimmed(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("  from-file  \n", encoding="utf-8")
    monkeypatch.delenv("TAMIAS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN_FILE", str(token_file))
    assert cli.resolve_auth_token(_args()) == "from-file"


def test_an_empty_file_is_no_token_not_an_empty_secret(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("\n", encoding="utf-8")
    monkeypatch.delenv("TAMIAS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN_FILE", str(token_file))
    assert cli.resolve_auth_token(_args()) is None


def test_a_missing_file_warns_and_yields_no_token(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("TAMIAS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TAMIAS_AUTH_TOKEN_FILE", str(tmp_path / "absent"))
    assert cli.resolve_auth_token(_args()) is None
    assert "TAMIAS_AUTH_TOKEN_FILE" in capsys.readouterr().err


def test_no_source_at_all_is_none(monkeypatch) -> None:
    monkeypatch.delenv("TAMIAS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TAMIAS_AUTH_TOKEN_FILE", raising=False)
    assert cli.resolve_auth_token(_args()) is None


def test_passing_the_token_on_argv_warns(monkeypatch, tmp_path, capsys) -> None:
    """serve must say the token is exposed before the shell history does."""
    prices = tmp_path / "p.toml"
    prices.write_text('date = "2026-10-04"\nsimulated = true\n', encoding="utf-8")

    class _FakeUvicorn:
        @staticmethod
        def run(app, host: str, port: int, log_level: str, **kwargs: object) -> None:
            return None

    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
    code = cli.main(
        [
            "serve",
            "--upstream",
            "http://127.0.0.1:9",
            "--prices",
            str(prices),
            "--db",
            str(tmp_path / "d.db"),
            "--auth-token",
            "shh",
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "command line" in err
    assert "TAMIAS_AUTH_TOKEN_FILE" in err
