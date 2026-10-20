"""Deterministic fixtures for the five benchmark tasks (docs/benchmark/tasks.md).

Each task is a tiny project with buggy sources and a test suite. The buggy
variant must fail its oracle; the fixed variant must pass it. Nothing here is
random: the same ``--out`` always produces the same bytes, which is what lets
the paired arms (routing off vs on) be compared.

Usage:
    .venv/bin/python tests/benchmark/tasks_fixture.py --out DIR --task T1
    .venv/bin/python tests/benchmark/tasks_fixture.py --out DIR --task T1 --solution

Every task also writes ``.oracle_manifest.json`` mapping each test file to its
SHA-256, so the oracle can detect an agent that edited the tests instead of the
source (the F-14 anti-gaming clause).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

BUGGY = "buggy"
FIXED = "fixed"

TEST_HEADER = """import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""


# name -> {"sources": {module: {variant: text}}, "tests": {file: text}, "readme": str}
TASKS: dict[str, dict[str, object]] = {
    "T1": {
        "sources": {
            "slug": {
                BUGGY: (
                    "def slugify(s):\n"
                    "    s = s.lower()\n"
                    '    s = s.replace(" ", "_")\n'
                    "    return s\n"
                ),
                FIXED: (
                    "def slugify(s):\n"
                    "    import re\n\n"
                    "    s = s.strip().lower()\n"
                    '    s = re.sub(r"\\s+", "-", s)\n'
                    "    return s\n"
                ),
            }
        },
        "tests": {
            "test_slug.py": (
                "from slug import slugify\n\n\n"
                "def test_slugify():\n"
                '    assert slugify("  Hello   World ") == "hello-world"\n'
                '    assert slugify("A b") == "a-b"\n'
            )
        },
        "readme": "# T1 slug\n\nOne function is wrong. Fix the source, not the tests.\n",
    },
    "T2": {
        "sources": {
            "stats": {
                BUGGY: (
                    "def mean(xs):\n"
                    "    return sum(xs) // len(xs)\n\n\n"
                    "def median(xs):\n"
                    "    return xs[len(xs) // 2]\n"
                ),
                FIXED: (
                    "def mean(xs):\n"
                    "    return sum(xs) / len(xs)\n\n\n"
                    "def median(xs):\n"
                    "    xs = sorted(xs)\n"
                    "    n = len(xs)\n"
                    "    if n % 2 == 1:\n"
                    "        return xs[n // 2]\n"
                    "    return (xs[n // 2 - 1] + xs[n // 2]) / 2\n"
                ),
            }
        },
        "tests": {
            "test_stats.py": (
                "from stats import mean, median\n\n\n"
                "def test_mean():\n"
                "    assert mean([1, 2, 3, 4]) == 2.5\n"
                "    assert mean([2]) == 2\n\n\n"
                "def test_median():\n"
                "    assert median([3, 1, 2]) == 2\n"
                "    assert median([4, 1, 3, 2]) == 2.5\n"
            )
        },
        "readme": "# T2 stats\n\nTwo functions are wrong. Fix the source, not the tests.\n",
    },
    "T3": {
        "sources": {
            "cart": {
                BUGGY: (
                    "def total(items):\n"
                    '    return sum(item["price"] * item["qty"] - item["discount"]\n'
                    "               for item in items)\n"
                ),
                FIXED: (
                    "def total(items):\n"
                    '    return sum(item["price"] * item["qty"] * (1 - item["discount"])\n'
                    "               for item in items)\n"
                ),
            }
        },
        "tests": {
            "test_cart.py": (
                "from cart import total\n\n\n"
                "def test_total():\n"
                '    assert total([{"price": 100.0, "qty": 2, "discount": 0.1}]) == 180.0\n'
                "    assert total([]) == 0.0\n"
            )
        },
        "readme": "# T3 cart\n\nDiscounts are percentages. Fix the source, not the tests.\n",
    },
    "T4": {
        "sources": {
            "calendar_utils": {
                BUGGY: ("def is_leap(y):\n    return y % 4 == 0\n"),
                FIXED: (
                    "def is_leap(y):\n"
                    "    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)\n"
                ),
            }
        },
        "tests": {
            "test_calendar_utils.py": (
                "from calendar_utils import is_leap\n\n\n"
                "def test_is_leap():\n"
                "    assert is_leap(1900) is False\n"
                "    assert is_leap(2000) is True\n"
                "    assert is_leap(2024) is True\n"
                "    assert is_leap(2023) is False\n"
            )
        },
        "readme": "# T4 leap\n\nThe Gregorian rule is not the naive one. "
        "Fix the source, not the tests.\n",
    },
    "T5": {
        "sources": {
            "text": {
                BUGGY: ('def word_count(s):\n    return len(s.split(" "))\n'),
                FIXED: ("def word_count(s):\n    return len(s.split())\n"),
            },
            "fizz": {
                BUGGY: (
                    "def fizzbuzz(n):\n"
                    '    if n % 3 == 0:\n        return "Fizz"\n'
                    '    if n % 5 == 0:\n        return "Buzz"\n'
                    "    return str(n)\n"
                ),
                FIXED: (
                    "def fizzbuzz(n):\n"
                    '    if n % 15 == 0:\n        return "FizzBuzz"\n'
                    '    if n % 3 == 0:\n        return "Fizz"\n'
                    '    if n % 5 == 0:\n        return "Buzz"\n'
                    "    return str(n)\n"
                ),
            },
        },
        "tests": {
            "test_text.py": (
                "from text import word_count\n\n\n"
                "def test_word_count():\n"
                '    assert word_count("a  b\\nc") == 3\n'
                '    assert word_count("") == 0\n'
            ),
            "test_fizz.py": (
                "from fizz import fizzbuzz\n\n\n"
                "def test_fizzbuzz():\n"
                '    assert fizzbuzz(15) == "FizzBuzz"\n'
                '    assert fizzbuzz(3) == "Fizz"\n'
                '    assert fizzbuzz(5) == "Buzz"\n'
                '    assert fizzbuzz(7) == "7"\n'
            ),
        },
        "readme": "# T5 multi\n\nTwo modules are wrong. Fix the source, not the tests.\n",
    },
}

