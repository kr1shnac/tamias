"""Doc-integrity test: assert that the version agrees across the authority
layer and that every relative markdown link in the shipped docs resolves.

Runs as part of the regular gate: .venv/bin/python -m pytest -q.
Placed under tests/ so it is discovered alongside the other tests.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = 'README.md'


def _read(relative: str) -> str:
    return open(os.path.join(ROOT, relative), encoding="utf-8").read()


def check_version_consistency():
    """The source version must agree across the authority layer.

    The version is read dynamically so this check does not need editing at
    every release; it only asserts that `pyproject.toml` and
    `src/tamias/__init__.py` agree, and that the installed distribution (when
    one is present) carries the same version.
    """
    errors = []
    pyproject = _read('pyproject.toml')
    init = _read('src/tamias/__init__.py')

    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    if not m:
        return 'FAIL: no version found in pyproject.toml'
    version = m.group(1)

    init_version = re.search(r'^__version__ = "([^"]+)"', init, re.M)
    if not init_version or init_version.group(1) != version:
        errors.append(
            f'__init__.__version__ {init_version.group(1) if init_version else None!r}'
            f' != pyproject version {version!r}'
        )

    # The installed distribution records the version at install time; a bump
    # that forgets to reinstall leaves it stale, which is worth catching.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        try:
            installed = dist_version('tamias')
        except PackageNotFoundError:
            installed = None
        if installed is not None and installed != version:
            errors.append(
                f'installed metadata version {installed!r} != source version {version!r} '
                '(rerun: pip install -e ".[dev]")'
            )
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.11+
        pass

    if errors:
        return 'FAIL: version inconsistency: ' + '; '.join(errors)
    return f'PASS: version consistency ({version})'


def _doc_files():
    """Every markdown file a reader is likely to open from the repository root."""
    names = [README]
    docs = os.path.join(ROOT, 'docs')
    for entry in sorted(os.listdir(docs)):
        if entry.endswith('.md'):
            names.append(f'docs/{entry}')
    return names


def _relative_links(text: str):
    for target in re.findall(r'\]\(([^)]+)\)', text):
        target = target.split('#', 1)[0].strip()
        if not target:
            continue
        if re.match(r'^[a-z][a-z0-9+.-]*://', target) or target.startswith('mailto:'):
            continue
        if target.startswith('/'):
            continue
        yield target


def check_relative_links_resolve():
    """Every relative markdown link in README.md and docs/*.md must exist."""
    errors = []
    for name in _doc_files():
        base = os.path.dirname(os.path.join(ROOT, name))
        for target in _relative_links(_read(name)):
            path = os.path.normpath(os.path.join(base, target))
            if not os.path.exists(path):
                errors.append(f'{name}: link target missing: {target}')
    if errors:
        return 'FAIL: broken relative links: ' + '; '.join(errors)
    return 'PASS: relative links resolve'


def check_budget_guarantee_phrases():
    """The cap guarantee must carry all five qualifications, in the README.

    The guarantee is the project's strongest claim; a four-item list is wrong.
    These fragments are the load-bearing half of each qualification, so a
    rewrite that drops one fails the gate instead of shipping quietly.
    """
    readme = _read(README)
    budget = _read('docs/BUDGET.md')
    verified = _read('docs/VERIFICATION.md')
    required = [
        ('overshoot bound', 'settled - ceiling'),
        ('unpriced model refusal', 'cannot price'),
        ('passthrough 403', '403'),
        ('cap, not unbypassability', 'not about the proxy being'),
        ('computed cost, not the bill', 'computed cost, not the bill'),
    ]
    errors = [f'README.md: guarantee qualification missing: {label} ({fragment!r})'
              for label, fragment in required if fragment not in readme]
    if 'names a model' not in readme:
        errors.append('README.md: passthrough qualification must name the model case')
    for name, text in (('docs/BUDGET.md', budget), ('docs/VERIFICATION.md', verified)):
        if 'UNTESTED live' not in text:
            errors.append(f'{name}: the live-untested caveat must stay stated')
    if errors:
        return 'FAIL: ' + '; '.join(errors)
    return 'PASS: the cap guarantee carries all five qualifications'


def test_docs_integrity():
    results = [
        check_version_consistency(),
        check_relative_links_resolve(),
        check_budget_guarantee_phrases(),
    ]
    for r in results:
        print(r)
    if any('FAIL' in r for r in results):
        sys.exit(1)


if __name__ == '__main__':
    test_docs_integrity()
