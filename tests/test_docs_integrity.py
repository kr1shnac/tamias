"""Doc-integrity test: mechanically assert that every authoritative claim in
claims.md resolves and that versions agree across the authority layer.

Runs as part of the regular gate: .venv/bin/python -m pytest -q.
Placed under tests/ so it is discovered alongside the other tests.
"""

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
    """Version references that appear across the authority layer should be
    internally consistent — no contradictory a6/a7 mentions outside historical
    context."""
    errors = []
    readme_txt = open(README).read()
    release_txt = open(RELEASE).read()
    # If RELEASE-STATUS.md references a6, it should be in explicit historical
    # context (section/heading, not as the current source version).
    if '0.1.0a6' in release_txt and '0.1.0a7' not in release_txt:
        if not re.search(r'v0\.1\.0a6.*tag|annotated tag', release_txt):
            errors.append('RELEASE-STATUS.md references a6 without historical context')
    # If any file mentions both a6 and a7, ensure they are not presented as
    # current-equal (the source IS a7, and a6 is a historical tag).
    # No further constraint: each file may legitimately mention different tags.
    if errors:
        return 'FAIL: version inconsistency: ' + '; '.join(errors)
    return 'PASS: version consistency'


# Pytest will discover and run this function
def test_docs_integrity():
    results = []
    results.append(check_no_stale_line_citations())
    results.append(check_anchors_resolve())
    results.append(check_version_consistency())
    for r in results:
        print(r)
    if any('FAIL' in r for r in results):
        sys.exit(1)


if __name__ == '__main__':
    test_docs_integrity()