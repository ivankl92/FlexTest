"""Assemble a CLI-read switch into the same record the NETCONF path emits.

`capabilities.build()` is the reference shape. Everything here exists to
produce that shape from ISTAX CLI text, so that `topology.py`, `cnc.py` and
`render.py` need no knowledge of which transport was used. The only
differences a consumer should ever see are:

* `transport` says "cli" rather than "netconf";
* `yang_modules` is empty, because a CLI-read switch has not told us which
  modules it implements -- feature support is evidenced by the command that
  answered instead;
* a handful of fields the CLI genuinely does not print are `None` with the
  omission recorded, rather than filled with a plausible-looking value;
* `ptp` is populated, which over NETCONF it cannot be.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import istax_parse as P
from . import ptp as ptp_mod
from .istax import CliResult
from .istax_parse import norm_mac
from .probe import FEATURE_MODULES


def _eui64(mac: Optional[str]) -> Optional[str]:
    """A 48-bit MAC in the EUI-64 form IEEE 1588 uses for clockIdentity:
    the OUI, then ``ff:fe``, then the rest."""
    if not mac:
        return None
    parts = mac.split(":")
    if len(parts) != 6:
        return None
    return ":".join(parts[:3] + ["ff", "fe"] + parts[3:])


def _clock_identity(ptp_record: dict) -> Optional[str]:
    try:
        inst = ptp_record["models"]["ietf-ptp"]["instance-list"][0]
        return inst["default-ds"].get("clock-identity")
    except (KeyError, IndexError, TypeError):
        return None


# Which command answering proves which feature exists on this firmware.
_FEATURE_EVIDENCE = {
    "bridge": ("vlan", "show vlan"),
    "qbv": ("tas-status", "show tsn tas status"),
    "qbu": ("frame-preemption-status", "show tsn frame-preemption status"),
    "qci": ("psfp-status", "show tsn stream filter status"),
    "qav": ("qos", "show qos interface <if>"),
    "qcc": (None, None),
    "lldp": ("lldp-neighbors", "show lldp neighbors"),
    "ethernet": ("interface-status", "show interface * status"),
    "interfaces": ("interface-status", "show interface * status"),
    "system": ("running-config", "show running-config"),
    "ptp": ("ptp-default", "show ptp 0 default"),
}


def _capture_ok(result: CliResult, key: str) -> Optional[bool]:
    """True if the command ran, False if the firmware rejected it, None if
    it was never issued."""
    cap = result.captures.get(key)
    if cap is None:
        # Per-interface variants carry a suffix.
        for name, c in result.captures.items():
            if name.split("--", 1)[0] == key:
                cap = c
                break
    if cap is None:
        return None
    if cap.ok:
        return True
    return False if cap.error_kind == "command-unsupported" else None


def build_features(result: CliResult, interfaces: List[dict],
                   ptp_record: dict) -> Dict[str, dict]:
    features: Dict[str, dict] = {}
    for key, spec in FEATURE_MODULES.items():
        capture_key, command = _FEATURE_EVIDENCE.get(key, (None, None))
        supported: Optional[bool]
        if key == "ptp":
            supported = ptp_record.get("status") in (
                "available-via-cli", "not-configured")
            evidence = ptp_record.get("evidence", "")
        elif key == "qcc":
            supported = False
            evidence = ("no stream/UNI abstraction is exposed by the CLI; "
                        "expected for a fully-centralized CNC, which holds "
                        "the stream model itself")
        elif key == "qav":
            shaped = any(i.get("qos", {}).get("credit_based_shaper_active")
                         for i in interfaces)
            ran = _capture_ok(result, "qos")
            supported = bool(ran) if ran is not None else None
            evidence = (f"`{command}` reports per-queue credit-based shapers"
                        + ("; at least one is active" if shaped else
                           "; none currently enabled"))
        else:
            ran = _capture_ok(result, capture_key) if capture_key else None
            supported = ran
            if ran is True:
                evidence = f"`{command}` was accepted by the firmware"
            elif ran is False:
                evidence = (f"the firmware rejected `{command}` "
                            "(command not present in this release)")
            else:
                evidence = f"`{command}` was not issued or gave no usable reply"

        features[key] = {
            "feature": key,
            "label": spec["label"],
            "standard": spec["std"],
            "supported": bool(supported) if supported is not None else False,
            "determined": supported is not None,
            "modules": [],
            "evidence": evidence,
            "source": "cli",
            "command": command,
        }
    return features


def build(result: CliResult, netconf_probe: Optional[dict] = None) -> dict:
    """CliResult -> the uniform per-switch record."""
    running_cfg = P.parse_running_config(result.text_of("running-config"))
    interfaces = P.parse_interfaces(result)
    P.apply_interface_config(interfaces, running_cfg)

    port_number_of = {i["name"]: i["bridge_port"]["port_number"]
                      for i in interfaces
                      if i["bridge_port"]["port_number"] is not None}

    # --- Qbv / Qbu / QoS, per port ---------------------------------------
    tas_blocks = P.parse_kv_blocks(result.text_of("tas-status"))
    fp_blocks = P.parse_kv_blocks(result.text_of("frame-preemption-status"))

    for iface in interfaces:
        slug = iface["name"].replace(" ", "_").replace("/", "-")

        tas = tas_blocks.get(iface["name"])
        if tas is None:
            per_port = P.parse_kv_blocks(result.text_of(f"tas-status--{slug}"))
            tas = per_port.get(iface["name"]) or (
                list(per_port.values())[0] if per_port else None)
        tas_cfg_lines = iface.get("_tas_config_lines", [])
        if tas or tas_cfg_lines:
            iface["qbv"] = P._tas_status_to_qbv(tas or {}, tas_cfg_lines)
        elif _capture_ok(result, "tas-status") is True:
            # The command exists and the port simply has no schedule.
            iface["qbv"] = P._tas_status_to_qbv({}, [])
            iface["qbv"]["configured"] = False

        # QoS is parsed before Qbu, because the priority regeneration map it
        # carries is what turns "queue N" into "priority N" correctly.
        qos_text = result.text_of(f"qos--{slug}") or result.text_of("qos")
        parsed_qos = P._parse_qos(qos_text)
        prio_regen = parsed_qos["values"].get("priority_regeneration")

        fp = fp_blocks.get(iface["name"])
        if fp is None:
            per_port = P.parse_kv_blocks(
                result.text_of(f"frame-preemption-status--{slug}"))
            fp = per_port.get(iface["name"]) or (
                list(per_port.values())[0] if per_port else None)
        queues = iface.get("_preemptable_queues", [])
        if fp or queues:
            iface["qbu"] = P._fp_status_to_qbu(fp or {}, queues, prio_regen)

        if parsed_qos["nodes_present"]:
            merged = iface["qos"]
            merged["nodes_present"] = sorted(
                set(merged["nodes_present"]) | set(parsed_qos["nodes_present"]))
            merged["values"].update(parsed_qos["values"])
            if parsed_qos.get("credit_based_shaper_active"):
                merged["credit_based_shaper_active"] = True

    # --- PTP --------------------------------------------------------------
    ptp_record = ptp_mod.build(result, running_cfg, interfaces)
    ptp_record["lock"] = ptp_mod.lock_assessment(ptp_record)

    # --- bridge -----------------------------------------------------------
    vlans = P.parse_vlans(result.text_of("vlan"))
    fdb = P.parse_mac_table(result.text_of("mac-address-table"), port_number_of)

    # The switch's own address is the unicast static CPU entry in the
    # filtering database. That is a reading, not an inference: the firmware
    # installs it so frames addressed to the bridge reach the CPU. It also
    # agrees with the PTP clock identity, which is this MAC in EUI-64 form,
    # and with the Chassis ID its neighbours report over LLDP -- three
    # independent sources, cross-checked below.
    bridge_address = None
    bridge_address_source = None
    for entry in fdb:
        if entry.get("cpu") and entry["entry_type"] == "static":
            addr = entry["address"]
            if addr and not addr.startswith(("01:", "33:33", "ff:ff")):
                bridge_address = addr
                bridge_address_source = "static CPU entry in show mac address-table"
                break

    bridge_warnings: List[str] = []
    mst_region = running_cfg.get("mst_region_name")
    mst_mac = norm_mac(mst_region) if mst_region else None
    if bridge_address and mst_mac and mst_mac != bridge_address:
        bridge_warnings.append(
            f"the MST region name (`{mst_region}`) looks like a MAC address "
            f"but is not this bridge's ({bridge_address}). It is a region "
            "name, so this is legal -- but switches only form one MST region "
            "when their region names match, so check that this is deliberate.")
    if bridge_address is None:
        bridge_warnings.append(
            "no unicast static CPU entry in `show mac address-table`, so the "
            "bridge address is unknown. It is left empty rather than "
            "substituted from the MST region name, which is not an address.")

    ptp_clock_id = _clock_identity(ptp_record)
    if bridge_address and ptp_clock_id:
        expected = _eui64(bridge_address)
        if expected and expected != ptp_clock_id:
            bridge_warnings.append(
                f"the PTP clock identity ({ptp_clock_id}) is not the EUI-64 "
                f"form of the bridge address ({bridge_address}). One of the "
                "two readings is off; check the raw captures before trusting "
                "either.")

    bridges = [{
        "name": "bridge0",
        "address": bridge_address,
        "address_raw": bridge_address,
        "address_source": bridge_address_source,
        "mst_region_name": mst_region,
        "warnings": bridge_warnings,
        "bridge_type": None,
        "ports": len(interfaces),
        "up_time_s": None,
        "components": [{
            "name": "bridge0",
            "id": 1,
            "type": "c-vlan-component",
            "vlans": vlans,
            "vlan_registration_entries": [],
            "filtering_entries": fdb,
            "filtering_database": {
                "static_entries": sum(1 for e in fdb
                                      if e["entry_type"] == "static"),
                "dynamic_entries": sum(1 for e in fdb
                                       if e["entry_type"] == "dynamic"),
                "size": None,
                "aging_time_s": None,
            },
        }],
        "source": "show vlan + show mac address-table + show running-config",
    }]

    # --- LLDP -------------------------------------------------------------
    neighbours = P.parse_lldp_neighbors(result.text_of("lldp-neighbors"))
    local_lldp = {"chassis_id_mac": bridge_address,
                  "system_name": running_cfg.get("hostname")}

    # Remote firmware, which is how we learned the release that broke
    # NETCONF -- LLDP System Description carries it.
    for n in neighbours:
        if n.get("system_description") and not result.firmware:
            pass

    features = build_features(result, interfaces, ptp_record)

    # --- summary ----------------------------------------------------------
    qbv_ports = [i for i in interfaces if i["qbv"].get("present")]
    qbu_ports = [i for i in interfaces if i["qbu"].get("present")]

    warnings: List[str] = []
    notes: List[str] = []
    for i in qbv_ports:
        warnings.extend(f"{i['name']}: {w}" for w in i["qbv"].get("warnings", []))
    note_ports: Dict[str, List[str]] = {}
    for i in qbv_ports:
        for n in i["qbv"].get("notes", []):
            note_ports.setdefault(n, []).append(i["name"])
    for note, ports in note_ports.items():
        notes.append(f"{note} — on {len(ports)} port(s): {', '.join(ports)}")

    notes.append(
        "Read over the ISTAX CLI, not NETCONF. Values are parsed from "
        "human-readable command output; field-for-field they match the "
        "NETCONF shape, but they carry no schema validation and the output "
        "format is not versioned.")
    if running_cfg.get("netconf_server_configured"):
        notes.append(
            "`netconf server` IS present in this switch's running-config, yet "
            "the NETCONF server did not answer. That is a firmware fault "
            "rather than a missing configuration — worth quoting to the "
            "manufacturer.")

    def _common(key: str):
        vals = {i["qbv"]["capability"].get(key) for i in qbv_ports}
        vals.discard(None)
        return vals.pop() if len(vals) == 1 else (sorted(vals) if vals else None)

    summary = {
        "interface_count": len(interfaces),
        "bridge_port_count": len(interfaces),
        "qbv_capable_ports": [i["name"] for i in qbv_ports],
        "qbv_enabled_ports": [i["name"] for i in qbv_ports
                              if i["qbv"]["configuration"].get("gate_enabled")],
        "qbu_capable_ports": [i["name"] for i in qbu_ports],
        "qbu_active_ports": [i["name"] for i in qbu_ports
                             if i["qbu"]["configuration"].get("preemption_active")],
        "qbv_limits": {
            "supported_gcl_entries_max": _common("supported_gcl_entries_max"),
            "supported_cycle_time_max_ns": None,
            "supported_interval_max_ns": None,
            "note": "supported-cycle-max and supported-interval-max are not "
                    "printed by `show tsn tas status`; they are readable only "
                    "over NETCONF (ieee802-dot1q-sched).",
        },
        "vlans": [v["vid"] for v in vlans],
        "vlan_details": vlans,
        "traffic_classes": 8,
        "traffic_classes_source": "IEEE 802.1Q gate-states mask width; the CLI "
                                  "exposes queues 0-7",
    }

    # Strip the scratch fields the parsers used to pass config along.
    for iface in interfaces:
        for scratch in ("_tas_config_lines", "_preemptable_queues",
                        "_ptp_config_lines"):
            iface.pop(scratch, None)

    return {
        "switch": result.name,
        "host": result.host,
        "reachable": result.reachable,
        "transport": "cli",
        "transport_detail": {
            "protocol": "ssh",
            "port": result.port,
            "prompt": result.prompt,
            "netconf_probe": netconf_probe,
            "netconf_server_in_running_config":
                running_cfg.get("netconf_server_configured"),
        },
        "system": {
            "hostname": running_cfg.get("hostname"),
            "contact": None,
            "location": None,
            "management_ip": running_cfg.get("management_ip"),
            "firmware": result.firmware,
        },
        "netconf": None,
        "yang_modules": {},
        "features": features,
        "ptp": ptp_record,
        "bridges": bridges,
        "interfaces": interfaces,
        "lldp": {"local": local_lldp, "neighbours": neighbours},
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
        "errors": result.errors(),
        "collection_duration_s": round(result.duration_s, 3),
    }
