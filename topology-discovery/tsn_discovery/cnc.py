"""Normalise discovery output into a CNC input document.

IEEE 802.1Qcc's fully centralized model splits configuration into a CUC
(which knows the talkers and listeners and their stream requirements) and a
CNC (which knows the network and computes the schedule). This module
produces the *network* half of the CNC's input: the bridge inventory, the
link graph, and -- critically -- the per-port capability envelope that
bounds any schedule the CNC may compute.

It also produces an explicit **gap list**: what a CNC built on this NETCONF
surface cannot do. That list is as much a deliverable as the capabilities.
A scheduler that assumes it can read the time base, install per-stream
filters, or configure a credit-based shaper on this hardware will produce
configurations that cannot be installed.

The output is intentionally flat and self-describing. It is an interchange
document, not an internal structure: a future CNC should be able to consume
it without importing this package.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

SCHEMA = "tsn-testbed/cnc-input/v1"


def _min_or_none(values: List[Optional[int]]) -> Optional[int]:
    vals = [v for v in values if isinstance(v, int)]
    return min(vals) if vals else None


def _port_role(node: str, port: str, links: List[dict],
               switch_names: set) -> tuple:
    """Classify a port and find its neighbour."""
    for link in links:
        for near, far in (("a", "b"), ("b", "a")):
            if link[near]["node"] == node and str(link[near]["port"]) == str(port):
                peer = link[far]["node"]
                role = "inter-switch" if peer in switch_names else "edge"
                return role, {
                    "node": peer,
                    "port": link[far]["port"],
                    "method": link["method"],
                    "confirmed": link.get("confirmed_bidirectional", False),
                }
    return "unused", None


def build(records: Dict[str, dict], topo: dict, inventory,
          run_meta: dict) -> dict:
    switch_names = {n["id"] for n in topo["nodes"] if n["role"] == "switch"}
    links = topo["links"]

    bridges: List[dict] = []
    all_cycle_max: List[Optional[int]] = []
    all_gcl_max: List[Optional[int]] = []
    all_interval_max: List[Optional[int]] = []

    for name, rec in sorted(records.items()):
        if not rec.get("reachable"):
            bridges.append({
                "id": name,
                "reachable": False,
                "transport": rec.get("transport", "netconf"),
                "transport_detail": rec.get("transport_detail"),
                "management": {"address": rec.get("host"),
                               "protocol": "netconf", "port": 830},
                "error": rec.get("errors"),
            })
            continue

        feats = rec.get("features", {})
        summary = rec.get("summary", {})
        bridge0 = (rec.get("bridges") or [{}])[0]

        ports: List[dict] = []
        for iface in rec.get("interfaces", []):
            if not iface.get("is_bridge_port"):
                continue
            bp = iface.get("bridge_port") or {}
            qbv = iface.get("qbv", {})
            qbu = iface.get("qbu", {})
            role, neighbour = _port_role(name, iface["name"], links, switch_names)

            qbv_cap = qbv.get("capability", {}) if qbv.get("present") else {}
            qbv_cfg = qbv.get("configuration", {}) if qbv.get("present") else {}
            qbu_cap = qbu.get("capability", {}) if qbu.get("present") else {}
            qbu_cfg = qbu.get("configuration", {}) if qbu.get("present") else {}

            all_cycle_max.append(qbv_cap.get("supported_cycle_time_max_ns"))
            all_gcl_max.append(qbv_cap.get("supported_gcl_entries_max"))
            all_interval_max.append(qbv_cap.get("supported_interval_max_ns"))

            ports.append({
                "name": iface["name"],
                "port_number": bp.get("port_number"),
                "if_index": iface.get("if_index"),
                "enabled": iface.get("enabled"),
                "oper_status": iface.get("oper_status"),
                "speed_mbps": iface.get("speed_mbps"),
                "speed_source": iface.get("speed_source"),
                "pvid": bp.get("pvid"),
                "traffic_classes": 8,
                "role": role,
                "neighbour": neighbour,
                "qbv": {
                    "supported": bool(qbv.get("present")),
                    "max_gcl_entries": qbv_cap.get("supported_gcl_entries_max"),
                    "max_cycle_time_ns": qbv_cap.get("supported_cycle_time_max_ns"),
                    "max_interval_ns": qbv_cap.get("supported_interval_max_ns"),
                    "gate_enabled": qbv_cfg.get("gate_enabled"),
                    "current_cycle_time_ns": qbv_cfg.get("admin_cycle_time_ns"),
                    "current_base_time_ns": qbv_cfg.get("admin_base_time_ns"),
                    "current_gcl_entries": len(
                        qbv_cfg.get("admin_control_list", []) or []),
                    "current_gate_states": (qbv_cfg.get("admin_gate_states") or {}
                                            ).get("value"),
                },
                "qbu": {
                    "supported": bool(qbu.get("present")),
                    "preemption_active": qbu_cfg.get("preemption_active"),
                    "preemptable_priorities": qbu_cfg.get("preemptable_priorities"),
                    "express_priorities": qbu_cfg.get("express_priorities"),
                    "hold_advance_ns": qbu_cap.get("hold_advance_ns"),
                    "release_advance_ns": qbu_cap.get("release_advance_ns"),
                },
                "qos": {
                    "nodes_exposed": iface.get("qos", {}).get("nodes_present", []),
                    "traffic_class_map": iface.get("qos", {}).get("traffic_class_map"),
                    "priority_regeneration": iface.get("qos", {}).get(
                        "priority_regeneration"),
                },
            })

        bridges.append({
            "id": name,
            "reachable": True,
            "transport": rec.get("transport", "netconf"),
            "transport_detail": rec.get("transport_detail"),
            "hostname": (rec.get("system") or {}).get("hostname"),
            "firmware": (rec.get("system") or {}).get("firmware"),
            "management": {"address": rec.get("host"),
                           "protocol": "netconf", "port": 830},
            "bridge": {
                "name": bridge0.get("name"),
                "address": bridge0.get("address"),
                "type": bridge0.get("bridge_type"),
                "port_count": bridge0.get("ports"),
                "up_time_s": bridge0.get("up_time_s"),
            },
            "netconf": rec.get("netconf"),
            "yang_modules": sorted(rec.get("yang_modules", {}).keys()),
            "capabilities": {
                key: {
                    "supported": feats.get(key, {}).get("supported", False),
                    "standard": feats.get(key, {}).get("standard"),
                    "modules": [m["module"] for m in
                                feats.get(key, {}).get("modules", [])],
                }
                for key in ("bridge", "qbv", "qbu", "qci", "qav", "qcc",
                            "lldp", "ptp")
            },
            "ptp": rec.get("ptp"),
            "vlans": summary.get("vlan_details", []),
            "ports": ports,
            "warnings": rec.get("warnings", []),
        })

    reachable = [b for b in bridges if b.get("reachable")]

    def _all_support(key: str) -> bool:
        return bool(reachable) and all(
            b["capabilities"][key]["supported"] for b in reachable)

    def _any_support(key: str) -> bool:
        return any(b["capabilities"][key]["supported"] for b in reachable)

    gaps: List[dict] = []

    # PTP has three distinct states now and they call for different
    # responses, so they are not collapsed into one gap.
    ptp_via_cli = sorted(b["id"] for b in reachable
                         if (b.get("ptp") or {}).get("status")
                         == "available-via-cli")
    ptp_absent = sorted(b["id"] for b in reachable
                        if (b.get("ptp") or {}).get("status")
                        not in ("available", "available-via-cli"))

    if ptp_absent and not ptp_via_cli:
        gaps.append({
            "capability": "ptp",
            "impact": "blocking-for-verification",
            "detail": "No PTP/gPTP YANG module on any reachable switch, and no "
                      "CLI reading either. A CNC cannot read the time base its "
                      "Qbv base-times are anchored to, nor confirm gPTP lock. "
                      "Schedules can be installed but not time-verified.",
            "affects": ptp_absent,
            "workaround": "Verify gPTP out of band (ISTAX CLI `show ptp 0 "
                          "current`, or ptp4l/pmc on the endpoints as the "
                          "i226-adaptation measurement already does) before "
                          "trusting a schedule.",
        })
    elif ptp_via_cli:
        gaps.append({
            "capability": "ptp",
            "impact": "degraded-read-only",
            "detail": "PTP is readable, but only over the ISTAX CLI: this "
                      "hardware implements no PTP or gPTP YANG module in "
                      "either AN001 v1.2 or v1.3. A CNC therefore gets the "
                      "time base through a screen-scraped, unversioned "
                      "interface, and cannot configure PTP through the same "
                      "channel it configures Qbv.",
            "affects": ptp_via_cli,
            "workaround": "Treat the CLI PTP read as a verification gate "
                          "before installing or trusting a schedule, and "
                          "re-check it around each measurement point rather "
                          "than once per campaign.",
        })

    cli_bridges = sorted(b["id"] for b in reachable
                         if b.get("transport") == "cli")
    if cli_bridges:
        gaps.append({
            "capability": "transport",
            "impact": "degraded",
            "detail": "These bridges were read over the ISTAX CLI because "
                      "their NETCONF server did not answer. CLI reads carry no "
                      "schema validation, the output format is not versioned "
                      "between firmware releases, and NETCONF's transactional "
                      "machinery (candidate datastore, validate, "
                      "confirmed-commit, rollback-on-error) is unavailable — "
                      "which matters most for the write path a CNC will need.",
            "affects": cli_bridges,
            "workaround": "Restore the NETCONF server. `netconf server` being "
                          "present in the running-config while nothing listens "
                          "on port 830 is a firmware regression to raise with "
                          "the manufacturer, not a configuration error.",
        })
    if not _any_support("qci"):
        gaps.append({
            "capability": "qci",
            "impact": "feature-unavailable",
            "detail": "No 802.1Qci PSFP model. Per-stream filtering, policing "
                      "and stream gates cannot be configured over NETCONF.",
            "workaround": "Enforce isolation with VLAN and Qbv alone, or "
                          "configure PSFP out of band if the hardware has it.",
        })
    if not _any_support("qav"):
        gaps.append({
            "capability": "qav",
            "impact": "feature-unavailable",
            "detail": "No 802.1Qav credit-based shaper model. Only strict "
                      "priority and the Qbv gates are reachable for shaping.",
            "workaround": None,
        })
    if not _any_support("qcc"):
        gaps.append({
            "capability": "qcc",
            "impact": "expected",
            "detail": "No 802.1Qcc UNI/stream model. The switches are "
                      "configured directly by writing Qbv and VLAN state; "
                      "there is no stream abstraction on the bridge side. "
                      "This is the normal case for a fully-centralized CNC "
                      "and is not an obstacle.",
            "workaround": "The CNC keeps the stream model itself and renders "
                          "it into per-port Qbv gate-control lists.",
        })

    # Ingress classification. A Qbv schedule gates traffic classes; if the
    # bridge does not derive the traffic class from the frame's PCP, every
    # frame lands in the port's default class no matter how the talker tags
    # it, and gates on the other classes open onto empty queues. This is
    # configuration rather than a missing capability, but it defeats
    # scheduling just as completely, so it belongs in the same list.
    untrusted = []
    non_identity = []
    for name, rec in sorted(records.items()):
        for iface in rec.get("interfaces", []):
            if not iface.get("is_bridge_port"):
                continue
            values = (iface.get("qos") or {}).get("values") or {}
            classification = values.get("ingress-classification") or {}
            if classification.get("trust_tag") is False:
                untrusted.append(f"{name}:{iface.get('name')}")
            if values.get("priority_regeneration_is_identity") is False:
                non_identity.append(f"{name}:{iface.get('name')}")

    if untrusted:
        gaps.append({
            "capability": "ingress-classification",
            "impact": "blocking-for-scheduling",
            "detail": f"{len(untrusted)} bridge port(s) have `qos trust tag "
                      "disabled`, so the PCP a talker sets is ignored and "
                      "every frame is classified to the port's default "
                      "traffic class. A Qbv gate-control list that opens "
                      "classes 1-7 would gate empty queues. This is a "
                      "configuration state, not a missing capability -- the "
                      "hardware can do it.",
            "ports": untrusted,
            "workaround": "Enable tag trust on the ports carrying scheduled "
                          "traffic before installing any schedule, and "
                          "re-run discovery to confirm.",
        })
    if non_identity:
        gaps.append({
            "capability": "priority-regeneration",
            "impact": "operational",
            "detail": f"{len(non_identity)} port(s) map PCP to traffic class "
                      "by something other than the identity, so a stream's "
                      "PCP is not its gate index. The map is read per port "
                      "and recorded under qos/priority_regeneration; a CNC "
                      "must apply it when turning a stream priority into a "
                      "gate.",
            "ports": non_identity,
            "workaround": "Use the per-port map rather than assuming PCP N "
                          "means traffic class N.",
        })

    gaps.append({
        "capability": "datastore-coherence",
        "impact": "operational",
        "detail": "Per Kontron AN001 v1.2 (Data Stores): the sysrepo plugin "
                  "reads running configuration from the switch management "
                  "software only when the plugin starts. Changes made in the "
                  "CLI or web UI after that are not visible over NETCONF, and "
                  "changes written over NETCONF are not persistent until "
                  "saved from the CLI or web UI.",
        "workaround": "Treat NETCONF as the single writer during a campaign. "
                      "Re-read after any out-of-band change, and save "
                      "explicitly if the configuration must survive a reboot.",
    })

    envelope = {
        "min_supported_cycle_time_max_ns": _min_or_none(all_cycle_max),
        "min_supported_gcl_entries_max": _min_or_none(all_gcl_max),
        "min_supported_interval_max_ns": _min_or_none(all_interval_max),
        "traffic_classes": 8,
        "note": "Network-wide envelope: the smallest per-port limit across "
                "every reachable bridge port. A schedule that fits here fits "
                "everywhere in this network.",
    }

    return {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": "IEEE 802.1Qcc fully centralized (network half only; the CUC "
                 "and stream requirements are out of scope of discovery)",
        "run": run_meta,
        "network": {
            "bridges": bridges,
            "links": links,
            "end_stations": [
                {
                    "id": n["id"],
                    "macs": n["macs"],
                    "mgmt_ip": n["mgmt_ip"],
                    "discovered": n["discovered"],
                    "in_inventory": n["in_inventory"],
                    "evidence": n["evidence"],
                }
                for n in topo["nodes"] if n["role"] == "endpoint"
            ],
        },
        "network_capability_envelope": envelope,
        "network_wide_support": {
            key: {
                "all_bridges": _all_support(key),
                "any_bridge": _any_support(key),
            }
            for key in ("bridge", "qbv", "qbu", "qci", "qav", "qcc", "lldp", "ptp")
        },
        "gaps": gaps,
        "crosscheck": topo["crosscheck"],
    }
