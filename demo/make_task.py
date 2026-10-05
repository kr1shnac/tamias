"""Generate the small "inventory" project the demo arms are run against.

Usage:
    .venv/bin/python demo/make_task.py --out DIR            # buggy: 7 failing tests
    .venv/bin/python demo/make_task.py --out DIR --solution  # fixed: all pass

Six source modules hold seven functions between them, and every function is
wrong in exactly one way in buggy mode.  The seven test functions -- one file
per module, two in ``tests/test_stats.py`` -- are the task: an agent has to make
all seven pass without editing anything under ``tests/``.

Nothing here is random.  The same ``--out`` argument always produces the same
bytes, which is what lets the demo arms be compared against each other.
"""

from __future__ import annotations

import argparse
import os

BUGGY = "buggy"
FIXED = "fixed"


def write_file(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


# --------------------------------------------------------------------------
# Source modules.  Each entry is {variant: file contents}.
# --------------------------------------------------------------------------

SOURCES: dict[str, dict[str, str]] = {
    "slug": {
        BUGGY: """def slugify(s):
    s = s.lower()
    s = s.replace(" ", "_")
    return s
""",
        FIXED: """def slugify(s):
    import re

    s = s.strip().lower()
    s = re.sub(r"\\s+", "-", s)
    return s
""",
    },
    "stats": {
        BUGGY: """def mean(xs):
    return sum(xs) // len(xs)


def median(xs):
    return xs[len(xs) // 2]
""",
        FIXED: """def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2
""",
    },
    "cart": {
        BUGGY: """def total(items):
    return sum(item["price"] * item["qty"] - item["discount"] for item in items)
""",
        FIXED: """def total(items):
    return sum(item["price"] * item["qty"] * (1 - item["discount"]) for item in items)
""",
    },
    "calendar_utils": {
        BUGGY: """def is_leap(y):
    return y % 4 == 0
""",
        FIXED: """def is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
""",
    },
    "text": {
        BUGGY: """def word_count(s):
    return len(s.split(" "))
""",
        FIXED: """def word_count(s):
    return len(s.split())
""",
    },
    "fizz": {
        BUGGY: """def fizzbuzz(n):
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)
""",
        FIXED: """def fizzbuzz(n):
    if n % 15 == 0:
        return "FizzBuzz"
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)
""",
    },
}


TEST_HEADER = """import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""


# --------------------------------------------------------------------------
# Tests: one file per module.  These files are byte-identical in both variants,
# because the task forbids editing them; only the sources differ.
# --------------------------------------------------------------------------

TESTS: dict[str, str] = {
    "test_slug.py": """from slug import slugify


def test_slugify():
    assert slugify("  Hello   World ") == "hello-world"
    assert slugify("A b") == "a-b"
""",
    "test_stats.py": """from stats import mean, median


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5
    assert mean([2]) == 2


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
""",
    "test_cart.py": """from cart import total


def test_total():
    assert total([{"price": 100.0, "qty": 2, "discount": 0.1}]) == 180.0
    assert total([]) == 0.0
""",
    "test_calendar_utils.py": """from calendar_utils import is_leap


def test_is_leap():
    assert is_leap(1900) is False
    assert is_leap(2000) is True
    assert is_leap(2024) is True
    assert is_leap(2023) is False
""",
    "test_text.py": """from text import word_count


def test_word_count():
    assert word_count("a  b\\nc") == 3
    assert word_count("") == 0
""",
    "test_fizz.py": """from fizz import fizzbuzz


def test_fizzbuzz():
    assert fizzbuzz(15) == "FizzBuzz"
    assert fizzbuzz(3) == "Fizz"
    assert fizzbuzz(5) == "Buzz"
    assert fizzbuzz(7) == "7"
""",
}


README = """# inventory

A small helper library.  Six modules, seven functions.

Run the tests from this directory:

    pytest -q

Seven of them fail.  Fix the source files -- not the tests -- until they pass.
"""


def generate(out_dir: str, variant: str) -> None:
    """Write the whole project into ``out_dir``.

    ``variant`` is ``BUGGY`` or ``FIXED``; it selects the sources and nothing
    else, so the two runs differ only in the code under test.
    """
    if variant not in (BUGGY, FIXED):
        raise ValueError(f"unknown variant {variant!r}")

    for module, variants in SOURCES.items():
        write_file(os.path.join(out_dir, module + ".py"), variants[variant])

    for filename, body in TESTS.items():
        write_file(os.path.join(out_dir, "tests", filename), TEST_HEADER + body)

    write_file(os.path.join(out_dir, "README.md"), README)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the inventory demo project.")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument(
        "--solution",
        action="store_true",
        help="write the fixed sources instead of the buggy ones",
    )
    args = parser.parse_args()

    generate(args.out, FIXED if args.solution else BUGGY)
    print(args.out)


if __name__ == "__main__":
    main()
