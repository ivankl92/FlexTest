#!/usr/bin/env bash
# Run a full topology and capability discovery.
#
#   scripts/run_discovery.sh                    # every switch in SYSTEM.md
#   scripts/run_discovery.sh --switch SW1=192.168.1.10
#   scripts/run_discovery.sh --full-dump        # also capture an unfiltered <get>
#
# Any option is passed straight through to tsn_discovery.cli, so
# `scripts/run_discovery.sh --help` shows everything.
#
# Credentials: set NETCONF_PASSWORD in the environment to override the lab
# default. Putting a password on the command line leaves it in the shell
# history and in /proc, so prefer the environment variable.
#
# Exit: 0 clean, 1 discovery ran but something needs attention,
#       2 could not run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"

cd "$ROOT"

if [ -x "$VENV/bin/python3" ]; then
    PY="$VENV/bin/python3"
else
    PY="python3"
    if ! "$PY" -c 'import ncclient' 2>/dev/null; then
        echo "error: ncclient is not available and there is no virtualenv." >&2
        echo "       run scripts/setup_env.sh first." >&2
        exit 2
    fi
    echo "note: no virtualenv, using the system python3" >&2
fi

exec "$PY" -m tsn_discovery.cli \
    --inventory "${INVENTORY:-$ROOT/../SYSTEM.md}" \
    "$@"
