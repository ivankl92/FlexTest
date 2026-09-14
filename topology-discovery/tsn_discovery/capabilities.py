"""Extract switch capabilities and configuration from NETCONF replies.

Scope, per the subproject brief: PTP, VLAN, QoS, Qbv (802.1Qbv TAS) and Qbu
(802.1Qbu frame preemption), shaped so that the result is directly usable as
the input side of a Centralized Network Configuration entity.

Two things distinguish a *capability* from a *configuration* here, and the
distinction is kept throughout:

* **Capability** -- what the hardware can do at all. For Qbv that is
  ``supported-list-max`` (how many gate-control entries fit),
  ``supported-cycle-max`` and ``supported-interval-max``. These are
  ``config false`` and only appear in a ``<get>``. A CNC needs them to know
  whether a schedule it computed is installable.
* **Configuration** -- what is currently programmed: the admin gate states,
  the admin control list, cycle time, base time.

Both are reported, in separate sub-objects, for every port.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import xmlutil as X

NS_PER_SECOND = 1_000_000_000


# --- small helpers -------------------------------------------------------

def _rational_to_ns(el) -> Optional[int]:
    """IEEE 802.1Q rational time (numerator/denominator seconds) -> ns."""
    if el is None:
        return None
    num = X.to_int(X.text(el, "numerator"))
    den = X.to_int(X.text(el, "denominator"))
    if num is None or not den:
        return None
    return int(round(num * NS_PER_SECOND / den))


def _ptp_time_to_ns(el) -> Optional[int]:
    """802.1Q ptp-time (seconds + nanoseconds, or seconds +
    fractional-seconds) -> absolute ns. Both spellings occur in the wild:
    AN001 shows ``nanoseconds``, the MG-SOFT capture shows
    ``fractional-seconds``."""
    if el is None:
        return None
    secs = X.to_int(X.text(el, "seconds"), 0) or 0
    nanos = X.to_int(X.text(el, "nanoseconds"))
    if nanos is None:
        frac = X.to_int(X.text(el, "fractional-seconds"))
        nanos = frac if frac is not None else 0
    return secs * NS_PER_SECOND + nanos


def decode_gate_states(value: Optional[int], num_tc: int = 8) -> Optional[dict]:
    """Decode a gate-states bitmask into per-traffic-class open/closed.

    Bit *n* corresponds to traffic class *n*; 1 = gate open. 255 is
    "all eight gates open", which is the KSwitch default.
    """
    if value is None:
        return None
    gates = {f"tc{n}": bool(value & (1 << n)) for n in range(num_tc)}
    return {
        "value": value,
        "binary": format(value & ((1 << num_tc) - 1), f"0{num_tc}b"),
        "open_traffic_classes": [n for n in range(num_tc) if value & (1 << n)],
        "gates": gates,
    }


# --- system --------------------------------------------------------------

def extract_system(result) -> dict:
    out: dict = {"hostname": None, "contact": None, "location": None}
    xml = result.xml_of("system")
    if xml:
        try:
            root = X.parse(xml)
            sys_el = X.first(root, "system")
            if sys_el is not None:
                out["hostname"] = X.text(sys_el, "hostname")
                out["contact"] = X.text(sys_el, "contact")
                out["location"] = X.text(sys_el, "location")
        except Exception as exc:
            out["parse_error"] = str(exc)

    xml_state = result.xml_of("system-state")
    if xml_state:
        try:
            root = X.parse(xml_state)
            platform = X.first(root, "platform")
            if platform is not None:
                out["platform"] = {
                    "os_name": X.text(platform, "os-name"),
                    "os_release": X.text(platform, "os-release"),
                    "os_version": X.text(platform, "os-version"),
                    "machine": X.text(platform, "machine"),
                }
            clock = X.first(root, "clock")
            if clock is not None:
                out["clock"] = {
                    "current_datetime": X.text(clock, "current-datetime"),
                    "boot_datetime": X.text(clock, "boot-datetime"),
                }
        except Exception as exc:
            out["state_parse_error"] = str(exc)
    return out


# --- Qbv -----------------------------------------------------------------

def extract_qbv(bridge_port) -> Optional[dict]:
    """802.1Qbv gate parameters for one bridge port."""
    gp = X.first(bridge_port, "gate-parameters")
    if gp is None:
        return None

    admin_gate_states = X.to_int(X.text(gp, "admin-gate-states"))
    oper_gate_states = X.to_int(X.text(gp, "oper-gate-states"))

    gcl: List[dict] = []
    for entry in X.descendants(gp, "gate-control-entry"):
        gsv = X.to_int(X.text(entry, "gate-state-value"))
        gcl.append({
            "index": X.to_int(X.text(entry, "index")),
            "operation": X.strip_identity(X.text(entry, "operation-name")),
            "gate_state_value": gsv,
            "gate_states": decode_gate_states(gsv),
            "time_interval_ns": X.to_int(X.text(entry, "time-interval-value")),
        })
    gcl.sort(key=lambda e: (e["index"] is None, e["index"]))

    cycle_ns = _rational_to_ns(X.child(gp, "admin-cycle-time"))
    oper_cycle_ns = _rational_to_ns(X.child(gp, "oper-cycle-time"))
    sup_cycle_ns = _rational_to_ns(X.child(gp, "supported-cycle-max"))

    gcl_total_ns = sum(e["time_interval_ns"] or 0 for e in gcl) if gcl else 0

    capability = {
        "supported_gcl_entries_max": X.to_int(X.text(gp, "supported-list-max")),
        "supported_cycle_time_max_ns": sup_cycle_ns,
        "supported_interval_max_ns": X.to_int(X.text(gp, "supported-interval-max")),
        "supports_cycle_time_extension": X.child(gp, "admin-cycle-time-extension") is not None,
    }

    configuration = {
        "gate_enabled": X.to_bool(X.text(gp, "gate-enabled")),
        "config_change": X.to_bool(X.text(gp, "config-change")),
        "config_pending": X.to_bool(X.text(gp, "config-pending")),
        "admin_gate_states": decode_gate_states(admin_gate_states),
        "oper_gate_states": decode_gate_states(oper_gate_states),
        "admin_cycle_time_ns": cycle_ns,
        "oper_cycle_time_ns": oper_cycle_ns,
        "admin_cycle_time_extension_ns": X.to_int(
            X.text(gp, "admin-cycle-time-extension")),
        "admin_base_time_ns": _ptp_time_to_ns(X.child(gp, "admin-base-time")),
        "oper_base_time_ns": _ptp_time_to_ns(X.child(gp, "oper-base-time")),
        "admin_control_list_length": X.to_int(
            X.text(gp, "admin-control-list-length"), len(gcl)),
        "admin_control_list": gcl,
    }

    # Consistency checks a CNC would otherwise have to repeat. Severity
    # depends on whether the gate is actually running: an inconsistent
    # schedule on a port whose gate is disabled is inert, and on this
    # platform the factory default is exactly that -- admin-cycle-time
    # 100/1000 s (100 ms) against a supported-cycle-max of ~33.5 ms. Every
    # idle port would otherwise raise a warning that means nothing.
    enabled = X.to_bool(X.text(gp, "gate-enabled"))
    warnings: List[str] = []
    notes: List[str] = []
    sink = warnings if enabled else notes

    if gcl and cycle_ns and gcl_total_ns != cycle_ns:
        sink.append(
            f"gate-control-list intervals sum to {gcl_total_ns} ns but "
            f"admin-cycle-time is {cycle_ns} ns"
        )
    if (capability["supported_gcl_entries_max"] is not None
            and len(gcl) > capability["supported_gcl_entries_max"]):
        warnings.append(
            f"{len(gcl)} gate-control entries exceed supported-list-max "
            f"{capability['supported_gcl_entries_max']}"
        )
    if (capability["supported_cycle_time_max_ns"] and cycle_ns
            and cycle_ns > capability["supported_cycle_time_max_ns"]):
        sink.append(
            f"admin-cycle-time {cycle_ns} ns exceeds supported-cycle-max "
            f"{capability['supported_cycle_time_max_ns']} ns"
            + ("" if enabled else " (gate disabled, so not in effect)")
        )

    return {
        "present": True,
        "capability": capability,
        "configuration": configuration,
        "gcl_total_ns": gcl_total_ns,
        "warnings": warnings,
        "notes": notes,
    }


# --- Qbu -----------------------------------------------------------------

def extract_qbu(bridge_port) -> Optional[dict]:
    """802.1Qbu frame preemption parameters for one bridge port."""
    fp = X.first(bridge_port, "frame-preemption-parameters")
    if fp is None:
        return None

    status_table = X.first(fp, "frame-preemption-status-table")
    per_priority: Dict[str, Optional[str]] = {}
    if status_table is not None:
        for n in range(8):
            per_priority[f"priority{n}"] = X.text(status_table, f"priority{n}")
        # Some implementations model this as a list instead of eight leaves.
        for entry in X.descendants(status_table, "frame-preemption-status"):
            prio = X.text(entry, "priority")
            val = X.text(entry, "frame-preemption-status") or X.text(entry, "status")
            if prio is not None:
                per_priority[f"priority{prio}"] = val

    preemptable = [k for k, v in per_priority.items()
                   if v and "preempt" in v.lower()]
    express = [k for k, v in per_priority.items()
               if v and "express" in v.lower()]

    return {
        "present": True,
        "capability": {
            "hold_advance_ns": X.to_int(X.text(fp, "hold-advance")),
            "release_advance_ns": X.to_int(X.text(fp, "release-advance")),
        },
        "configuration": {
            "frame_preemption_status": {k: v for k, v in per_priority.items()
                                        if v is not None},
            "preemptable_priorities": sorted(preemptable),
            "express_priorities": sorted(express),
            "preemption_active": X.to_bool(X.text(fp, "preemption-active")),
            "hold_request": X.text(fp, "hold-request"),
        },
    }


# --- QoS -----------------------------------------------------------------

QOS_LEAVES = [
    "default-priority",
    "priority-regeneration",
    "acceptable-frame-types",
    "enable-ingress-filtering",
    "traffic-class",
    "traffic-class-table",
    "priority-code-point",
    "pcp-selection",
    "pcp-decoding-table",
    "pcp-encoding-table",
    "transmission-selection-algorithm-table",
    "queue-max-sdu-table",
]


def extract_qos(bridge_port) -> dict:
    """802.1Q QoS-relevant bridge-port nodes.

    The KSwitch NETCONF plugin implements only a subset of 802.1Q, and which
    subset is firmware-dependent, so this collects whatever of the standard
    QoS nodes is actually present rather than assuming a fixed shape.
    ``nodes_present`` is part of the output: for a CNC, knowing that a knob
    is *not* reachable over NETCONF is as important as its value.
    """
    out: dict = {"nodes_present": [], "values": {}}

    for leaf in QOS_LEAVES:
        el = X.first(bridge_port, leaf)
        if el is None:
            continue
        out["nodes_present"].append(leaf)
        if len(el) == 0:
            out["values"][leaf] = (el.text or "").strip()
        else:
            # container or list: summarise its direct children
            sub = {}
            for c in el:
                key = X.lname(c)
                val = (c.text or "").strip() if len(c) == 0 else {
                    X.lname(g): (g.text or "").strip() for g in c
                }
                sub.setdefault(key, []).append(val)
            out["values"][leaf] = sub

    # priority regeneration table, when present as a list
    regen = {}
    for entry in X.descendants(bridge_port, "priority-regeneration"):
        for n in range(8):
            v = X.text(entry, f"priority{n}")
            if v is not None:
                regen[f"priority{n}"] = X.to_int(v)
    if regen:
        out["priority_regeneration"] = regen

    # traffic-class table: priority -> traffic class
    tc_map = {}
    for entry in X.descendants(bridge_port, "traffic-class-table"):
        for n in range(8):
            v = X.text(entry, f"priority{n}")
            if v is not None:
                tc_map[f"priority{n}"] = X.to_int(v)
    if tc_map:
        out["traffic_class_map"] = tc_map

    return out


# --- interfaces ----------------------------------------------------------

def extract_ethernet(interface) -> dict:
    """802.3 ethernet attributes, where the switch exposes them."""
    eth = X.first(interface, "ethernet")
    if eth is None:
        return {}
    speed = X.text(eth, "speed")
    return {
        "auto_negotiation": X.to_bool(
            X.deep_text(eth, "enable") if X.first(eth, "auto-negotiation") is not None
            else None),
        "duplex": X.text(eth, "duplex"),
        "speed": speed,
        "max_frame_length": X.to_int(X.text(eth, "max-frame-length")),
        "flow_control": X.text(eth, "flow-control"),
    }


def _speed_mbps(interface, eth: dict) -> Optional[int]:
    """Best-effort link speed in Mbit/s.

    ietf-interfaces reports ``speed`` in bit/s as a uint64; the 802.3 model
    reports it as a decimal64 in bit/s; and the KSwitch port naming itself
    carries the speed (``Gi 1/1`` = 1 Gbit/s, ``2.5G 1/1`` = 2.5 Gbit/s).
    The name is used only as a last resort and is flagged in the output.
    """
    for src in (X.text(interface, "speed"), eth.get("speed")):
        if src:
            try:
                val = float(src)
                if val > 0:
                    return int(val / 1_000_000)
            except (TypeError, ValueError):
                pass
    name = (X.text(interface, "name") or "").strip()
    if name.lower().startswith("2.5g"):
        return 2500
    if name.lower().startswith("gi"):
        return 1000
    if name.lower().startswith("10g"):
        return 10000
    if name.lower().startswith("fa"):
        return 100
    return None


def extract_interfaces(result) -> List[dict]:
    xml = result.xml_of("interfaces")
    if not xml:
        return []
    try:
        root = X.parse(xml)
    except Exception:
        return []

    out: List[dict] = []
    for iface in X.descendants(root, "interface"):
        name = X.text(iface, "name")
        if not name:
            continue
        bp = X.first(iface, "bridge-port")
        eth = extract_ethernet(iface)

        entry: dict = {
            "name": name,
            "type": X.strip_identity(X.text(iface, "type")),
            "if_index": X.to_int(X.text(iface, "if-index")),
            "enabled": X.to_bool(X.text(iface, "enabled")),
            "admin_status": X.text(iface, "admin-status"),
            "oper_status": X.text(iface, "oper-status"),
            "description": X.text(iface, "description"),
            "phys_address": X.norm_mac(X.text(iface, "phys-address")),
            "speed_mbps": _speed_mbps(iface, eth),
            "speed_source": "reported" if (X.text(iface, "speed") or eth.get("speed"))
                            else ("derived-from-port-name" if name else None),
            "ethernet": eth or None,
            "is_bridge_port": bp is not None,
        }

        if bp is not None:
            entry["bridge_port"] = {
                "component_name": X.text(bp, "component-name"),
                "port_number": X.to_int(X.text(bp, "port-number")),
                "pvid": X.to_int(X.text(bp, "pvid")),
                "port_type": X.strip_identity(X.text(bp, "port-type")),
            }
            entry["qbv"] = extract_qbv(bp) or {"present": False}
            entry["qbu"] = extract_qbu(bp) or {"present": False}
            entry["qos"] = extract_qos(bp)
        else:
            entry["qbv"] = {"present": False}
            entry["qbu"] = {"present": False}
            entry["qos"] = {"nodes_present": [], "values": {}}

        out.append(entry)

    out.sort(key=lambda e: e["name"])
    return out


# --- bridge / VLAN -------------------------------------------------------

def extract_bridges(result) -> List[dict]:
    xml = result.xml_of("bridges")
    if not xml:
        return []
    try:
        root = X.parse(xml)
    except Exception:
        return []

    bridges: List[dict] = []
    for bridge in X.descendants(root, "bridge"):
        if X.lname(bridge) != "bridge":
            continue
        b: dict = {
            "name": X.text(bridge, "name"),
            "address": X.norm_mac(X.text(bridge, "address")),
            "address_raw": X.text(bridge, "address"),
            "bridge_type": X.strip_identity(X.text(bridge, "bridge-type")),
            "ports": X.to_int(X.text(bridge, "ports")),
            "up_time_s": X.to_int(X.text(bridge, "up-time")),
            "components": [],
        }

        for comp in X.children(bridge, "component"):
            c: dict = {
                "name": X.text(comp, "name"),
                "id": X.to_int(X.text(comp, "id")),
                "type": X.strip_identity(X.text(comp, "type")),
                "vlans": [],
                "vlan_registration_entries": [],
                "filtering_entries": [],
            }

            for vlan in X.descendants(comp, "vlan"):
                vid = X.to_int(X.text(vlan, "vid"))
                if vid is None:
                    continue
                c["vlans"].append({
                    "vid": vid,
                    "name": X.text(vlan, "name"),
                    "untagged_ports": _port_list(X.text(vlan, "untagged-ports")),
                    "egress_ports": _port_list(X.text(vlan, "egress-ports")),
                })

            for vre in X.descendants(comp, "vlan-registration-entry"):
                c["vlan_registration_entries"].append({
                    "database_id": X.to_int(X.text(vre, "database-id")),
                    "vids": X.text(vre, "vids"),
                    "entry_type": X.strip_identity(X.text(vre, "entry-type")),
                    "port_map": _port_map(vre),
                })

            for fe in X.descendants(comp, "filtering-entry"):
                c["filtering_entries"].append({
                    "database_id": X.to_int(X.text(fe, "database-id")),
                    "vids": X.text(fe, "vids"),
                    "address": X.norm_mac(X.text(fe, "address")),
                    "address_raw": X.text(fe, "address"),
                    "entry_type": X.strip_identity(X.text(fe, "entry-type")),
                    "status": X.strip_identity(X.text(fe, "status")),
                    "port_map": _port_map(fe),
                })

            fdb = X.first(comp, "filtering-database")
            if fdb is not None:
                c["filtering_database"] = {
                    "static_entries": X.to_int(X.text(fdb, "static-entries")),
                    "dynamic_entries": X.to_int(X.text(fdb, "dynamic-entries")),
                    "size": X.to_int(X.text(fdb, "size")),
                    "aging_time_s": X.to_int(X.text(fdb, "aging-time")),
                }

            c["vlans"].sort(key=lambda v: v["vid"])
            b["components"].append(c)

        bridges.append(b)
    return bridges


def _port_list(value: Optional[str]) -> List[int]:
    if not value:
        return []
    out = []
    for tok in str(value).replace(",", " ").split():
        v = X.to_int(tok)
        if v is not None:
            out.append(v)
    return out


def _port_map(el) -> List[dict]:
    out = []
    for pm in X.children(el, "port-map"):
        ref = X.to_int(X.text(pm, "port-ref"))
        svre = X.first(pm, "static-vlan-registration-entries")
        entry = {"port_ref": ref}
        if svre is not None:
            entry["registrar_admin_control"] = X.strip_identity(
                X.text(svre, "registrar-admin-control"))
            entry["vlan_transmitted"] = X.strip_identity(
                X.text(svre, "vlan-transmitted"))
        sfe = X.first(pm, "static-filtering-entries")
        if sfe is not None:
            entry["control_element"] = X.strip_identity(
                X.text(sfe, "control-element"))
        out.append(entry)
    return out


# --- assembly ------------------------------------------------------------

def build(result, probe_result: dict) -> dict:
    """Assemble the full capability record for one switch."""
    interfaces = extract_interfaces(result)
    bridges = extract_bridges(result)

    qbv_ports = [i for i in interfaces if i["qbv"].get("present")]
    qbu_ports = [i for i in interfaces if i["qbu"].get("present")]
    bridge_ports = [i for i in interfaces if i.get("is_bridge_port")]

    all_vlans: Dict[int, dict] = {}
    for b in bridges:
        for c in b["components"]:
            for v in c["vlans"]:
                all_vlans.setdefault(v["vid"], v)

    warnings: List[str] = []
    notes: List[str] = []
    for i in qbv_ports:
        for w in i["qbv"].get("warnings", []):
            warnings.append(f"{i['name']}: {w}")
    # Port-level notes are collapsed: the same factory default on eight ports
    # is one finding, not eight.
    note_ports: Dict[str, List[str]] = {}
    for i in qbv_ports:
        for n in i["qbv"].get("notes", []):
            note_ports.setdefault(n, []).append(i["name"])
    for note, ports in note_ports.items():
        notes.append(f"{note} — on {len(ports)} port(s): {', '.join(ports)}")

    # Qbv limits are per-port in the model but identical across ports on this
    # platform; report the common value when they agree, and flag if not.
    def _common(key: str):
        vals = {i["qbv"]["capability"].get(key) for i in qbv_ports}
        vals.discard(None)
        if len(vals) == 1:
            return vals.pop()
        if len(vals) > 1:
            warnings.append(f"Qbv {key} differs between ports: {sorted(vals)}")
            return sorted(vals)
        return None

    summary = {
        "interface_count": len(interfaces),
        "bridge_port_count": len(bridge_ports),
        "qbv_capable_ports": [i["name"] for i in qbv_ports],
        "qbv_enabled_ports": [i["name"] for i in qbv_ports
                              if i["qbv"]["configuration"].get("gate_enabled")],
        "qbu_capable_ports": [i["name"] for i in qbu_ports],
        "qbu_active_ports": [i["name"] for i in qbu_ports
                             if i["qbu"]["configuration"].get("preemption_active")],
        "qbv_limits": {
            "supported_gcl_entries_max": _common("supported_gcl_entries_max"),
            "supported_cycle_time_max_ns": _common("supported_cycle_time_max_ns"),
            "supported_interval_max_ns": _common("supported_interval_max_ns"),
        },
        "vlans": sorted(all_vlans.keys()),
        "vlan_details": [all_vlans[v] for v in sorted(all_vlans)],
        "traffic_classes": 8,
        "traffic_classes_source": "IEEE 802.1Q gate-states bitmask width; the "
                                  "switch does not expose a traffic-class count leaf",
    }

    return {
        "switch": result.name,
        "host": result.host,
        "reachable": result.reachable,
        "system": extract_system(result),
        "netconf": probe_result["netconf"],
        "yang_modules": probe_result["yang_modules"],
        "features": probe_result["features"],
        "ptp": None,               # filled in by the caller from probe.ptp_finding
        "bridges": bridges,
        "interfaces": interfaces,
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
        "errors": result.errors(),
        "collection_duration_s": round(result.duration_s, 3),
    }
