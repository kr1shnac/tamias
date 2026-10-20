"""The oracle's own test — the proof the benchmark apparatus exists.

`TAMIAS-SSOT.md` §18 Phase 3 and F-35 require five benchmark tasks with
pass/fail oracles before the paired benchmark can run. This file is that
apparatus, checked: every task's oracle is evaluated against the **buggy**
fixture (must FAIL) and the **fixed** fixture (must PASS), and T5's anti-gaming
clause is shown to reject an edited test.

Runs offline, no API key, no network — exactly like the rest of the gate.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import oracle  # noqa: E402
import tasks_fixture  # noqa: E402

TASKS = tasks_fixture.TASK_NAMES


def test_five_tasks_are_defined() -> None:
    """§18 Phase 3 says five tasks; not four, not six."""
    assert len(TASKS) == 5, f"expected 5 benchmark tasks, found {len(TASKS)}: {TASKS}"


@pytest.mark.parametrize("task", TASKS)
def test_oracle_fails_on_the_buggy_fixture(task: str, tmp_path) -> None:
    """A task that has not been fixed must not pass its oracle."""
    workdir = str(tmp_path / task)
    tasks_fixture.generate(workdir, task, tasks_fixture.BUGGY)
    result = oracle.evaluate(task, workdir)
    assert result.passed is False, f"{task}: buggy fixture wrongly PASSED ({result.reason})"
    assert result.exit_code == 1


@pytest.mark.parametrize("task", TASKS)
def test_oracle_passes_on_the_fixed_fixture(task: str, tmp_path) -> None:
    """The reference solution must pass its oracle — otherwise the task is unsolvable."""
    workdir = str(tmp_path / task)
    tasks_fixture.generate(workdir, task, tasks_fixture.FIXED)
    result = oracle.evaluate(task, workdir)
    assert result.passed is True, f"{task}: fixed fixture wrongly FAILED ({result.reason})"
    assert result.exit_code == 0


def test_t5_anti_gaming_rejects_an_edited_test(tmp_path) -> None:
    """F-14 applied to the oracle: a task cannot be passed by editing its test.

    T5's source is fixed, so pytest would pass — but the agent also touched a
    test file, which the oracle must refuse.
    """
    workdir = str(tmp_path / "T5")
    tasks_fixture.generate(workdir, "T5", tasks_fixture.FIXED)

    # The agent cheats: weaken a test instead of keeping the source honest.
    cheated = os.path.join(workdir, "tests", "test_fizz.py")
    with open(cheated, "a", encoding="utf-8") as handle:
        handle.write("\n\ndef test_nothing():\n    assert True\n")

    result = oracle.evaluate("T5", workdir)
    assert result.passed is False, "edited test was not caught"
    assert result.tests_edited == ("test_fizz.py",)
    assert "anti-gaming" in result.reason


def test_fixture_is_deterministic(tmp_path) -> None:
    """Same task, same bytes — the property that lets arm A and arm B compare."""
    first = str(tmp_path / "a")
    second = str(tmp_path / "b")
    tasks_fixture.generate(first, "T1", tasks_fixture.BUGGY)
    tasks_fixture.generate(second, "T1", tasks_fixture.BUGGY)

    for name in ("slug.py", "README.md", os.path.join("tests", "test_slug.py")):
        with open(os.path.join(first, name), "rb") as handle:
            a = handle.read()
        with open(os.path.join(second, name), "rb") as handle:
            b = handle.read()
        assert a == b, f"{name} differs between two generations"


def test_unknown_task_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        oracle.evaluate("T99", str(tmp_path))
