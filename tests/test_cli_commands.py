from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli
from tamias.store import Store
from tamias.types import CostBreakdown, Decision, Usage


def test_prune_keeps_the_newest_rows(tmp_path: Path, capsys) -> None:
    db = tmp_path / "log.sqlite"
    store = Store(db)
    try:
        for index in range(4):
            store.log_request(
                f"2026-10-04T12:00:0{index}Z",
                "session-1",
                "model-a",
                "model-a",
                Usage(None, None, None, None),
                CostBreakdown(None, "no pricing", "2026-10-04"),
                1,
                "200",
                Decision(action="STAY", target_model=None, reason="ok"),
            )
    finally:
        store.close()

    assert cli.main(["prune", "--db", str(db), "--keep-rows", "1"]) == 0
    assert "deleted 3 rows; 1 remain" in capsys.readouterr().out

    assert cli.main(["prune", "--db", str(db), "--keep-days", "30", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would delete 0 rows; 1 remain" in out


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_run_sets_project_base_urls_reports_and_cleans_up(tmp_path: Path, capsys) -> None:
    prices = tmp_path / "prices.toml"
    prices.write_text('date = "2026-10-06"\n[model]\ninput = 1\noutput = 1\n', encoding="utf-8")
    child_result = tmp_path / "child-result.json"
    child = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(child_result)!r}).write_text(json.dumps({{\n"
        "    'openai': os.environ['OPENAI_BASE_URL'],\n"
        "    'anthropic': os.environ['ANTHROPIC_BASE_URL'],\n"
        "}))\n"
        "raise SystemExit(23)\n"
    )

    code = cli.main(
        [
            "run",
            "--mode",
            "shadow",
            "--project",
            "work",
            "--upstream",
            f"http://127.0.0.1:{_unused_port()}",
            "--prices",
            str(prices),
            "--db",
            str(tmp_path / "run.db"),
            "--",
            sys.executable,
            "-c",
            child,
        ]
    )

    bases = json.loads(child_result.read_text(encoding="utf-8"))
    assert code == 23
    assert bases["openai"].endswith("/p/work/v1")
    assert bases["anthropic"].endswith("/p/work")
    assert bases["openai"].removesuffix("/v1") == bases["anthropic"]
    port = int(bases["anthropic"].split(":")[2].split("/")[0])
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)
    assert "requests: 0" in capsys.readouterr().out


def test_run_reports_a_proxy_start_timeout(monkeypatch, tmp_path: Path, capsys) -> None:
    class NeverStarts:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode or 0

        def kill(self) -> None:
            self.returncode = -9

    prices = tmp_path / "prices.toml"
    prices.write_text('date = "2026-10-06"\n[model]\ninput = 1\noutput = 1\n', encoding="utf-8")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: NeverStarts())
    monkeypatch.setattr(cli, "_wait_for_proxy", lambda *args, **kwargs: False)

    assert (
        cli.main(
            [
                "run",
                "--upstream",
                "http://127.0.0.1:1",
                "--prices",
                str(prices),
                "--db",
                str(tmp_path / "run.db"),
                "--",
                "fake-child",
            ]
        )
        == 1
    )
    assert (
        "tamias run: proxy did not accept connections within 15 seconds" in capsys.readouterr().err
    )


class _NeverStarts:
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def _capture_serve_command(monkeypatch) -> list[str]:
    """Record the serve argv `run` would start and make the proxy never answer."""
    command: list[str] = []
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: (command.extend(args), _NeverStarts())[1],
    )
    monkeypatch.setattr(cli, "_wait_for_proxy", lambda *args, **kwargs: False)
    return command


def _write_prices(directory: Path) -> Path:
    sheet = directory / "prices.toml"
    sheet.write_text('date = "2026-10-06"\n[model]\ninput = 1\noutput = 1\n', encoding="utf-8")
    return sheet


def test_run_passes_router_mode_and_min_gap(monkeypatch, tmp_path: Path) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    command = _capture_serve_command(monkeypatch)

    code = cli.main(
        [
            "run",
            "--router-mode",
            "active",
            "--min-gap",
            "7",
            "--",
            "fake-child",
        ]
    )

    assert code == 1
    assert command[command.index("--router-mode") + 1] == "active"
    assert command[command.index("--min-gap") + 1] == "7"


