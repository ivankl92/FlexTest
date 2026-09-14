#!/usr/bin/env bash
# Preflight for TSN topology discovery.
#
# Checks, in order:
#   1. the local toolchain (python3, ncclient, optionally netopeer2-cli)
#   2. IP reachability of every device listed in SYSTEM.md
#   3. TCP reachability of the NETCONF port on every switch
#   4. a real NETCONF session to each switch, if netopeer2-cli is available
#
# Run this before the first discovery, after any rewiring, and whenever
# discovery reports a switch unreachable. It touches nothing: no
# configuration is read or written beyond a NETCONF <hello>.
#
# Exit: 0 all checks passed, 1 something failed, 2 cannot run.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
INVENTORY="${INVENTORY:-$ROOT/../SYSTEM.md}"
NETCONF_PORT="${NETCONF_PORT:-830}"
NETCONF_USER="${NETCONF_USER:-netconf}"
NETCONF_PASSWORD="${NETCONF_PASSWORD:-geheim}"
PING_TIMEOUT="${PING_TIMEOUT:-1}"
TCP_TIMEOUT="${TCP_TIMEOUT:-3}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YEL=""; RST=""; }

fail=0
warn=0

ok()   { printf '  %sPASS%s  %s\n' "$GRN" "$RST" "$*"; }
bad()  { printf '  %sFAIL%s  %s\n' "$RED" "$RST" "$*"; fail=$((fail+1)); }
note() { printf '  %sWARN%s  %s\n' "$YEL" "$RST" "$*"; warn=$((warn+1)); }
hdr()  { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------- toolchain
hdr "1. Local toolchain"

if ! command -v python3 >/dev/null 2>&1; then
    bad "python3 not found"
    exit 2
fi
ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"

if [ -x "$ROOT/.venv/bin/python3" ]; then
    PY="$ROOT/.venv/bin/python3"
    ok "virtualenv at $ROOT/.venv"
else
    PY="python3"
    note "no virtualenv at $ROOT/.venv — using the system python3. Run scripts/setup_env.sh to create one."
fi

if "$PY" -c 'import ncclient' 2>/dev/null; then
    ok "ncclient $("$PY" -c 'import ncclient,sys;print(getattr(ncclient,"__version__","?"))')"
else
    bad "ncclient not importable by $PY — run scripts/setup_env.sh"
fi

if "$PY" -c 'import lxml' 2>/dev/null; then
    ok "lxml present"
else
    bad "lxml not importable by $PY — run scripts/setup_env.sh"
fi

if command -v netopeer2-cli >/dev/null 2>&1; then
    ok "netopeer2-cli at $(command -v netopeer2-cli)"
    HAVE_NETOPEER=1
else
    note "netopeer2-cli not on PATH — the interactive cross-check in step 4 will be skipped. Discovery itself does not need it."
    HAVE_NETOPEER=0
fi

# ---------------------------------------------------------------- inventory
hdr "2. Inventory"

if [ ! -f "$INVENTORY" ]; then
    bad "inventory not found: $INVENTORY (set INVENTORY=/path/to/SYSTEM.md)"
    exit 2
fi
ok "inventory $INVENTORY"

# Pull "NAME IP" pairs out of SYSTEM.md. Continuation lines (a second port
# with no name) inherit the previous name with a "-b" suffix for reporting.
mapfile -t ROWS < <(awk '
    match($0, /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/) {
        ip = substr($0, RSTART, RLENGTH)
        head = substr($0, 1, RSTART-1)
        gsub(/^[ \t]+|[ \t]+$/, "", head)
        if (head ~ /^[A-Za-z][A-Za-z0-9_-]*/) {
            match(head, /^[A-Za-z][A-Za-z0-9_-]*/)
            name = substr(head, RSTART, RLENGTH)
            last = name; n = 0
        } else {
            n++; name = last "-" (n+1)
        }
        print name, ip
    }' "$INVENTORY")

if [ "${#ROWS[@]}" -eq 0 ]; then
    bad "no addresses parsed from $INVENTORY"
    exit 2
fi
ok "${#ROWS[@]} addresses parsed"

# --------------------------------------------------------------- ip reach
hdr "3. IP reachability (ICMP)"

SWITCH_IPS=()
for row in "${ROWS[@]}"; do
    name="${row%% *}"
    ip="${row##* }"
    if ping -c 1 -W "$PING_TIMEOUT" "$ip" >/dev/null 2>&1; then
        ok "$(printf '%-8s %-15s' "$name" "$ip")"
    else
        # An endpoint that is powered off is not a discovery failure; a
        # switch that does not answer is.
        if [[ "$name" =~ ^SW[0-9]+ ]]; then
            bad "$(printf '%-8s %-15s' "$name" "$ip") no ICMP reply"
        else
            note "$(printf '%-8s %-15s' "$name" "$ip") no ICMP reply (endpoint — may simply be off)"
        fi
    fi
    [[ "$name" =~ ^SW[0-9]+$ ]] && SWITCH_IPS+=("$name $ip")
done

# ------------------------------------------------------------- netconf port
hdr "4. NETCONF port ${NETCONF_PORT}/tcp"

for row in "${SWITCH_IPS[@]}"; do
    name="${row%% *}"
    ip="${row##* }"
    if timeout "$TCP_TIMEOUT" bash -c \
           "exec 3<>/dev/tcp/$ip/$NETCONF_PORT" 2>/dev/null; then
        ok "$(printf '%-8s %-15s' "$name" "$ip") port $NETCONF_PORT open"
    else
        bad "$(printf '%-8s %-15s' "$name" "$ip") port $NETCONF_PORT closed or filtered — is the NETCONF server enabled? (ISTAX: configure terminal / netconf server)"
    fi
done

# ---------------------------------------------------------- netconf session
hdr "5. NETCONF session"

for row in "${SWITCH_IPS[@]}"; do
    name="${row%% *}"
    ip="${row##* }"
    out="$("$PY" - "$ip" "$NETCONF_PORT" "$NETCONF_USER" "$NETCONF_PASSWORD" <<'PYEOF' 2>&1
import sys
try:
    from ncclient import manager
except Exception as exc:
    print(f"SKIP ncclient unavailable: {exc}")
    sys.exit(0)
host, port, user, pw = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    with manager.connect(host=host, port=port, username=user, password=pw,
                         hostkey_verify=False, allow_agent=False,
                         look_for_keys=False, timeout=15,
                         device_params={"name": "default"}) as m:
        caps = [str(c) for c in m.server_capabilities]
        mods = sorted({c.split("module=")[1].split("&")[0]
                       for c in caps if "module=" in c})
        tsn = [x for x in mods if "dot1q" in x or "dot1ab" in x]
        print(f"OK session={m.session_id} modules={len(mods)} tsn={','.join(tsn) or 'none'}")
except Exception as exc:
    print(f"ERR {type(exc).__name__}: {exc}")
PYEOF
)"
    case "$out" in
        OK*)   ok  "$(printf '%-8s %-15s' "$name" "$ip") ${out#OK }" ;;
        SKIP*) note "$(printf '%-8s %-15s' "$name" "$ip") ${out#SKIP }" ;;
        *)     bad "$(printf '%-8s %-15s' "$name" "$ip") ${out#ERR }" ;;
    esac
