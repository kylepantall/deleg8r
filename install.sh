#!/bin/sh
set -eu

revision_url=${DELEG8R_REVISION_URL:-https://api.github.com/repos/kylepantall/deleg8r/commits/main}
archive_base=${DELEG8R_ARCHIVE_BASE:-https://codeload.github.com/kylepantall/deleg8r/tar.gz}

if ! command -v curl >/dev/null 2>&1; then
    echo 'deleg8r: curl is required to download updates.' >&2
    exit 1
fi

compatible_python() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1
}

python=${DELEG8R_PYTHON:-}
if [ -z "$python" ]; then
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8; do
        if compatible_python "$candidate"; then
            python=$candidate
            break
        fi
    done
fi
if [ -z "$python" ] || ! compatible_python "$python"; then
    echo 'deleg8r: Python 3.8 or newer is required.' >&2
    echo 'If Python is installed outside PATH, set DELEG8R_PYTHON to the full path when running sh.' >&2
    exit 1
fi

staging=$(mktemp -d "${TMPDIR:-/tmp}/deleg8r.XXXXXXXX")
trap 'rm -rf "$staging"' EXIT
trap 'exit 1' HUP INT TERM

curl -fsSL "$revision_url" -o "$staging/revision.json"
revision=$("$python" -c '
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as response:
    revision = json.load(response).get("sha", "")
if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
    sys.exit("deleg8r: the source server returned an invalid revision")
print(revision)
' "$staging/revision.json")
curl -fsSL "$archive_base/$revision" -o "$staging/source.tar.gz"

# Copy only regular release files from the archive. The archive's root name varies.
"$python" - "$staging" <<'PY'
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

folder = Path(sys.argv[1])
names = {"delegate.py", "models.py", "models.json", "store.py", "installer.py", "install.sh", "README.md"}
seen = set()
with tarfile.open(folder / "source.tar.gz", "r:gz") as archive:
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or len(path.parts) != 2 or path.name not in names:
            continue
        if not member.isfile() or path.name in seen:
            sys.exit("deleg8r: invalid release archive entry")
        with archive.extractfile(member) as source, (folder / path.name).open("wb") as target:
            shutil.copyfileobj(source, target)
        seen.add(path.name)
if seen != names:
    sys.exit("deleg8r: release archive is missing required files")
PY

"$python" "$staging/installer.py" _install --source "$staging" --revision "$revision" "$@"
