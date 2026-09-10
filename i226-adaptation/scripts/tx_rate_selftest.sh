#!/usr/bin/env bash
# tx_rate_selftest.sh - find the highest frame rate at which this NIC still
# returns a hardware TX timestamp for (nearly) every frame.
#
# WHY THIS EXISTS
# The I225/I226 has only a few TX timestamp registers. A frame holds one from
# transmission until the driver reads the value back, and if all registers are
# busy when the next frame is queued the driver SKIPS timestamping it. So there
# is a rate above which you stop getting a timestamp per frame — and where that
# rate lies depends on your kernel, driver version and interrupt coalescing.
# Nobody can tell you the number; it has to be measured.
#
# This needs ONE PC. No switch, no second host, no gPTP. Frames are sent to an
# unused unicast MAC and simply leave the port; only the TX side is exercised.
#
# Usage:
#   sudo ./tx_rate_selftest.sh enp1s0                 # default sweep
#   sudo ./tx_rate_selftest.sh enp1s0 "1000 5000 10000 20000"
#   sudo ./tx_rate_selftest.sh enp1s0 "10000" 512 10  # rate, frame bytes, seconds
set -euo pipefail

# Force C numeric formatting. Under a locale such as de_DE, awk's printf emits
# "100,0" and every downstream numeric comparison silently degrades to a string
# comparison ("100,0" < "90" because "1" < "9"), which reports passing rates as
# failures. Do not remove.
export LC_ALL=C

IF="${1:?usage: tx_rate_selftest.sh <iface> [rates] [frame_bytes] [seconds]}"
RATES="${2:-1000 5000 10000 20000 50000}"
SIZE="${3:-512}"
SECS="${4:-5}"
TX="${TSN_TX_BIN:-/opt/tsn-flextest/tsn_tx}"
DST="02:00:00:00:00:ff"     # locally administered, unused
PASS=90                     # yield % below which a rate is considered failed

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
[[ -x "$TX" ]] || { echo "tsn_tx not found at $TX (set TSN_TX_BIN)" >&2; exit 1; }
[[ -d "/sys/class/net/$IF" ]] || { echo "no such interface: $IF" >&2; exit 1; }

echo "Interface : $IF  ($(basename "$(readlink -f "/sys/class/net/$IF/device/driver")"), \
$(cat "/sys/class/net/$IF/speed" 2>/dev/null || echo '?') Mb/s, \
link $(cat "/sys/class/net/$IF/operstate"))"
echo "Frame     : ${SIZE} B      Duration: ${SECS} s per rate"
echo "Kernel    : $(uname -r)"
echo

