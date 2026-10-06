from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli


def test_dashboard_delegates_without_starting_a_server(monkeypatch) -> None:
    calls: list[list[str]] = []

    def dashboard_main(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr("tamias.dashboard.main", dashboard_main)
    assert (
        cli.main(["dashboard", "--db", "log.db", "--prices", "prices.toml", "--port", "9124"]) == 17
    )
    assert calls == [
        [
            "--db",
            "log.db",
            "--prices",
            "prices.toml",
            "--host",
            "127.0.0.1",
            "--port",
            "9124",
        ]
    ]


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
            "active",
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


def test_run_defaults_prices_and_upstream(monkeypatch, tmp_path: Path) -> None:
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

    (tmp_path / "prices.toml").write_text(
        'date = "2026-10-06"\n[model]\ninput = 1\noutput = 1\n', encoding="utf-8"
    )
    command: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: (command.extend(args), NeverStarts())[1],
    )
    monkeypatch.setattr(cli, "_wait_for_proxy", lambda *args, **kwargs: False)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert command[command.index("--prices") + 1] == "prices.toml"
    assert command[command.index("--upstream") + 1] == cli.DEFAULT_URL


def test_run_explains_how_to_create_a_missing_default_price_sheet(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", "--", "fake-child"]) == 1
    assert "tamias prices fetch --out prices.toml" in capsys.readouterr().err


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
