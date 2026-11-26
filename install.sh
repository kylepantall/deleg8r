#!/bin/sh
set -eu

repo=${DELEG8R_REPO_URL:-https://github.com/kylepantall/deleg8ter.git}
python=${DELEG8R_PYTHON:-python3}

if ! command -v git >/dev/null 2>&1 ||
    ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    echo 'deleg8r requires Git and Python 3.11 or newer.' >&2
    exit 1
fi

staging=$(mktemp -d "${TMPDIR:-/tmp}/deleg8r.XXXXXXXX")
trap 'rm -rf "$staging"' EXIT
trap 'exit 1' HUP INT TERM
git clone --quiet --depth 1 --no-tags --branch main -- "$repo" "$staging/source"
"$python" "$staging/source/installer.py" _install --source "$staging/source" "$@"