def test_run_keeps_the_deprecated_mode_alias(monkeypatch, tmp_path: Path, capsys) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--mode", "off", "--", "fake-child"]) == 1
    assert command[command.index("--router-mode") + 1] == "off"
    assert "--mode is deprecated" in capsys.readouterr().err


def test_run_rejects_disagreeing_router_mode_and_mode(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    command = _capture_serve_command(monkeypatch)

    code = cli.main(
        ["run", "--router-mode", "active", "--mode", "off", "--", "fake-child"]
    )

    assert code == 2
    assert command == []
    assert "disagree" in capsys.readouterr().err


def test_run_does_not_pass_min_gap_when_unset(monkeypatch, tmp_path: Path) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert "--min-gap" not in command


def test_run_defaults_prices_and_upstream(monkeypatch, tmp_path: Path) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert command[command.index("--prices") + 1] == "prices.toml"
    assert command[command.index("--upstream") + 1] == cli.DEFAULT_UPSTREAM


def test_run_upstream_defaults_to_the_tamias_upstream_variable(monkeypatch, tmp_path: Path) -> None:
    _write_prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TAMIAS_UPSTREAM", "https://upstream.example/base")
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert command[command.index("--upstream") + 1] == "https://upstream.example/base"


def test_run_prices_default_falls_back_to_the_home_sheet(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home_sheet = home / ".tamias" / "prices.toml"
    home_sheet.parent.mkdir(parents=True)
    _write_prices(home_sheet.parent)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert command[command.index("--prices") + 1] == str(home_sheet)
    assert command[command.index("--upstream") + 1] == cli.DEFAULT_UPSTREAM


def test_run_falls_back_to_the_sheet_bundled_with_the_package(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home-sheet"))
    monkeypatch.delenv("TAMIAS_UPSTREAM", raising=False)
    command = _capture_serve_command(monkeypatch)

    assert cli.main(["run", "--", "fake-child"]) == 1
    bundled = cli._bundled_prices_path()
    assert bundled is not None
    assert command[command.index("--prices") + 1] == str(bundled)
    # The provenance warning must name the bundle, so a real run is never
    # mistaken for one priced against the caller's own sheet (F-20).
    assert "bundled with tamias" in capsys.readouterr().err


def test_run_rejects_an_explicitly_named_missing_price_sheet(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)  # a default sheet exists, so only --prices can be wrong
    missing = tmp_path / "missing.toml"

    assert cli.main(["run", "--prices", str(missing), "--", "fake-child"]) == 2
    assert capsys.readouterr().err.splitlines() == [
        f"no price sheet found; run: tamias prices fetch --out {missing}"
    ]


def test_run_reserves_a_new_default_database_without_overwriting(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    first = Path(cli._reserve_run_db(None, "work"))
    second = Path(cli._reserve_run_db(None, "work"))

    assert first.parent == Path("tamias-runs")
    assert first.name.startswith("work-")
    assert first.suffix == ".db"
    assert first != second
    assert first.is_file()
    assert second.is_file()
    with pytest.raises(FileExistsError):
        cli._reserve_run_db(str(first), None)


def test_serve_router_config_parses_a_comma_separated_pool(tmp_path: Path) -> None:
    args = SimpleNamespace(
        router_config=None,
        router_profile="generic",
        effort_mode="off",
        cheap_model="cheap-a, cheap-b, cheap-a",
        strong_model="",
        min_gap=None,
    )
    config = cli._serve_router_config(args)
    assert config.cheap_model == "cheap-a"
    assert config.pool == ("cheap-a", "cheap-b")


def test_serve_router_config_single_model_is_a_one_member_pool(tmp_path: Path) -> None:
    args = SimpleNamespace(
        router_config=None,
        router_profile="generic",
        effort_mode="off",
        cheap_model="cheap-a",
        strong_model="",
        min_gap=0,
    )
    config = cli._serve_router_config(args)
    assert config.cheap_model == "cheap-a"
    assert config.pool == ("cheap-a",)
    assert config.min_gap == 0
