"""The demo task generator and the model picker, checked from the repo's own suite.

Two things are proved here, and neither touches the network:

* ``demo/make_task.py`` produces a task with a **known failure count**.  Buggy
  mode must fail exactly seven tests and let nothing pass by accident; ``--solution``
  must make all seven pass.  Both are run as real pytest subprocesses in a tmp dir,
  because the number that matters is the one pytest prints, not a guess made in
  process.
* ``demo/pick_models.py:to_toml_entry`` converts one model dict into a price sheet
  table.  It is pure, so a hand-made dict is enough and no catalogue is fetched.

Run this file alone with:

    .venv/bin/pytest -q tests/test_demo_tools.py
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo"

EXPECTED_TESTS = 7
TOLERANCE = 1e-9


def _load(name: str) -> ModuleType:
    """Import a demo/ script by path, so demo/ never lands on sys.path.

    Loading by path rather than by import name keeps these scripts out of the
    rest of the suite's namespace: ``demo/pick_models.py`` and ``demo/*.py``
    would otherwise be importable as top-level modules for every other test.
    """
    spec = importlib.util.spec_from_file_location(f"_demo_{name}", DEMO_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_task = _load("make_task")
pick_models = _load("pick_models")


# --------------------------------------------------------------------------
# The task itself
# --------------------------------------------------------------------------


def _generate(tmp_path: Path, solution: bool) -> Path:
    out = tmp_path / ("solution" if solution else "buggy")
    make_task.generate(str(out), make_task.FIXED if solution else make_task.BUGGY)
    return out


def _run_pytest(task_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run pytest against a generated project, in that project.

    ``sys.executable`` is the interpreter running this suite, i.e. the project
    venv, so the demo task is scored by the same Python the arms will use.
    ``--tb=no`` keeps the summary short; ``-p no:cacheprovider`` leaves no
    .pytest_cache behind in a tmp dir.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=task_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def _summary(result: subprocess.CompletedProcess[str]) -> str:
    """The last line pytest printed that reports counts, e.g. "7 failed in 0.03s"."""
    for line in reversed(result.stdout.splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            return line.strip()
    return result.stdout.strip()


def test_buggy_task_fails_exactly_seven_tests(tmp_path: Path) -> None:
    task_dir = _generate(tmp_path, solution=False)
    result = _run_pytest(task_dir)

    assert result.returncode == 1, f"pytest did not fail:\n{result.stdout}\n{result.stderr}"
    summary = _summary(result)
    assert re.match(rf"^{EXPECTED_TESTS} failed in ", summary), summary


def test_buggy_task_lets_nothing_pass_by_accident(tmp_path: Path) -> None:
    """Seven failures and zero passes.

    A task where one of the seven tests already passes is a different task: the
    agent gets a free one, and the failure count no longer means "seven bugs".
    """
    result = _run_pytest(_generate(tmp_path, solution=False))

    assert not re.search(r"\b[1-9]\d* passed", result.stdout), result.stdout
    assert not re.search(r"\berror", result.stdout), result.stdout


def test_buggy_task_fails_each_module(tmp_path: Path) -> None:
    """The seven failures are the seven named functions, one line each.

    Guards against the count staying at seven while the content drifts: a
    generator that broke one module's import would report one collection error
    instead of that module's test failing.
    """
    result = _run_pytest(_generate(tmp_path, solution=False))

    failed = set(re.findall(r"^FAILED (tests/\S+)", result.stdout, re.MULTILINE))
    assert failed == {
        "tests/test_slug.py::test_slugify",
        "tests/test_stats.py::test_mean",
        "tests/test_stats.py::test_median",
        "tests/test_cart.py::test_total",
        "tests/test_calendar_utils.py::test_is_leap",
        "tests/test_text.py::test_word_count",
        "tests/test_fizz.py::test_fizzbuzz",
    }


def test_solution_makes_every_test_pass(tmp_path: Path) -> None:
    result = _run_pytest(_generate(tmp_path, solution=True))

    assert result.returncode == 0, f"pytest did not pass:\n{result.stdout}\n{result.stderr}"
    assert re.search(rf"\b{EXPECTED_TESTS} passed\b", result.stdout), result.stdout
    assert not re.search(r"\bfailed\b", result.stdout), result.stdout


def test_generation_is_deterministic(tmp_path: Path) -> None:
    """Same arguments, same bytes -- so two arms can be compared at all."""
    first = _generate(tmp_path / "a", solution=False)
    second = _generate(tmp_path / "b", solution=False)

    names = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    assert names == sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_the_tests_are_identical_in_both_variants(tmp_path: Path) -> None:
    """Only the sources differ.

    The task prompt forbids editing anything under tests/, so a generator that
    changed the tests would quietly change the task between the two arms.
    """
    buggy = _generate(tmp_path / "buggy", solution=False)
    solution = _generate(tmp_path / "solution", solution=True)

    for path in sorted((buggy / "tests").glob("*.py")):
        assert path.read_bytes() == (solution / "tests" / path.name).read_bytes()


def test_generation_rejects_an_unknown_variant(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown variant"):
        make_task.generate(str(tmp_path / "nope"), "half-fixed")


def test_task_prompt_carries_the_pytest_placeholder() -> None:
    prompt = (DEMO_DIR / "TASK_PROMPT.txt").read_text(encoding="utf-8")

    assert "{PYTEST}" in prompt
    assert "Do not edit anything under tests/" in prompt


# --------------------------------------------------------------------------
# to_toml_entry: the price conversion, with no network
# --------------------------------------------------------------------------

# Prices are decimal *strings* per token, as the OpenRouter catalogue serves them.
MODEL_WITH_CACHE = {
    "id": "acme/nemotron-demo-1",
    "pricing": {
        "prompt": "0.000003",
        "completion": "0.000015",
        "input_cache_read": "0.0000003",
    },
}
MODEL_WITHOUT_CACHE = {
    "id": "acme/nemotron-demo-2",
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
}


def _rates(entry: str) -> dict[str, float]:
    """Parse one emitted table back into rates, so the test reads numbers not text."""
    document = tomllib.loads(f'date = "2026-10-05"\n{entry}')
    tables = {key: value for key, value in document.items() if key != "date"}
    assert len(tables) == 1, f"expected exactly one model table, got {sorted(tables)}"
    return next(iter(tables.values()))


def test_to_toml_entry_converts_per_token_prices_to_per_million() -> None:
    rates = _rates(pick_models.to_toml_entry(MODEL_WITH_CACHE))

    assert rates["input"] == pytest.approx(3.0, abs=TOLERANCE)
    assert rates["output"] == pytest.approx(15.0, abs=TOLERANCE)


def test_to_toml_entry_uses_the_published_cache_read_rate() -> None:
    rates = _rates(pick_models.to_toml_entry(MODEL_WITH_CACHE))

    assert rates["cached_input"] == pytest.approx(0.3, abs=TOLERANCE)
    assert pick_models.NO_CACHE_DISCOUNT not in pick_models.to_toml_entry(MODEL_WITH_CACHE)


def test_to_toml_entry_falls_back_to_the_input_rate_and_says_so() -> None:
    entry = pick_models.to_toml_entry(MODEL_WITHOUT_CACHE)
    rates = _rates(entry)

    assert rates["cached_input"] == pytest.approx(3.0, abs=TOLERANCE)
    assert pick_models.NO_CACHE_DISCOUNT in entry


def test_to_toml_entry_writes_a_zero_cache_write_rate() -> None:
    """0.0, not missing.

    An OpenAI-style upstream carries no cache-write count, so a nonzero rate
    would leave every request UNKNOWN instead of priced.
    """
    assert _rates(pick_models.to_toml_entry(MODEL_WITH_CACHE))["cache_write"] == 0.0


def test_to_toml_entry_quotes_the_table_name() -> None:
    """Model ids carry "/" and ":", which a TOML bare key cannot hold."""
    entry = pick_models.to_toml_entry(MODEL_WITH_CACHE)

    assert entry.startswith('["acme/nemotron-demo-1"]')


def test_to_toml_entry_rejects_a_model_with_no_id() -> None:
    with pytest.raises(pick_models.PickError):
        pick_models.to_toml_entry({"pricing": {"prompt": "0.1"}})


def test_price_sheet_carries_todays_date_and_parses(tmp_path: Path) -> None:
    sheet = pick_models.to_toml_sheet([MODEL_WITH_CACHE, MODEL_WITHOUT_CACHE], when="2026-10-05")

    assert 'date = "2026-10-05"' in sheet
    document = tomllib.loads(sheet)
    assert set(document) == {"date", "acme/nemotron-demo-1", "acme/nemotron-demo-2"}
    # And the sheet tamias actually loads.
    assert document["acme/nemotron-demo-1"]["output"] == pytest.approx(15.0, abs=TOLERANCE)


def test_only_models_with_tools_and_a_price_are_kept() -> None:
    def model(model_id: str, **pricing: str) -> dict[str, object]:
        return {"id": model_id, "supported_parameters": ["tools"], "pricing": pricing}

    assert pick_models.is_priceable(model("a/b", prompt="0.1", completion="0.2"))
    # Free: real models, real zeroes, and every saving this demo could show is $0.
    assert not pick_models.is_priceable(model("a/b", prompt="0", completion="0"))
    assert not pick_models.is_priceable(model("a/b", prompt="0.1", completion="0"))
    assert not pick_models.is_priceable(model("a/b", prompt="0", completion="0.2"))

    no_tools = model("a/b", prompt="0.1", completion="0.2")
    no_tools["supported_parameters"] = ["max_tokens"]
    assert not pick_models.is_priceable(no_tools)

    assert not pick_models.is_priceable({"id": "a/b"})


def test_provider_is_the_part_before_the_slash() -> None:
    assert pick_models.provider_of("nvidia/nemotron-3-ultra-550b-a55b:free") == "nvidia"
    # No slash: the whole id is its own group rather than an empty one.
    assert pick_models.provider_of("local-model") == "local-model"
