"""Test shim: the mock upstream now ships in :mod:`tamias.demo_upstream`.

``tamias demo`` has to work from an installed wheel, where the test tree is
absent, so the implementation moved into the package.  This shim keeps the
existing test imports working without a mechanical edit of every test file.
"""

from tamias.demo_upstream import *  # noqa: F401,F403
