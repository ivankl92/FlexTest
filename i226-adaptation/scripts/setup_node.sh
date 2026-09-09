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
if ! ethtool -T "$STREAM_IF" | grep -q "HWTSTAMP_FILTER_ALL"; then
  warn "$STREAM_IF does not advertise HWTSTAMP_FILTER_ALL; RX hardware timestamps"
  warn "for non-PTP frames may be unavailable. The measurement tools will report this."
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
tx_timestamp_timeout    50
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
  cat >/etc/systemd/system/iperf3-bg.service <<'EOF'
[Unit]
Description=iperf3 server for TSN-FlexTest background load
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/iperf3 -s -p 5201
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now iperf3-bg.service
  log "  iperf3 server running"
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
