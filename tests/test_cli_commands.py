from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli


def test_dashboard_delegates_without_starting_a_server(monkeypatch) -> None:
    calls: list[list[str]] = []

    def dashboard_main(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr("tamias.dashboard.main", dashboard_main)
    assert (
        cli.main(["dashboard", "--db", "log.db", "--prices", "prices.toml", "--port", "9124"])
        == 17
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
