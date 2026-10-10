"""The demo runner must stay inside the alpha boundary and be non-destructive.

`active` routing is rejected in 0.1.0a4, so the runner must never ask for it, and
it must refuse to overwrite a committed run unless `--force` is passed.  These
are static/cheap checks: they read the script and run its `--help`, without
starting the proxy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_ARM = REPO / "demo" / "run_arm.sh"


def test_runner_never_requests_active_routing() -> None:
    text = RUN_ARM.read_text(encoding="utf-8")
    assert "--router-mode active" not in text
    assert "router-mode active" not in text


def test_runner_documents_the_alpha_boundary() -> None:
    text = RUN_ARM.read_text(encoding="utf-8")
    assert "shadow" in text
    assert "--force" in text


def test_runner_help_exits_zero() -> None:
    completed = subprocess.run(
        ["bash", str(RUN_ARM), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "usage:" in (completed.stdout + completed.stderr)