# Counter the igc driver bumps when it cannot timestamp a frame. Not every
# driver version exposes it; absence is not an error.
skipped() { ethtool -S "$IF" 2>/dev/null | awk '/tx_hwtstamp_skipped/{print $2; found=1}
                                                END{if(!found) print "-"}'; }

# portState of the local ptp4l, or "-" when ptp4l is not running here.
ptp_state() {
  [[ -r /etc/linuxptp/gPTP.cfg ]] || { echo "-"; return; }
  pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET PORT_DATA_SET' 2>/dev/null |
    awk '/portState/{print $2; found=1} END{if(!found) print "-"}' | head -1
}

printf '%8s  %10s  %10s  %8s  %9s  %8s  %8s  %s\n' \
       RATE SENT TIMESTAMPS YIELD SKIPPED "GAP SD" "GAP MAX" VERDICT
printf '%8s  %10s  %10s  %8s  %9s  %8s  %8s  %s\n' \
       "-------" "---------" "----------" "-------" "--------" "--------" "--------" "-------"
echo "                                                          (us)      (us)"

BEST=0
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

for RATE in $RATES; do
  N=$(( RATE * SECS ))
  BEFORE=$(skipped)

  "$TX" -i "$IF" -d "$DST" -n "$N" -r "$RATE" -s "$SIZE" \
        -o "$TMP/tx_${RATE}.csv" >/dev/null 2>"$TMP/log_${RATE}" || true

  AFTER=$(skipped)
  SENT=$(grep -oP 'sent=\K[0-9]+'              "$TMP/log_${RATE}" | head -1 || echo 0)
  GOT=$(grep -oP 'hw_tx_timestamps=\K[0-9]+'   "$TMP/log_${RATE}" | head -1 || echo 0)

  if [[ "$BEFORE" == "-" || "$AFTER" == "-" ]]; then
    DELTA="n/a"
  else
    DELTA=$(( AFTER - BEFORE ))
  fi

  read -r YIELD OK < <(awk -v g="${GOT:-0}" -v s="${SENT:-0}" -v p="$PASS" \
       'BEGIN{y = (s>0) ? 100*g/s : 0; printf "%.1f %d\n", y, (y>=p)?1:0}')

  if [[ "$OK" == "1" ]]; then VERDICT="ok"; BEST=$RATE
  else VERDICT="DEGRADED"; fi
  PERIOD_US=$(awk -v r="$RATE" 'BEGIN{printf "%.3f", 1e6/r}')

  # 100 % yield for THIS tool does not mean the rate is safe. The NIC has a
  # small number of TX-timestamp registers, and ptp4l on the same interface
  # needs one for every Pdelay message. If we take them all, ptp4l logs
  # "timed out while polling for tx timestamp" and the port goes FAULTY --
  # which the yield column cannot see. Check the victim directly.
  if [[ "$(ptp_state)" == "FAULTY" ]]; then
    VERDICT="$VERDICT / ptp4l FAULTY"
    PTP_VICTIM=${PTP_VICTIM:-$RATE}
  fi

  # A software-timestamp fallback would invalidate the whole measurement.
  if grep -q ',sw$' "$TMP/tx_${RATE}.csv" 2>/dev/null; then
    VERDICT="SOFTWARE TIMESTAMPS"
  fi

  # On-wire pacing, straight from the hardware TX timestamps: how far each gap
  # actually landed from the nominal period. This is the number that says whether
  # the stream is genuinely isochronous, which yield alone does not tell you.
  JIT=$(awk -F, -v rate="$RATE" '
      NR>1 && $2 ~ /^[0-9]+$/ {
        if (prev) { d = $2 - prev; n++; sum += d; sq += d*d;
                    e = d - (1e9/rate); if (e<0) e=-e; if (e>max) max=e }
        prev = $2
      }
      END{ if (n<2) { print "n/a n/a"; exit }
           mean = sum/n; sd = sqrt(sq/n - mean*mean)
           printf "%.1f %.1f\n", sd/1000, max/1000 }' \
      "$TMP/tx_${RATE}.csv" 2>/dev/null || echo "n/a n/a")
  JIT_SD=${JIT%% *}; JIT_MAX=${JIT##* }

  # Worst-case gap error as a multiple of the nominal period. Yield says the
  # NIC timestamped the frames; this says whether they left on schedule. They
  # are independent, and only the pair together describes the stimulus.
  if [[ "$JIT_MAX" != "n/a" ]]; then
    GAPX=$(LC_ALL=C awk -v m="$JIT_MAX" -v p="$PERIOD_US" 'BEGIN{printf "%.0f", m/p}')
    if [[ "$VERDICT" == "ok" ]] && (( GAPX >= 2 )); then
      VERDICT="ok / gap x${GAPX}"
      [[ -z "${PACING_NOTE:-}" ]] && PACING_NOTE="$RATE"
    fi
  fi

  printf '%8s  %10s  %10s  %7s%%  %9s  %8s  %8s  %s\n' \
         "$RATE" "$SENT" "$GOT" "$YIELD" "$DELTA" "$JIT_SD" "$JIT_MAX" "$VERDICT"
done

echo
if [[ "$BEST" -gt 0 ]]; then
  MIN_US=$(awk -v r="$BEST" 'BEGIN{printf "%.0f", 1e6/r}')
  echo "TIMESTAMPING ceiling: ${BEST} fps still holds >=${PASS}% hardware TX"
  echo "timestamps (one frame every ${MIN_US} us)."
  echo
  echo "This is a CEILING, not a recommended STREAM_RATE. It says the NIC can"
  echo "hand back a timestamp for every frame at that rate; it says nothing"
  echo "about whether the frames left on schedule. Read the GAP columns before"
  echo "choosing a rate."
  if [[ -n "${PTP_VICTIM:-}" ]]; then
    echo
    echo "PTP WARNING: ptp4l on ${IF} went FAULTY at ${PTP_VICTIM} fps and above."
    echo "This tool took the NIC's TX-timestamp registers and starved it. Yield"
    echo "stayed high because the victim is ptp4l, not us -- and without gPTP"
    echo "there is no common time base, so no latency can be measured at all."
    echo "Do NOT use a rate at or above ${PTP_VICTIM} fps with timestamping on"
    echo "every frame. Either lower the rate, or keep the rate and thin the"
    echo "timestamp requests with tsn_tx -N / STREAM_TS_EVERY in config.conf."
    echo "Recover with: systemctl reset-failed ptp4l@${IF}; systemctl restart ptp4l@${IF}"
  fi
  if [[ -n "${PACING_NOTE:-}" ]]; then
    echo
    echo "PACING WARNING: from ${PACING_NOTE} fps upward the worst-case on-wire"
    echo "gap is two or more nominal periods (see the 'gap xN' verdicts). The"
    echo "stream is bursty, not isochronous: clock_nanosleep cannot hold the"
    echo "schedule when the period approaches the scheduler's own wakeup jitter."
    echo "Every frame is still timestamped correctly, so latency per frame is"
    echo "valid -- but the OFFERED LOAD PATTERN is not a clean CBR stream, and"
    echo "queueing delay depends on the arrival pattern. Say so in any writeup."
    echo "The fix is hardware pacing: SO_TXTIME + etf (REPORT.md open item #10)."
  fi
else
  echo "No rate reached ${PASS}% yield. Check 'ethtool -T ${IF}' for"
  echo "hardware-transmit support, and that this is really an igc interface."
fi
echo
echo "GAP SD / GAP MAX: standard deviation and worst-case deviation of the actual"
echo "on-wire inter-frame gap from the nominal period, measured by the NIC itself."
echo "Large values mean the stream is not isochronous even though every frame was"
echo "timestamped - software pacing, not a timestamping problem. The fix for that"
echo "is SO_TXTIME + the etf qdisc (see REPORT.md, open item #10)."
echo
echo "Note: this measures the TIMESTAMPING limit, not the sending limit. The NIC"
echo "can transmit far faster than it can hand back timestamps. If you need a"
echo "high frame rate AND timestamps, send the stream on one socket and timestamp"
echo "only every Nth frame on a second socket (see REPORT.md, open item #9)."
