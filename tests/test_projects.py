from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias.store import PROJECT_MAX_CHARS, sanitize_project


def test_project_sanitizer_accepts_only_bounded_identifiers() -> None:
    assert sanitize_project("work-1.alpha") == "work-1.alpha"
    for invalid in ("", "bad name", "slash/name", "x" * (PROJECT_MAX_CHARS + 1)):
        assert sanitize_project(invalid) is None


@pytest.mark.parametrize("value", [None, 0, [], {}])
def test_project_sanitizer_rejects_non_strings(value: object) -> None:
    assert sanitize_project(value) is None
