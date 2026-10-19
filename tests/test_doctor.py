from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli
from tamias.cli import doctor


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _prices(path: Path, sheet_date: str) -> None:
    path.write_text(f'date = "{sheet_date}"\n[example]\ninput = 1\noutput = 1\n')


def test_doctor_port_defaults_to_serve_and_can_be_overridden() -> None:
    parser = cli.build_parser()
    serve_port = parser.parse_args(
        ["serve", "--upstream", "http://upstream.test", "--prices", "prices.toml", "--db", "log.db"]
    ).port

    assert parser.parse_args(["doctor"]).port == serve_port
    assert parser.parse_args(["doctor", "--port", "8788"]).port == 8788


def test_doctor_reports_offline_checks_with_tmp_paths(tmp_path: Path, capsys, monkeypatch) -> None:
    prices = tmp_path / "prices.toml"
    _prices(prices, "2026-10-06")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = doctor(
        prices=prices,
        db=tmp_path / "requests.db",
        port=0,
        online=False,
        upstream="https://example.invalid",
        today=date(2026, 10, 6),
        port_checker=lambda _: True,
    )

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"OK Python: {sys.version_info.major}.{sys.version_info.minor}",
        f"OK Price sheet: {prices} (0 days old)",
        f"OK DB directory: {tmp_path} writable",
        f"OK DB: {tmp_path / 'requests.db'} not present yet (created on first request)",
        "OK Port: 0 free",
        "WARN OPENROUTER_API_KEY: missing",
    ]


def test_doctor_warns_for_stale_prices_and_checks_online_with_injected_opener(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    prices = tmp_path / "prices.toml"
    _prices(prices, "2026-08-01")
    monkeypatch.setenv("OPENROUTER_API_KEY", "set-for-test")
    calls: list[tuple[str, float]] = []

    def opener(url: str, *, timeout: float) -> _Response:
        calls.append((url, timeout))
        return _Response()

    code = doctor(
        prices=prices,
        db=tmp_path / "requests.db",
        port=0,
        online=True,
        upstream="https://upstream.test/health",
        today=date(2026, 10, 6),
        opener=opener,
        port_checker=lambda _: True,
    )

    assert code == 0
    assert "WARN Price sheet:" in capsys.readouterr().out
    assert calls == [("https://upstream.test/health", 2.0)]


def test_doctor_returns_nonzero_for_a_busy_port(tmp_path: Path, capsys, monkeypatch) -> None:
    prices = tmp_path / "prices.toml"
    _prices(prices, "2026-10-06")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    port = 8000
    code = doctor(
        prices=prices,
        db=tmp_path / "requests.db",
        port=port,
        online=False,
        upstream="https://example.invalid",
        today=date(2026, 10, 6),
        port_checker=lambda _: False,
    )

    assert code == 1
    assert f"FAIL Port: {port} in use" in capsys.readouterr().out


def test_doctor_reports_a_large_db_and_suggests_prune(tmp_path: Path, capsys, monkeypatch) -> None:
    prices = tmp_path / "prices.toml"
    _prices(prices, "2026-10-06")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    db = tmp_path / "requests.db"
    db.write_bytes(b"\x00" * (cli.DOCTOR_DB_SIZE_WARN_MB * 1024 * 1024 + 1))

    code = doctor(
        prices=prices,
        db=db,
        port=0,
        online=False,
        upstream="https://example.invalid",
        today=date(2026, 10, 6),
        port_checker=lambda _: True,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "WARN DB size:" in out
    assert "tamias prune --db" in out


def test_doctor_uses_the_bundled_snapshot_when_no_sheet_is_named(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # No --prices given and no local sheet: the resolver prices with the bundled
    # snapshot, so doctor must not warn (F-20 first-run trap).
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_bundled_prices_path", lambda: Path("/bundled/prices.default.toml"))

    code = doctor(
        prices=tmp_path / "prices.toml",
        db=tmp_path / "requests.db",
        port=0,
        online=False,
        upstream="https://example.invalid",
        explicit=False,
        today=date(2026, 10, 6),
        port_checker=lambda _: True,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "OK Price sheet: no local sheet; will use the bundled snapshot" in out
    assert "WARN Price sheet:" not in out


def test_doctor_still_warns_when_an_explicit_sheet_is_missing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # An explicitly named file that does not exist is a real problem: the caller
    # named it, and silently pricing with another would be worse than stopping.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = doctor(
        prices=tmp_path / "missing.toml",
        db=tmp_path / "requests.db",
        port=0,
        online=False,
        upstream="https://example.invalid",
        explicit=True,
        today=date(2026, 10, 6),
        port_checker=lambda _: True,
    )

    assert code == 0
    assert "WARN Price sheet:" in capsys.readouterr().out
