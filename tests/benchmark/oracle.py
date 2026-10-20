"""The benchmark oracle: did the agent's edits make the task's own check pass?

This is the apparatus ``TAMIAS-SSOT.md`` §18 Phase 3 calls for and F-35 records
as missing ("if the oracle cannot be written, the benchmark cannot run — this
ordering is the entire gate"). It is deliberately **off-proxy**: the proxy sees
tool success, never task success (`SSOT:80`), so completion must be judged by
something the proxy does not control.

The oracle answers exactly one question per task and arm: does ``pytest`` pass
in the task directory, and (for T5) did the agent leave the tests untouched? It
does **not** measure cost, quality or latency — those are §18's other columns.

Nothing here is a savings claim and no number in it is measured against a
provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tasks_fixture  # noqa: E402  (path injected above)

# Tasks whose oracle additionally rejects any edit under tests/.
ANTI_GAMING_TASKS = frozenset({"T5"})


@dataclass(frozen=True)
class OracleResult:
    """One oracle evaluation. ``passed`` is the whole answer; the rest is why."""

    task: str
    passed: bool
    reason: str
    tests_edited: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1


def _run_pytest(workdir: str) -> tuple[bool, str]:
    """Run the task's own suite with the task dir importable. No network."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    passed = completed.returncode == 0
    tail = (completed.stdout or completed.stderr).strip().splitlines()
    summary = tail[-1] if tail else "no output"
    return passed, summary


def _edited_tests(workdir: str) -> tuple[str, ...]:
    """Test files whose bytes differ from what the fixture wrote."""
    manifest_path = os.path.join(workdir, ".oracle_manifest.json")
    if not os.path.exists(manifest_path):
        return ()
    with open(manifest_path) as handle:
        expected = json.loads(handle.read()).get("tests", {})

    tests_dir = os.path.join(workdir, "tests")
    edited = []
    for name, want in sorted(expected.items()):
        path = os.path.join(tests_dir, name)
        if not os.path.exists(path):
            edited.append(name)
            continue
        with open(path, "rb") as handle:
            have = hashlib.sha256(handle.read()).hexdigest()
        if have != want:
            edited.append(name)
    return tuple(edited)


def evaluate(task: str, workdir: str) -> OracleResult:
    """Score one task in one arm. Pure function over the files on disk."""
    if task not in tasks_fixture.TASK_NAMES:
        raise ValueError(f"unknown task {task!r}")

    if task in ANTI_GAMING_TASKS:
        edited = _edited_tests(workdir)
        if edited:
            return OracleResult(
                task,
                passed=False,
                reason=f"tests edited (anti-gaming): {', '.join(edited)}",
                tests_edited=edited,
            )

    passed, summary = _run_pytest(workdir)
    return OracleResult(
        task,
        passed=passed,
        reason=summary,
        tests_edited=(),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score one benchmark task.")
    parser.add_argument("--task", required=True, choices=tasks_fixture.TASK_NAMES)
    parser.add_argument("--dir", required=True, help="the task working directory")
    args = parser.parse_args()
    result = evaluate(args.task, args.dir)
    print(f"{result.task}: {'PASS' if result.passed else 'FAIL'} — {result.reason}")
    raise SystemExit(result.exit_code)
