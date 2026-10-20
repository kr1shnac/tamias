#!/bin/sh
# Claims guard for the project website (docs/index.html).
#
# The project's credibility rests on the page saying exactly what the code
# proves. This script fails when the page carries a claim TAMIAS-SSOT.md
# forbids, when a statement the page depends on goes missing, or when the
# version it quotes has drifted from pyproject.toml.
#
# Usage:
#   sh scripts/check-claims.sh            # offline checks (default)
#   sh scripts/check-claims.sh --online   # also curl the install source URL

set -eu

cd "$(dirname "$0")/.."

PAGE="docs/index.html"
fail=0

if [ ! -f "$PAGE" ]; then
    echo "FAIL: $PAGE is missing" >&2
    exit 1
fi

# --- Forbidden claims -------------------------------------------------------
# Each entry is an extended regex, matched case-insensitively.

check_absent() {
    # $1 = extended regex, $2 = human label
    if grep -Eqi "$1" "$PAGE"; then
        echo "FAIL: forbidden claim in $PAGE ($2):" >&2
        grep -Ein "$1" "$PAGE" >&2 || true
        fail=1
    fi
}

check_absent '\$[0-9]'                          'a dollar figure'
check_absent 'save(s)? you money'               'an unmeasured savings claim'
check_absent 'pip install tamias'               'the dead PyPI install command'
check_absent 'mission control'                  'simulated-dashboard branding'
check_absent 'waitlist'                         'a waitlist form'
check_absent 'v1\.2'                            'a version badge that matches no tag'
check_absent '30 seconds'                       'an unverified speed claim'
check_absent 'claude-[0-9]|haiku|gpt-[0-9]'     'a named-model routing claim'

# --- Required statements ----------------------------------------------------

require() {
    # $1 = literal string the page must contain
    if ! grep -Fq "$1" "$PAGE"; then
        echo "FAIL: required statement missing from $PAGE: $1" >&2
        fail=1
    fi
}

require "savings estimated, not yet measured"
require "127.0.0.1"

# --- Version drift ----------------------------------------------------------

version=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1)
if [ -z "$version" ]; then
    echo "FAIL: could not read the version from pyproject.toml" >&2
    fail=1
elif ! grep -Fq "tamias $version" "$PAGE"; then
    echo "FAIL: $PAGE does not quote the source version (expected: tamias $version)" >&2
    fail=1
fi

# --- Every repository link on the page must resolve to a real file ----------

base="https://github.com/kr1shnac/tamias/blob/main/"
for path in $(grep -o "$base[^\"]*" "$PAGE" | sed "s|^$base||" | sort -u); do
    if [ ! -f "$path" ]; then
        echo "FAIL: $PAGE links to $base$path but $path does not exist" >&2
        fail=1
    fi
done

# --- Optional online check --------------------------------------------------

case "${1-}" in
    --online)
        if ! curl -fsSL --max-time 20 -o /dev/null https://github.com/kr1shnac/tamias; then
            echo "FAIL: the install source the page points at is unreachable" >&2
            fail=1
        fi
        ;;
    "")
        ;;
    *)
        echo "usage: sh scripts/check-claims.sh [--online]" >&2
        exit 2
        ;;
esac

if [ "$fail" -ne 0 ]; then
    exit 1
fi

echo "PASS: $PAGE carries no forbidden claim and quotes version ${version:-<unreadable>}"
