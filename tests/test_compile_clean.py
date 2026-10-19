"""Every source module must compile with no warnings, including as errors.

A latent defect class the run 20261020-1500 council surfaced: an invalid escape
sequence (``"\\-"`` inside a non-raw string) compiles to a ``SyntaxWarning`` on
current Python and becomes a ``SyntaxError`` in a future version.  The test
suite never noticed, because importing a module only warns.  This test compiles
each module with warnings promoted to errors, so the class cannot recur.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

MODULES = sorted((SRC / "tamias").glob("*.py"))


def test_modules_were_found() -> None:
    assert MODULES, "no source modules discovered"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_compiles_without_warnings(path: Path, tmp_path: Path) -> None:
    """Compile with SyntaxWarning promoted to error (a future SyntaxError)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        try:
            py_compile.compile(
                str(path), cfile=str(tmp_path / (path.stem + ".pyc")), doraise=True
            )
        except py_compile.PyCompileError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"{path.name} does not compile cleanly: {exc}") from exc


def test_the_invalid_escape_that_prompted_this_test_is_gone() -> None:
    """A direct regression guard for the exact defect (dashboard CSV regex)."""
    source = (SRC / "tamias" / "dashboard.py").read_text(encoding="utf-8")
    # A non-raw backslash-hyphen is the defect; the fix is a doubled backslash.
    assert "[=\\-+@" not in source, "invalid escape sequence returned to dashboard.py"
    assert "[=\\\\-+@" in source, "the formula-injection guard's escaped hyphen is missing"
