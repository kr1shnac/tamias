"""The overhead benchmark must run offline and report a usable, honest summary.

Timing is never asserted -- it varies by machine -- only that the script runs to
completion, logs exactly the requests it measured, and emits the structural
fields a reader needs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_benchmark_script_reports_structural_fields() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "benchmark_overhead.py"),
            "--requests",
            "5",
            "--warmup",
            "1",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["requests"] == 5
    assert result["rows_logged"] == 5
    for path in ("baseline", "proxy"):
        assert result[path]["throughput_rps"] > 0
        assert result[path]["p50_ms"] >= 0
    assert "added_mean_ms" in result
    assert result["throughput_ratio"] > 0
