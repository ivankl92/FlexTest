#!/usr/bin/env bash
# setup_node.sh - prepare one Ubuntu node of the TSN-FlexTest I226 testbed.
#
# Idempotent: safe to re-run. Installs dependencies, verifies the NICs and their
# timestamping capabilities, configures gPTP (802.1AS) via linuxptp, and builds
# the measurement tools.
#
# Usage (as root, on each PC):
#   ./setup_node.sh --stream-if enp1s0 --bg-if enp2s0 \
#                   --stream-ip 192.168.1.61/24 --bg-ip 192.168.1.62/24 \
#                   [--role talker|listener] [--no-ip-config]
set -euo pipefail

STREAM_IF=""
BG_IF=""
STREAM_IP=""
BG_IP=""
PEER_BG_IP=""
ROLE="both"
CONFIG_IP=1
INSTALL_DIR="/opt/tsn-flextest"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[FAIL ]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stream-if) STREAM_IF="$2"; shift 2;;
    --bg-if)     BG_IF="$2";     shift 2;;
    --stream-ip) STREAM_IP="$2"; shift 2;;
    --bg-ip)     BG_IP="$2";     shift 2;;
    --peer-bg-ip) PEER_BG_IP="$2"; shift 2;;
    --role)      ROLE="$2";      shift 2;;
    --no-ip-config) CONFIG_IP=0; shift;;
    *) die "unknown argument: $1";;
  esac
done

[[ -n "$STREAM_IF" && -n "$BG_IF" ]] || die "--stream-if and --bg-if are required"
[[ $EUID -eq 0 ]] || die "must run as root"

# ---------------------------------------------------------------------------
log "1/7 installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential linuxptp ethtool iperf3 tcpdump iproute2 \
  python3 python3-pip python3-numpy python3-pandas python3-matplotlib \
  git ca-certificates chrony >/dev/null
log "packages installed: $(ptp4l -v 2>&1 | head -1)"

# --- iperf3 version gate ----------------------------------------------------
# iperf 3.16 made iperf3 multi-threaded and shipped thread-lifetime bugs with
# it. On this hardware the server takes SIGSEGV the moment a UDP test connects
# (journal: "Main process exited, code=dumped, status=11/SEGV"), the client
# reports "unable to read from stream socket: Resource temporarily
# unavailable", and no background traffic ever reaches the link. Nothing else
# fails: the stream is still sent, still timestamped, still analysed - so every
# load point silently returns the unloaded floor and the campaign reads as
# "background load has no effect on latency". A whole afternoon of measurement
# can be lost to it, which is why this is a hard stop and not a warning.
#
# Upstream fixed the threading segfaults in 3.18 (#1801, #1760, #1750),
# another in 3.19 (#1807), and in 3.21 a socket-close race plus erroneous
# zero-loss reporting on lossy UDP tests - which matters because
# run_measurement.sh reads the achieved background rate out of iperf3's JSON.
# Ubuntu 24.04 LTS ships 3.16 and there is no fixed package for it.
IPERF3_BIN=$(command -v iperf3 || true)
[[ -x /usr/local/bin/iperf3 ]] && IPERF3_BIN=/usr/local/bin/iperf3
[[ -n "$IPERF3_BIN" ]] || die "iperf3 not installed"
IPERF3_VER=$("$IPERF3_BIN" -v 2>&1 | head -1 | awk '{print $2}')
if printf '%s\n%s\n' "3.18" "$IPERF3_VER" | sort -V -C; then
  log "  iperf3 $IPERF3_VER at $IPERF3_BIN"
  if printf '%s\n%s\n' "3.21" "$IPERF3_VER" | sort -V -C; then :; else
    warn "  iperf3 $IPERF3_VER is usable; 3.21+ additionally fixes zero-loss"
    warn "  misreporting on lossy UDP tests, which this campaign reads back"
  fi
elif [[ "${ALLOW_OLD_IPERF3:-0}" == "1" ]]; then
  warn "  iperf3 $IPERF3_VER is known to crash on UDP - continuing because"
  warn "  ALLOW_OLD_IPERF3=1. Background load will probably be absent."
else
  warn "iperf3 $IPERF3_VER ($IPERF3_BIN) is a known-broken release: the server"
  warn "segfaults on UDP tests, so background load never reaches the link and"
  warn "every load point returns the unloaded floor without any error."
  die "fix it first:  sudo $SRC_DIR/scripts/fix_iperf3.sh   (run on both nodes)
     then re-run this script. To proceed anyway, with no background load,
     prefix this command with ALLOW_OLD_IPERF3=1 and use 'sudo -E'."
fi

