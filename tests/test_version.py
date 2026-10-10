"""The distribution version is one number, everywhere.

``tamias.__version__``, the installed distribution metadata, and the static
``[project] version`` in ``pyproject.toml`` must agree, and ``tamias --version``
must print that number.  A drift here means a wheel ships with a version the
source does not claim.
"""

from __future__ import annotations

import importlib.metadata
import sys
import tomllib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias import __version__, cli  # noqa: E402


def _pyproject_version() -> str:
    document = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    return document["project"]["version"]


def test_source_attribute_matches_pyproject() -> None:
    assert __version__ == _pyproject_version()


def test_installed_metadata_matches_source(capsys) -> None:
    try:
        installed = importlib.metadata.version("tamias")
    except importlib.metadata.PackageNotFoundError:
        capsys.readouterr()
        pytest.skip("tamias distribution is not installed in this interpreter")
    assert installed == __version__


def test_version_flag_prints_the_distribution_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"tamias {__version__}"