TASK_NAMES = tuple(TASKS)


def _write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def test_manifest(out_dir: str) -> dict[str, str]:
    """sha256 of every test file, so the oracle can detect edits under tests/."""
    tests_dir = os.path.join(out_dir, "tests")
    manifest = {}
    for name in sorted(os.listdir(tests_dir)):
        path = os.path.join(tests_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                manifest[name] = hashlib.sha256(handle.read()).hexdigest()
    return manifest


def generate(out_dir: str, task: str, variant: str) -> None:
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; expected one of {TASK_NAMES}")
    if variant not in (BUGGY, FIXED):
        raise ValueError(f"unknown variant {variant!r}")

    spec = TASKS[task]
    for module, variants in spec["sources"].items():  # type: ignore[union-attr]
        _write(os.path.join(out_dir, module + ".py"), variants[variant])

    for filename, body in spec["tests"].items():  # type: ignore[union-attr]
        _write(os.path.join(out_dir, "tests", filename), TEST_HEADER + body)

    _write(os.path.join(out_dir, "README.md"), spec["readme"])  # type: ignore[arg-type]

    # The manifest records the tests as written *by the fixture*.  An agent that
    # edits a test changes its hash, which the oracle rejects.
    manifest = test_manifest(out_dir)
    _write(
        os.path.join(out_dir, ".oracle_manifest.json"),
        json.dumps({"task": task, "tests": manifest}, indent=2, sort_keys=True),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a benchmark task fixture.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--task", required=True, choices=TASK_NAMES)
    parser.add_argument("--solution", action="store_true")
    args = parser.parse_args()
    generate(args.out, args.task, FIXED if args.solution else BUGGY)
    print(args.out)


if __name__ == "__main__":
    main()