# chrony would fight phc2sys for CLOCK_REALTIME; gPTP must own the clock.
if systemctl is-enabled --quiet chrony 2>/dev/null; then
  log "disabling chrony (it would fight phc2sys for the system clock)"
  systemctl disable --now chrony >/dev/null 2>&1 || true
fi
systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
log "2/7 verifying interfaces"
for IF in "$STREAM_IF" "$BG_IF"; do
  [[ -d "/sys/class/net/$IF" ]] || die "interface $IF does not exist"
  DRV=$(basename "$(readlink -f "/sys/class/net/$IF/device/driver")")
  ip link set "$IF" up
  sleep 1
  STATE=$(cat "/sys/class/net/$IF/operstate")
  SPEED=$(cat "/sys/class/net/$IF/speed" 2>/dev/null || echo "?")
  log "  $IF driver=$DRV state=$STATE speed=${SPEED}Mb/s"
  [[ "$DRV" == "igc" ]] || warn "  $IF is not on the igc driver (found '$DRV')"
  [[ "$STATE" == "up" ]] || warn "  $IF link is '$STATE' - check the cable to the switch"
done

log "  hardware timestamping capabilities of $STREAM_IF:"
ethtool -T "$STREAM_IF" | sed 's/^/    /'
if ! ethtool -T "$STREAM_IF" | grep -q "hardware-transmit"; then
  die "$STREAM_IF does not report hardware-transmit timestamping - measurement impossible"
fi
# The RX filter list must offer "all" (HWTSTAMP_FILTER_ALL) — the PTP-only
# filters would not timestamp our 0x88B5 frames.
#
# ethtool prints this section in two different spellings depending on version:
#
#     Hardware Receive Filter Modes:        Hardware Receive Filter Modes:
#         none                                  none        (HWTSTAMP_FILTER_NONE)
#         all                                   all         (HWTSTAMP_FILTER_ALL)
#
# Matching only the symbolic name misses the short form and warns on hardware
# that is in fact fine. Read the filter section and look for a bare "all".
if ! ethtool -T "$STREAM_IF" |
     sed -n '/Hardware Receive Filter Modes:/,$p' |
     grep -qE '^[[:space:]]*(all|HWTSTAMP_FILTER_ALL)\b'; then
  warn "$STREAM_IF does not advertise the 'all' RX filter (HWTSTAMP_FILTER_ALL);"
  warn "RX hardware timestamps for non-PTP frames may be unavailable."
  warn "The measurement tools will report ts_src=sw if that turns out to be so."
fi

# ---------------------------------------------------------------------------
log "3/7 IP configuration"
if [[ $CONFIG_IP -eq 1 ]]; then
  [[ -n "$STREAM_IP" ]] && { ip addr show dev "$STREAM_IF" | grep -q "${STREAM_IP%%/*}" \
     || ip addr add "$STREAM_IP" dev "$STREAM_IF"; }
  [[ -n "$BG_IP" ]] && { ip addr show dev "$BG_IF" | grep -q "${BG_IP%%/*}" \
     || ip addr add "$BG_IP" dev "$BG_IF"; }
fi
ip -br addr show dev "$STREAM_IF" | sed 's/^/    /'
ip -br addr show dev "$BG_IF"     | sed 's/^/    /'

# Both port pairs sit in the same 192.168.1.0/24 subnet, so Linux would answer
# ARP for either address out of either NIC and the background load could end up
# on the measurement port. Pin each address to its own interface.
log "  applying strict ARP/rp_filter settings so the two port pairs stay separated"
for IF in "$STREAM_IF" "$BG_IF"; do
  sysctl -qw "net.ipv4.conf.$IF.arp_filter=1"
  sysctl -qw "net.ipv4.conf.$IF.arp_announce=2"
  sysctl -qw "net.ipv4.conf.$IF.arp_ignore=1"
  sysctl -qw "net.ipv4.conf.$IF.rp_filter=0"
done
sysctl -qw net.ipv4.conf.all.arp_filter=1
sysctl -qw net.ipv4.conf.all.arp_announce=2
sysctl -qw net.ipv4.conf.all.arp_ignore=1