done

if [ "$HAVE_NETOPEER" -eq 1 ]; then
    hdr "6. netopeer2-cli cross-check (first switch only)"
    if [ "${#SWITCH_IPS[@]}" -gt 0 ]; then
        first="${SWITCH_IPS[0]}"
        ip="${first##* }"
        # netopeer2-cli is interactive; drive it with a here-doc. A
        # successful run prints the ietf-system subtree.
        if out=$(printf 'connect --host %s --login %s\nget --filter-xpath "/ietf-system:system/*"\ndisconnect\nquit\n' \
                        "$ip" "$NETCONF_USER" \
                 | timeout 25 netopeer2-cli 2>&1); then
            if grep -q "<system" <<<"$out"; then
                ok "netopeer2-cli retrieved /ietf-system:system from $ip"
            else
                note "netopeer2-cli ran against $ip but returned no <system> element. It usually prompts for the password interactively — this check is best-effort; the ncclient result in step 5 is authoritative."
            fi
        else
            note "netopeer2-cli against $ip did not complete non-interactively (it prompts for a password). Step 5 is the authoritative check."
        fi
    fi
fi

# -------------------------------------------------------------------- verdict
printf '\n'
if [ "$fail" -gt 0 ]; then
    printf '%sPREFLIGHT FAILED%s — %d failure(s), %d warning(s)\n' "$RED" "$RST" "$fail" "$warn"
    printf 'Discovery will not produce a complete result until these are fixed.\n'
    exit 1
fi
printf '%sPREFLIGHT PASSED%s — 0 failures, %d warning(s)\n' "$GRN" "$RST" "$warn"
printf 'Run: scripts/run_discovery.sh\n'
exit 0
