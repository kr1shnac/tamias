"""Doc-integrity test: mechanically assert that every authoritative claim in
claims.md resolves and that versions agree across the authority layer.

Runs as part of the regular gate: .venv/bin/python -m pytest -q.
Placed under tests/ so it is discovered alongside the other tests.
"""

import os
import re
import sys

README = 'README.md'
CLAIM = 'docs/audit/claims.md'
RELEASE = 'docs/RELEASE-STATUS.md'


def check_no_stale_line_citations():
    """claims.md should not cite README.md:NN-NN line numbers."""
    txt = open(CLAIM).read()
    stale = re.findall(r'README\.md:\d+', txt)
    if stale:
        return 'FAIL: Stale line citation in claims.md'
    return 'PASS: No stale README line citations'


def check_anchors_resolve():
    """Every README anchor cited in claims.md should exist as a heading."""
    txt = open(CLAIM).read()
    anchors = re.findall(r'README § [^;|,]+', txt)
    errors = []
    known_exceptions = {
        'intro disclaimer',
        'Install / § What Tamias does not do',
        'STATUS.md:3',
    }
    for a in anchors:
        a = a.replace('README § ', '').rstrip(', ;')
        if not re.search(rf'^## {re.escape(a)}$', open(README).read(), re.M):
            if a not in known_exceptions:
                errors.append(f'README anchor not found as heading: {a}')
    if errors:
        return 'FAIL: README anchor not resolved: ' + '; '.join(errors)
    return 'PASS: README anchors resolved'


def check_version_consistency():
    """The source version must agree across the authority layer.

    The version is read dynamically so this check does not need editing at
    every release; it only asserts that `pyproject.toml`, `src/tamias/__init__.py`,
    the README footer and the project website agree.
    """
    errors = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyproject = os.path.join(root, 'pyproject.toml')
    init = os.path.join(root, 'src', 'tamias', '__init__.py')

    m = re.search(r'^version = "([^"]+)"', open(pyproject).read(), re.M)
    if not m:
        return 'FAIL: no version found in pyproject.toml'
    version = m.group(1)

    init_version = re.search(r'^__version__ = "([^"]+)"', open(init).read(), re.M)
    if not init_version or init_version.group(1) != version:
        errors.append(
            f'__init__.__version__ {init_version.group(1) if init_version else None!r}'
            f' != pyproject version {version!r}'
        )

    if f'tamias {version}' not in open(README).read():
        errors.append(f'README does not mention the current version {version!r}')

    # The project website quotes the version too. It drifted silently once
    # (0.1.0a7 on the page while the source was 0.1.0a8), because only the
    # shell claims-guard checked it and nothing ran that guard locally.
    page = os.path.join(root, 'docs', 'index.html')
    if os.path.exists(page):
        if f'tamias {version}' not in open(page).read():
            errors.append(f'docs/index.html does not mention the current version {version!r}')

    if errors:
        return 'FAIL: version inconsistency: ' + '; '.join(errors)
    return f'PASS: version consistency ({version})'


def check_no_contradictory_publish_state():
    """The docs must not claim the package is unpublished while it is published.

    This is the drift that caused a long debugging loop: README, AGENTS.md and
    RELEASE-STATUS.md kept saying 'not yet on PyPI' after 0.1.0a7 shipped. The
    published state is asserted from the CHANGELOG: once a version's entry says
    the package is on PyPI, no authority file may claim otherwise.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    changelog = open(os.path.join(root, 'CHANGELOG.md')).read()
    if 'pypi.org/project/tamias' not in changelog:
        return 'PASS: publish-state check skipped (no published release recorded)'

    stale_phrases = (
        'not yet on PyPI',
        'Nothing is on real PyPI',
        'pip install tamias` will fail',
        'pip install tamias will fail',
    )
    errors = []
    for name in ('README.md', 'AGENTS.md', 'docs/RELEASE-STATUS.md', 'BLOCKED.md'):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        text = open(path).read()
        for phrase in stale_phrases:
            if phrase in text:
                errors.append(f'{name} still claims unpublished: {phrase!r}')
    if errors:
        return 'FAIL: contradictory publish state: ' + '; '.join(errors)
    return 'PASS: publish state not contradicted'


# Pytest will discover and run this function
def test_docs_integrity():
    results = []
    results.append(check_no_stale_line_citations())
    results.append(check_anchors_resolve())
    results.append(check_version_consistency())
    results.append(check_no_contradictory_publish_state())
    for r in results:
        print(r)
    if any('FAIL' in r for r in results):
        sys.exit(1)


if __name__ == '__main__':
    test_docs_integrity()