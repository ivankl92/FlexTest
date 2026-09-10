#!/usr/bin/env bash
# run_measurement.sh - drive the TSN-FlexTest I226 measurement campaign from PC1.
#
# For every combination of QoS mode and background load it:
#   1. verifies gPTP is locked on both nodes,
#   2. applies the host QoS configuration on both nodes,
#   3. starts the hardware-timestamping listener on PC2,
#   4. starts the iperf3 background load across the shared inter-switch link,
#   5. sends the paced measurement stream from PC1 with hardware TX timestamps,
#   6. collects both CSVs plus metadata into results/<run-id>/<qos>_<load>/.
#
# Usage: sudo ./run_measurement.sh [--config config.conf] [--quick]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.conf"
QUICK=0
INSTALL_DIR="/opt/tsn-flextest"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --quick)  QUICK=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

# shellcheck disable=SC1090
source "$CONFIG"

log()  { printf '\033[1;34m[run ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"

PC2="${PC2_USER}@${PC2_MGMT}"
RSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

if [[ $QUICK -eq 1 ]]; then
  BG_LOADS="0 95"
  STREAM_DURATION=10
  log "--quick: loads='$BG_LOADS' duration=${STREAM_DURATION}s"
fi

# ---------------------------------------------------------------------------
log "checking ssh access to PC2 ($PC2)"
$RSH "$PC2" true || die "passwordless ssh to $PC2 does not work - run bootstrap_ssh.sh first"

log "discovering MAC addresses"
PC1_MAC=$(cat "/sys/class/net/${PC1_STREAM_IF}/address")
PC2_MAC=$($RSH "$PC2" "cat /sys/class/net/${PC2_STREAM_IF}/address")
[[ -n "$PC2_MAC" ]] || die "could not read PC2 MAC"
log "  PC1 $PC1_STREAM_IF = $PC1_MAC"
log "  PC2 $PC2_STREAM_IF = $PC2_MAC"

BG_SPEED=$(cat "/sys/class/net/${PC1_BG_IF}/speed" 2>/dev/null || echo 1000)
[[ "$BG_SPEED" =~ ^[0-9]+$ ]] || BG_SPEED=1000
log "  background port link rate: ${BG_SPEED} Mbit/s"

# ---------------------------------------------------------------------------
ptp_offset() {   # $1 = "local" | "remote"
  local cmd="pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET CURRENT_DATA_SET'"
  local out
  if [[ "$1" == "local" ]]; then out=$(eval "$cmd" 2>/dev/null || true)
  else out=$($RSH "$PC2" "sudo $cmd" 2>/dev/null || true); fi
  echo "$out" | grep -E 'offsetFromMaster' | awk '{print $2}' | head -1
}
ptp_state() {
  local cmd="pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET PORT_DATA_SET'"
  local out
  if [[ "$1" == "local" ]]; then out=$(eval "$cmd" 2>/dev/null || true)
  else out=$($RSH "$PC2" "sudo $cmd" 2>/dev/null || true); fi
  echo "$out" | grep -E 'portState' | awk '{print $2}' | head -1
}

# A ptp4l port that hits a transport error — most often "timed out while
# polling for tx timestamp" when the NIC's TX-timestamp path is saturated —
# goes to portState FAULTY. Observed on PC 1 at 105 % background load, where it
# then stayed FAULTY for the rest of the campaign and every remaining point was
# skipped. ptp4l's own fault_reset_interval did not bring it back, so restart
# the unit once and give it time to re-acquire.
ptp_recover() {   # $1 = "local" | "remote", $2 = interface
  local where="$1" ifc="$2"
  warn "  ptp4l on ${where} port ${ifc} is FAULTY - restarting the unit once"
  if [[ "$where" == "local" ]]; then
    systemctl reset-failed "ptp4l@${ifc}" 2>/dev/null || true
    systemctl restart "ptp4l@${ifc}" 2>/dev/null || true
  else
    $RSH "$PC2" "sudo systemctl reset-failed ptp4l@${ifc}; sudo systemctl restart ptp4l@${ifc}" \
      2>/dev/null || true
  fi
  sleep 20
}

check_ptp() {
  local s1 s2 o1 o2
  s1=$(ptp_state local);  o1=$(ptp_offset local)
  s2=$(ptp_state remote); o2=$(ptp_offset remote)
  log "  gPTP: PC1 state=${s1:-?} offset=${o1:-?} ns | PC2 state=${s2:-?} offset=${o2:-?} ns"
  PTP_LAST="PC1:${s1:-?}/${o1:-?} PC2:${s2:-?}/${o2:-?}"

  # Try once to clear a FAULTY port before reporting failure, otherwise a single
  # fault poisons every subsequent measurement point.
  if [[ "${PTP_NO_RECOVER:-0}" != "1" ]]; then
    local recovered=0
    [[ "$s1" == "FAULTY" ]] && { ptp_recover local  "$PC1_STREAM_IF"; recovered=1; }
    [[ "$s2" == "FAULTY" ]] && { ptp_recover remote "$PC2_STREAM_IF"; recovered=1; }
    if (( recovered )); then
      PTP_NO_RECOVER=1 check_ptp
      return $?
    fi
  fi

  for st in "$s1" "$s2"; do
    [[ "$st" == "SLAVE" || "$st" == "CLIENT" ]] || return 1
  done
  for of in "$o1" "$o2"; do
    local a=${of#-}; a=${a%%.*}
    [[ "$a" =~ ^[0-9]+$ ]] || return 1
    (( a < 1000 )) || return 1
  done
  return 0
}

# ---------------------------------------------------------------------------
# Preflight: confirm priority-tagged frames actually traverse both switches.
preflight() {
  log "preflight: 200 priority-tagged frames PC1 -> PC2"
  $RSH "$PC2" "sudo $INSTALL_DIR/tsn_rx -i ${PC2_STREAM_IF} -t 12 -n 200 -o /tmp/pf_rx.csv" \
       >/tmp/pf_rx.log 2>&1 &
  local rxpid=$!
  sleep 2
  "$INSTALL_DIR/tsn_tx" -i "$PC1_STREAM_IF" -d "$PC2_MAC" -n 200 -r 200 \
      -s "$STREAM_SIZE" -p "$STREAM_PCP_LOW" -v "$STREAM_VID" -q "$STREAM_SOCKPRIO" \
      -o /tmp/pf_tx.csv >/tmp/pf_tx.log 2>&1 || true
  wait $rxpid 2>/dev/null || true
  local got
  got=$($RSH "$PC2" "wc -l < /tmp/pf_rx.csv" 2>/dev/null || echo 0)
  got=$((got - 1))
  log "  received $got / 200 tagged frames"
  if (( got < 100 )); then
    warn "  tagged frames are not getting through - retrying untagged"
    $RSH "$PC2" "sudo $INSTALL_DIR/tsn_rx -i ${PC2_STREAM_IF} -t 12 -n 200 -o /tmp/pf_rx.csv" \
         >/tmp/pf_rx.log 2>&1 &
    rxpid=$!
    sleep 2
    "$INSTALL_DIR/tsn_tx" -i "$PC1_STREAM_IF" -d "$PC2_MAC" -n 200 -r 200 \
        -s "$STREAM_SIZE" -p -1 -q "$STREAM_SOCKPRIO" -o /tmp/pf_tx.csv >/tmp/pf_tx.log 2>&1 || true
    wait $rxpid 2>/dev/null || true
    got=$($RSH "$PC2" "wc -l < /tmp/pf_rx.csv" 2>/dev/null || echo 0); got=$((got - 1))
    if (( got < 100 )); then
      cat /tmp/pf_tx.log >&2; $RSH "$PC2" "cat /tmp/pf_rx.log" >&2 || true
      die "no measurement frames arrive at PC2 at all - check cabling and switch forwarding"
    fi
    die "untagged frames pass but priority-tagged (VID ${STREAM_VID}) frames do not.
     The KSwitch ports are dropping tagged traffic. Either allow VLAN ${STREAM_VID}
     / priority-tagged frames on the ports, or pick a real VLAN id and set
     STREAM_VID in config.conf."
  fi
  # confirm hardware timestamps were actually used
  if $RSH "$PC2" "tail -n +2 /tmp/pf_rx.csv | head -1" | grep -q ',sw$'; then
    warn "  PC2 fell back to SOFTWARE RX timestamps - accuracy will be degraded"
  fi
  if grep -q ',sw$' /tmp/pf_tx.csv 2>/dev/null; then
    warn "  PC1 fell back to SOFTWARE TX timestamps - accuracy will be degraded"
  fi
  log "  preflight OK"
}

# ---------------------------------------------------------------------------
RUN_ID=$(date +%Y%m%d-%H%M%S)
OUTDIR="${RESULT_ROOT}/${RUN_ID}"
mkdir -p "$OUTDIR"
log "results -> $OUTDIR"

log "initial gPTP check"
if ! check_ptp; then
  warn "gPTP not locked yet, waiting up to 120 s"
  for _ in $(seq 1 24); do sleep 5; check_ptp && break; done
  check_ptp || die "gPTP never locked on both nodes - fix synchronisation before measuring"
fi

preflight

cleanup() {
  pkill -f "iperf3 -c" 2>/dev/null || true
  $RSH "$PC2" "sudo pkill -f tsn_rx" 2>/dev/null || true
}
trap cleanup EXIT

REPS=${REPETITIONS:-1}
[[ "$REPS" =~ ^[0-9]+$ ]] && (( REPS >= 1 )) || REPS=1

# Repetitions are interleaved, not blocked: the outer loop is the repetition,
# so a full sweep of every (QoS, load) point completes before the next round
# starts. Blocking them (three runs of the same point back to back) would let
# any slow drift in the background load masquerade as a difference between
# configurations - which is exactly the artefact that made a 10 us "802.1p
# effect" appear at 50 % load with 802.1Q disabled on the switch.
TOTAL=0
for _q in $QOS_MODES; do for _l in $BG_LOADS; do TOTAL=$((TOTAL+1)); done; done
TOTAL=$(( TOTAL * REPS ))
N=0
(( REPS > 1 )) && log "repetitions: $REPS per point, interleaved (round 1 of all points, then round 2, ...)"

for REP in $(seq 1 "$REPS"); do
(( REPS > 1 )) && log "===== repetition round $REP of $REPS ====="

for QOS in $QOS_MODES; do
  if [[ "$QOS" == "dot1p" ]]; then PCP="$STREAM_PCP_HIGH"; else PCP="$STREAM_PCP_LOW"; fi

  log "applying host QoS mode '$QOS'"
  "$INSTALL_DIR/qos_config.sh" "$PC1_STREAM_IF" "$QOS"
  $RSH "$PC2" "sudo $INSTALL_DIR/qos_config.sh ${PC2_STREAM_IF} ${QOS}"

  for LOAD in $BG_LOADS; do
    N=$((N+1))
    LABEL="${QOS}_load${LOAD}"
    (( REPS > 1 )) && LABEL="${LABEL}_r${REP}"
    CASE_DIR="${OUTDIR}/${LABEL}"
    mkdir -p "$CASE_DIR"
    MBPS=$(( BG_SPEED * LOAD / 100 ))
    PER_STREAM=$(( MBPS / BG_STREAMS ))
    (( PER_STREAM < 1 )) && PER_STREAM=1

    log "[$N/$TOTAL] $LABEL: pcp=$PCP background=${MBPS} Mbit/s (${LOAD}% of ${BG_SPEED})"

    if ! check_ptp; then
      warn "  gPTP lost sync - waiting 60 s and retrying once"
      sleep 60
      check_ptp || { warn "  skipping $LABEL (no gPTP lock)"; echo "SKIPPED: no gPTP lock" > "$CASE_DIR/ERROR"; continue; }
    fi
    PTP_BEFORE="$PTP_LAST"

    # 1. listener on PC2
    $RSH "$PC2" "sudo $INSTALL_DIR/tsn_rx -i ${PC2_STREAM_IF} -t $((STREAM_DURATION + 12)) -o /tmp/rx_${LABEL}.csv" \
         >"${CASE_DIR}/rx.log" 2>&1 &
    RXPID=$!
    sleep 3

    # 2. background load on the second port pair (shares the inter-switch link)
    IPERFPID=""
    if (( MBPS > 0 )); then
      iperf3 -c "$PC2_BG_IP" -B "$PC1_BG_IP" -u -b "${PER_STREAM}M" -l "$BG_DGRAM" \
             -P "$BG_STREAMS" -t $((STREAM_DURATION + 6)) --json \
             >"${CASE_DIR}/iperf3.json" 2>"${CASE_DIR}/iperf3.err" &
      IPERFPID=$!
      sleep 3
    fi

    # 3. measured stream
    "$INSTALL_DIR/tsn_tx" -i "$PC1_STREAM_IF" -d "$PC2_MAC" \
        -n $(( STREAM_RATE * STREAM_DURATION )) -r "$STREAM_RATE" -s "$STREAM_SIZE" \
        -p "$PCP" -v "$STREAM_VID" -q "$STREAM_SOCKPRIO" -N "${STREAM_TS_EVERY:-1}" \
        -o "${CASE_DIR}/tx.csv" >"${CASE_DIR}/tx.log" 2>&1 || warn "  tsn_tx returned non-zero"

    [[ -n "$IPERFPID" ]] && { wait "$IPERFPID" 2>/dev/null || true; }
    wait $RXPID 2>/dev/null || true

    # 4. collect
    scp -q -o BatchMode=yes "${PC2}:/tmp/rx_${LABEL}.csv" "${CASE_DIR}/rx.csv" || \
      warn "  could not fetch rx.csv"
    # tsn_rx ran under sudo, so /tmp/rx_*.csv on PC 2 is owned by root. /tmp is
    # sticky, so the unprivileged ssh user cannot unlink another user's file
    # there and plain `rm` fails with EPERM ("Operation not permitted"). Remove
    # it with the same privilege that created it.
    $RSH "$PC2" "sudo rm -f /tmp/rx_${LABEL}.csv" || true

    check_ptp || true
    # TXN/RXN count CSV rows, i.e. recovered timestamps — NOT frames sent.
    # SENT is what tsn_tx actually put on the wire, read back from its own
    # report. Loss must be measured against SENT: dividing by TXN makes a
    # missing TX timestamp look like a negative loss.
    TXN=$(( $(wc -l < "${CASE_DIR}/tx.csv" 2>/dev/null || echo 1) - 1 ))
    RXN=$(( $(wc -l < "${CASE_DIR}/rx.csv" 2>/dev/null || echo 1) - 1 ))
    SENT=$(grep -o 'sent=[0-9]*' "${CASE_DIR}/tx.log" 2>/dev/null | head -1 | cut -d= -f2)
    SENT=${SENT:-0}
    # With sampled timestamping (STREAM_TS_EVERY > 1) far fewer timestamps are
    # requested than frames sent, by design. Yield must be measured against the
    # requests or a healthy sampled run looks like a total failure.
    TSREQ=$(grep -o 'ts_requested=[0-9]*' "${CASE_DIR}/tx.log" 2>/dev/null | head -1 | cut -d= -f2)
    TSREQ=${TSREQ:-$SENT}

    cat > "${CASE_DIR}/meta.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "label": "${LABEL}",
  "qos_mode": "${QOS}",
  "pcp": ${PCP},
  "vid": ${STREAM_VID},
  "background_load_percent": ${LOAD},
  "background_mbps": ${MBPS},
  "background_streams": ${BG_STREAMS},
  "link_speed_mbps": ${BG_SPEED},
  "stream_rate_pps": ${STREAM_RATE},
  "stream_frame_bytes": ${STREAM_SIZE},
  "stream_duration_s": ${STREAM_DURATION},
  "repetition": ${REP},
  "repetitions_total": ${REPS},
  "ts_every": ${STREAM_TS_EVERY:-1},
  "frames_sent": ${SENT},
  "tx_timestamps": ${TXN},
  "rx_timestamps": ${RXN},
  "ptp_before": "${PTP_BEFORE}",
  "ptp_after": "${PTP_LAST}"
}
EOF
    # Report the three numbers separately so a shortfall in TX-timestamp yield
    # cannot be mistaken for frame loss (or hidden by it). "rx greater than
    # tx_ts" is not a paradox: some frames went out and arrived while their TX
    # timestamp was never retrieved.
    if (( SENT > 0 )); then
      YIELD=$(LC_ALL=C awk -v a="$TXN" -v b="$TSREQ" 'BEGIN{printf "%.2f", (b>0)?100*a/b:0}')
      LOSS=$(LC_ALL=C awk -v a="$RXN" -v b="$SENT" 'BEGIN{printf "%.3f", 100*(1-a/b)}')
      log "  sent=${SENT} ts_req=${TSREQ} tx_ts=${TXN} (${YIELD}% yield) rx=${RXN} (${LOSS}% loss)"
    else
      log "  tx_ts=${TXN} rx=${RXN}"
    fi
    (( RXN < 1 )) && warn "  no frames received for $LABEL"

    # A collapsed TX-timestamp yield means most frames have no tx_hw_ns and are
    # dropped by the offline join, so the point is built from an unrepresentative
    # subset. Mark it in the run directory rather than letting it look normal.
    if (( SENT > 0 )); then
      if [[ $(LC_ALL=C awk -v a="$TXN" -v b="$TSREQ" 'BEGIN{print (b>0 && 100*a/b < 90) ? 1 : 0}') == 1 ]]; then
        warn "  TX-timestamp yield ${YIELD}% (<90%) - this point is NOT trustworthy"
        warn "  the offline join keeps only timestamped frames, so it samples a biased subset"
        echo "TX timestamp yield ${YIELD}% (${TXN}/${SENT}); below the 90% threshold." \
          > "${CASE_DIR}/DEGRADED"
      fi
    fi

    sleep 5   # cool down, matches the original testbed's inter-run pause
  done
done
done   # repetition round

# restore a clean state
"$INSTALL_DIR/qos_config.sh" "$PC1_STREAM_IF" none || true
$RSH "$PC2" "sudo $INSTALL_DIR/qos_config.sh ${PC2_STREAM_IF} none" || true

cp "$CONFIG" "${OUTDIR}/config.conf.used"

# The campaign runs under sudo, so everything above was created as root. Hand
# the results back to the invoking user, otherwise analyze_plot.py cannot write
# summary.csv or figures/ into the run directory and dies with EACCES.
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  chown -R "${SUDO_UID}:${SUDO_GID}" "$OUTDIR" 2>/dev/null || \
    warn "could not chown $OUTDIR back to the invoking user"
fi

log "campaign complete: $OUTDIR"
echo "$OUTDIR"
