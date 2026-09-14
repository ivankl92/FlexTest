#!/usr/bin/env bash
# fix_iperf3.sh - replace a crash-prone distro iperf3 with a current upstream build.
#
# WHY THIS EXISTS
# ---------------
# iperf 3.16 was the release that made iperf3 multi-threaded: "Multiple test
# streams started with -P/--parallel will now be serviced by different threads."
# That rewrite shipped with several thread-lifetime bugs, and on this testbed the
# symptom is fatal and silent: the *server* dies with SIGSEGV as soon as a UDP
# test connects,
#
#     iperf3-bg.service: Main process exited, code=dumped, status=11/SEGV
#     iperf3-bg.service: Failed with result 'core-dump'.
#
# the client reports "unable to read from stream socket: Resource temporarily
# unavailable", and NO BACKGROUND TRAFFIC EVER REACHES THE LINK. The measurement
# itself keeps working, so every load point quietly returns the unloaded floor
# and the campaign looks like "load has no effect on latency". It is not a
# result; it is an absent stimulus.
#
# Upstream fixed the threading segfaults in 3.18 (#1801, #1760/PR#1761,
# #1750/PR#1752, PR#1755), another segfault in 3.19 (#1807), and in 3.21 a
# socket-close race plus erroneous zero-loss reporting in lossy UDP tests - the
# last of which matters here, because run_measurement.sh reads the achieved
# background rate back out of iperf3's JSON.
#
# Ubuntu 24.04 LTS ships 3.16. There is no fixed package for it, so this script
# builds a current release into /usr/local and points the systemd unit at it.
# The distro package is left installed and untouched; /usr/local/bin precedes
# /usr/bin in PATH, so the new binary simply wins.
#
# Run on BOTH nodes (the server crashes, but a client of the same vintage is no
# safer):
#     sudo ./fix_iperf3.sh
#
# Override the version with IPERF_VERSION=3.20 sudo -E ./fix_iperf3.sh
set -euo pipefail

IPERF_VERSION="${IPERF_VERSION:-3.21}"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

log()  { printf '\033[1;34m[iperf]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[FAIL ]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"

# ---------------------------------------------------------------------------
log "current iperf3: $(iperf3 -v 2>&1 | head -1 || echo 'not installed')  ($(command -v iperf3 || echo '-'))"

# ---------------------------------------------------------------------------
log "1/5 build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends build-essential ca-certificates curl >/dev/null

# ---------------------------------------------------------------------------
log "2/5 fetching iperf ${IPERF_VERSION}"
TARBALL="iperf-${IPERF_VERSION}.tar.gz"
# The GitHub release asset and the ESnet mirror carry the same file; the release
# asset is a pre-bootstrapped tarball, so no autoreconf/libtool is needed.
URLS=(
  "https://github.com/esnet/iperf/releases/download/${IPERF_VERSION}/${TARBALL}"
  "https://downloads.es.net/pub/iperf/${TARBALL}"
)
FETCHED=0
for U in "${URLS[@]}"; do
  log "  trying $U"
  if curl -fsSL --retry 2 -o "${BUILD_DIR}/${TARBALL}" "$U"; then FETCHED=1; break; fi
done
(( FETCHED == 1 )) || die "could not download ${TARBALL} - no route to the internet? \
Download it on another machine and re-run with the tarball in $PWD"

tar -xzf "${BUILD_DIR}/${TARBALL}" -C "$BUILD_DIR"
SRC="${BUILD_DIR}/iperf-${IPERF_VERSION}"
[[ -x "${SRC}/configure" ]] || die "unexpected tarball layout: no ${SRC}/configure"

# ---------------------------------------------------------------------------
log "3/5 building (this takes about a minute)"
(
  cd "$SRC"
  ./configure --prefix="$PREFIX" >"${BUILD_DIR}/configure.log" 2>&1 \
    || { tail -20 "${BUILD_DIR}/configure.log"; die "configure failed"; }
  make -j"$(nproc)" >"${BUILD_DIR}/make.log" 2>&1 \
    || { tail -20 "${BUILD_DIR}/make.log"; die "make failed"; }
  make install >"${BUILD_DIR}/install.log" 2>&1 \
    || { tail -20 "${BUILD_DIR}/install.log"; die "make install failed"; }
)
# libiperf.so.0 lands in $PREFIX/lib, which is not always on the default
# loader path until ldconfig has seen it.
echo "${PREFIX}/lib" >/etc/ld.so.conf.d/iperf3-local.conf
ldconfig

NEWBIN="${PREFIX}/bin/iperf3"
[[ -x "$NEWBIN" ]] || die "$NEWBIN was not installed"
hash -r
log "  installed: $("$NEWBIN" -v 2>&1 | head -1)"
log "  PATH now resolves iperf3 to: $(command -v iperf3)"

# ---------------------------------------------------------------------------
log "4/5 repointing the background-load service (if present)"
if [[ -f /etc/systemd/system/iperf3-bg.service ]]; then
  sed -i "s#^ExecStart=.*iperf3 #ExecStart=${NEWBIN} #" /etc/systemd/system/iperf3-bg.service
  systemctl daemon-reload
  # Restart=always plus a segfault on every connection leaves the unit with a
  # large restart counter and, eventually, in a failed state that a plain
  # restart will not clear.
  systemctl reset-failed iperf3-bg.service 2>/dev/null || true
  systemctl restart iperf3-bg.service
  sleep 1
  systemctl is-active --quiet iperf3-bg.service \
    && log "  iperf3-bg.service active on $(grep ^ExecStart= /etc/systemd/system/iperf3-bg.service | cut -d= -f2-)" \
    || warn "  iperf3-bg.service is not active - check: systemctl status iperf3-bg.service"
else
  log "  no iperf3-bg.service on this node (talker) - nothing to repoint"
fi

# ---------------------------------------------------------------------------
# The whole point of this script is that the old binary crashed on UDP, so
# prove the new one does not, before anyone spends 30 minutes on a campaign.
log "5/5 loopback UDP self-test"
"$NEWBIN" -s -p 5999 -1 >"${BUILD_DIR}/selftest_server.log" 2>&1 &
SRV=$!
sleep 1
if "$NEWBIN" -c 127.0.0.1 -p 5999 -u -b 200M -l 1400 -P 4 -t 3 \
     >"${BUILD_DIR}/selftest_client.log" 2>&1; then
  RATE=$(grep -E 'receiver' "${BUILD_DIR}/selftest_client.log" | tail -1 || true)
  log "  PASS  ${RATE:-completed}"
else
  cat "${BUILD_DIR}/selftest_client.log" >&2
  kill "$SRV" 2>/dev/null || true
  die "the new iperf3 still fails a 4-stream loopback UDP test - do not trust background load yet"
fi
wait "$SRV" 2>/dev/null || true

cat <<EOF

  iperf3 is now $("$NEWBIN" -v 2>&1 | head -1 | awk '{print $2}') at $NEWBIN.

  Run this script on the OTHER node too, then verify across the real link:
      # on the listener, the service is already running
      iperf3 -c ${PC2_BG_IP:-<listener-bg-ip>} -u -b 100M -l 1400 -P 4 -t 3
  Expect a "receiver" line near 100 Mbit/s, not
  "unable to read from stream socket: Resource temporarily unavailable".

EOF
