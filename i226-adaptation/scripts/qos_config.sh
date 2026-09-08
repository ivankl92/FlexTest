#!/usr/bin/env bash
# qos_config.sh - apply or clear the host-side egress QoS configuration.
#
#   qos_config.sh <iface> none      remove all root qdiscs (No-QoS baseline)
#   qos_config.sh <iface> dot1p     mqprio: socket priority 3 -> hardware queue 0
#
# Note on scope: the measured stream is ~1000 pps on a 1 Gbit/s port, so the
# host's own egress queues are never the bottleneck. The differentiation that
# actually matters happens in the KSwitch on the shared inter-switch link, and
# is driven by the PCP that tsn_tx writes into the 802.1Q tag. This mqprio setup
# mirrors the original testbed's begin.sh so the host side is faithful too.
set -euo pipefail

IF="${1:?usage: qos_config.sh <iface> none|dot1p}"
MODE="${2:?usage: qos_config.sh <iface> none|dot1p}"

tc qdisc del dev "$IF" root 2>/dev/null || true

case "$MODE" in
  none)
    echo "[qos] $IF: root qdisc cleared (No-QoS baseline)"
    ;;
  dot1p)
    # I226 exposes 4 combined queues.
    ethtool -L "$IF" combined 4 2>/dev/null || true
    # map[priority] = traffic class. Socket priority 3 (our measurement stream)
    # -> tc 0 -> hw queue 0. Everything else -> tc 1 -> hw queue 1.
    tc qdisc add dev "$IF" parent root handle 6666 mqprio \
       num_tc 2 \
       map 1 1 1 0 1 1 1 1 1 1 1 1 1 1 1 1 \
       queues 1@0 1@1 \
       hw 0
    echo "[qos] $IF: mqprio applied (socket prio 3 -> queue 0)"
    tc qdisc show dev "$IF" | sed 's/^/      /'
    ;;
  *)
    echo "unknown mode '$MODE'" >&2
    exit 2
    ;;
esac
