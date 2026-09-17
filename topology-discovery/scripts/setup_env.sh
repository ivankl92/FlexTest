#!/usr/bin/env bash
# Create the Python environment the discovery tool needs.
#
# Everything is installed into a virtualenv inside this subproject
# (topology-discovery/.venv). Nothing is installed system-wide and nothing
# outside this directory is touched -- the i226-adaptation measurement
# environment on this machine is deliberately left alone.
#
# Two dependencies: ncclient (NETCONF client) and lxml (XML). Both are pure
# installs from PyPI; lxml ships wheels, so no compiler is needed.
#
# If the testbed network has no route to PyPI, see RUNBOOK.md §3.2 for the
# offline procedure (download wheels elsewhere, copy the directory over,
# install with --no-index --find-links).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"
WHEELS="${WHEELS:-}"

echo "Setting up $VENV"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found" >&2
    exit 2
fi

if ! python3 -c 'import venv' 2>/dev/null; then
    echo "error: the python3 venv module is missing." >&2
    echo "       on Debian/Ubuntu:  sudo apt-get install python3-venv" >&2
    exit 2
fi

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    echo "created virtualenv"
else
    echo "virtualenv already exists, reusing it"
fi

PIP="$VENV/bin/pip"
"$PIP" install --quiet --upgrade pip setuptools wheel

if [ -n "$WHEELS" ]; then
    echo "installing from local wheel directory $WHEELS (offline)"
    "$PIP" install --quiet --no-index --find-links "$WHEELS" \
        -r "$ROOT/requirements.txt"
else
    echo "installing from PyPI"
    "$PIP" install --quiet -r "$ROOT/requirements.txt"
fi

echo
"$VENV/bin/python3" - <<'PYEOF'
import ncclient, lxml.etree
print(f"ncclient {getattr(ncclient, '__version__', '?')}")
print(f"lxml     {lxml.etree.__version__}")
print("environment OK")
PYEOF

echo
echo "Next:"
echo "  scripts/preflight.sh        # check the switches answer"
echo "  scripts/run_discovery.sh    # run discovery"