# --- source-address selection, which the sysctls above do NOT fix -----------
# The strict-ARP settings govern who answers ARP. They say nothing about which
# SOURCE ADDRESS the kernel puts on a locally-originated reply, and that is a
# separate decision made by a route lookup. With two addresses of the same /24
# on two interfaces there are two equal-cost routes for that prefix, the kernel
# picks the lower ifindex - the STREAM interface - and every reply leaves with
# the stream address as its source.
#
# Measured symptom: iperf3's UDP handshake. The client sends UDP_CONNECT_MSG to
# 192.168.1.72:5201 and waits on a socket CONNECTED to .72. The server replies
# from .71, the kernel discards a datagram from the wrong peer before iperf3
# sees it, the 30 s SO_RCVTIMEO fires, and the client dies with "unable to read
# from stream socket: Resource temporarily unavailable". TCP is unaffected,
# because an accepted socket's addresses are already fixed by the handshake -
# so the control connection works and only the data path fails, which is what
# makes this look like an iperf3 bug rather than a routing one.
#
#   tcpdump proof:
#     enp2s0 Out IP 192.168.1.62.44668 > 192.168.1.72.5201: UDP, length 4
#     enp2s0 In  IP 192.168.1.71.5201 > 192.168.1.62.44668: UDP, length 4
#                    ^^^^^^^^^^^^ should be .72
#
# A /32 host route to the peer's background address, carrying an explicit src,
# beats the /24 on longest-prefix match and settles both the outgoing interface
# and the source address.
if [[ -n "$PEER_BG_IP" && -n "$BG_IP" ]]; then
  ip route replace "${PEER_BG_IP%%/*}/32" dev "$BG_IF" src "${BG_IP%%/*}"
  log "  pinned route to peer ${PEER_BG_IP%%/*} via $BG_IF src ${BG_IP%%/*}"
  log "    $(ip route get "${PEER_BG_IP%%/*}" | head -1)"
elif [[ -n "$STREAM_IP" && -n "$BG_IP" ]] && \
     python3 - "$STREAM_IP" "$BG_IP" <<'PY'
import ipaddress, sys
a = ipaddress.ip_interface(sys.argv[1]).network
b = ipaddress.ip_interface(sys.argv[2]).network
sys.exit(0 if a == b else 1)
PY
then
  warn "  $STREAM_IF and $BG_IF are both in the same subnet."
  warn "  Source-address selection for that prefix is then decided by interface"
  warn "  index, not by which port you meant - replies can leave with the wrong"
  warn "  source address and UDP peers will silently drop them."
  warn "  Pass --peer-bg-ip <other node's background IP> so this script can pin"
  warn "  a host route, or give the background pair its own subnet."
fi

# ---------------------------------------------------------------------------
log "4/7 writing gPTP (IEEE 802.1AS) configuration"
mkdir -p /etc/linuxptp
cat >/etc/linuxptp/gPTP.cfg <<'EOF'
#
# 802.1AS / gPTP profile for TSN-FlexTest on Intel I226 (igc).
#
# The PCs must never become grandmaster. In an 802.1AS profile that is
# expressed by 'gmCapable 0' ALONE. Do NOT also set 'clientOnly' (or
# 'slaveOnly'): those are the IEEE 1588 default-profile mechanism, and ptp4l
# rejects the combination outright at startup with
#     Cannot mix 1588 clientOnly with 802.1AS !gmCapable
#     failed to create a clock
# which systemd then turns into a restart loop. 'gmCapable 0' already forces
# priority1 and clockClass to 255, so the node loses every BMCA comparison.
#
# The grandmaster does not have to be a KSwitch. The switches may simply relay
# time from a GM elsewhere in the network; all this profile requires is that
# the PC is a client of whatever GM the domain has.
#
[global]
gmCapable               0
priority1               255
priority2               255
logAnnounceInterval     0
logSyncInterval         -3
logMinPdelayReqInterval 0
announceReceiptTimeout  3
syncReceiptTimeout      3
neighborPropDelayThresh 800
min_neighbor_prop_delay -20000000
assume_two_step         1
path_trace_enabled      1
follow_up_info          1
transportSpecific       0x1
ptp_dst_mac             01:80:C2:00:00:0E
network_transport       L2
delay_mechanism         P2P
time_stamping           hardware
# How long ptp4l waits for the NIC to return the TX timestamp of its own PTP
# frame. linuxptp's default is 1 ms; 50 ms was already generous, and it was
# still not enough here. At 105% background load on the second port pair, the
# TX-timestamp path stalls long enough that ptp4l times out, logs "timed out
# while polling for tx timestamp" and puts the port in portState FAULTY — from
# which it did not recover, so every later measurement point was skipped.
# 200 ms buys headroom under oversubscription. It costs nothing when the path
# is healthy: the timeout is an upper bound, not a delay.
tx_timestamp_timeout    200
step_threshold          0.00002
summary_interval        4
EOF

# Fail fast on a config ptp4l will not accept, instead of handing systemd a
# unit that crash-loops. ptp4l validates the file and exits before touching the
# interface when given -h.
if ! ptp4l -f /etc/linuxptp/gPTP.cfg -i "$STREAM_IF" -h >/dev/null 2>&1; then
  log "  ERROR: ptp4l rejects /etc/linuxptp/gPTP.cfg. Its own message:"
  ptp4l -f /etc/linuxptp/gPTP.cfg -i "$STREAM_IF" -h 2>&1 | sed 's/^/    /' | tail -5
  exit 5
fi

cat >/etc/systemd/system/ptp4l@.service <<EOF
[Unit]
Description=linuxptp gPTP client on %i (TSN-FlexTest)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/gPTP.cfg -i %i -m
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

# phc2sys keeps CLOCK_REALTIME close to the (gPTP-disciplined) PHC. The latency
# measurement itself only ever subtracts two PHC timestamps, so this is for
# readable logs and correct pacing, not for measurement accuracy.
cat >/etc/systemd/system/phc2sys@.service <<EOF
[Unit]
Description=linuxptp phc2sys, PHC of %i -> system clock (TSN-FlexTest)
After=ptp4l@%i.service
Requires=ptp4l@%i.service

[Service]
Type=simple
ExecStart=/usr/sbin/phc2sys -s %i -c CLOCK_REALTIME -w -m -O 0
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "ptp4l@${STREAM_IF}.service"
sleep 2
systemctl enable --now "phc2sys@${STREAM_IF}.service"
log "  ptp4l and phc2sys started on $STREAM_IF"

# ---------------------------------------------------------------------------
log "5/7 building measurement tools"
mkdir -p "$INSTALL_DIR"
gcc -O2 -Wall -Wextra -o "$INSTALL_DIR/tsn_tx" "$SRC_DIR/tools/tsn_tx.c"
gcc -O2 -Wall -Wextra -o "$INSTALL_DIR/tsn_rx" "$SRC_DIR/tools/tsn_rx.c"
cp "$SRC_DIR/scripts/qos_config.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/qos_config.sh"
log "  installed $INSTALL_DIR/{tsn_tx,tsn_rx,qos_config.sh}"

# ---------------------------------------------------------------------------
log "6/7 background-traffic service (listener side)"
if [[ "$ROLE" == "listener" || "$ROLE" == "both" ]]; then
  # $IPERF3_BIN, not a hard-coded /usr/bin/iperf3: fix_iperf3.sh installs a
  # current upstream build into /usr/local/bin, and the unit must follow it.
  cat >/etc/systemd/system/iperf3-bg.service <<EOF
[Unit]
Description=iperf3 server for TSN-FlexTest background load
After=network-online.target

[Service]
Type=simple
ExecStart=${IPERF3_BIN} -s -p 5201
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  # Restart=always in front of a binary that segfaults on every connection
  # leaves the unit with a large restart counter and eventually in a failed
  # state that enable/start alone will not clear.
  systemctl reset-failed iperf3-bg.service 2>/dev/null || true
  systemctl enable iperf3-bg.service >/dev/null 2>&1 || true
  systemctl restart iperf3-bg.service
  sleep 1
  systemctl is-active --quiet iperf3-bg.service \
    && log "  iperf3 server running: ${IPERF3_BIN} $("$IPERF3_BIN" -v 2>&1 | head -1 | awk '{print $2}')" \
    || warn "  iperf3-bg.service is not active - systemctl status iperf3-bg.service"
fi

# ---------------------------------------------------------------------------
log "7/7 waiting up to 90 s for gPTP lock on $STREAM_IF"
LOCKED=0
for i in $(seq 1 45); do
  STATE=$(pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg "GET PORT_DATA_SET" 2>/dev/null \
          | grep -E 'portState' | awk '{print $2}' | head -1 || true)
  OFFSET=$(pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg "GET CURRENT_DATA_SET" 2>/dev/null \
          | grep -E 'offsetFromMaster' | awk '{print $2}' | head -1 || true)
  if [[ "$STATE" == "SLAVE" || "$STATE" == "CLIENT" ]]; then
    log "  portState=$STATE offsetFromMaster=${OFFSET:-?} ns  (after ${i}0% of budget)"
    # require the offset to be sane (< 1 us) before declaring success
    if [[ -n "${OFFSET:-}" ]]; then
      ABS=${OFFSET#-}; ABS=${ABS%%.*}
      if [[ "$ABS" =~ ^[0-9]+$ ]] && (( ABS < 1000 )); then LOCKED=1; break; fi
    fi
  fi
  sleep 2
done

if [[ $LOCKED -eq 1 ]]; then
  log "gPTP LOCKED. Node setup complete."
else
  warn "gPTP did not reach a stable sub-microsecond lock within 90 s."
  warn "Current state:"
  pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg "GET PORT_DATA_SET" 2>&1 | sed 's/^/    /' || true
  journalctl -u "ptp4l@${STREAM_IF}.service" -n 20 --no-pager | sed 's/^/    /' || true
  warn "Check that gPTP is enabled on the KSwitch port this NIC is plugged into,"
  warn "and that the switch is acting as grandmaster."
  exit 4
fi
