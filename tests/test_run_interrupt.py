"""``tamias run`` shutting a child down the way each platform expects.

The Ctrl+C handler is the only place tamias signals a child process, and a
signal that raises in the handler turns a clean stop into a traceback.  Windows
has no SIGINT for ``Popen.send_signal`` (``ValueError``), so the handler must
branch there.  These tests pin the branch, because the Linux path is the one
any maintainer runs and the Windows path is the one that would break.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias import cli


class FakeProcess:
    """Records the stop calls, and can refuse a signal like Windows does."""

    def __init__(self, *, refuses: tuple = ()) -> None:
        self.calls: list[str] = []
        self._refuses = refuses

    def send_signal(self, sig: int) -> None:
        if sig in self._refuses:
            raise ValueError(f"Unsupported signal: {sig}")
        self.calls.append(f"signal:{sig}")

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        return 0


def test_posix_interrupts_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    process = FakeProcess()
    cli._interrupt_child(process)  # type: ignore[arg-type]
    assert process.calls == [f"signal:{signal.SIGINT}"]


def test_windows_uses_terminate_not_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: SIGINT raises ValueError, so terminate() is the stop."""
    monkeypatch.setattr(os, "name", "nt")
    process = FakeProcess(refuses=(signal.SIGINT,))
    cli._interrupt_child(process)  # type: ignore[arg-type]
    assert process.calls == ["terminate"]
    # The call that used to raise must not be attempted at all.
    assert not any(call.startswith("signal:") for call in process.calls)


def test_a_dead_child_falls_back_to_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal the child cannot take still yields a stop, never an exception."""
    monkeypatch.setattr(os, "name", "posix")
    process = FakeProcess(refuses=(signal.SIGINT,))
    cli._interrupt_child(process)  # type: ignore[arg-type]
    assert process.calls == ["terminate"]


def test_interrupt_of_a_real_process_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end against a real child: interrupt, then wait returns."""
    monkeypatch.setattr(os, "name", "posix")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        cli._interrupt_child(process)
        process.wait(timeout=10)
        assert process.returncode is not None
    finally:
        if process.poll() is None:  # pragma: no cover - only on failure
            process.kill()
            process.wait()
