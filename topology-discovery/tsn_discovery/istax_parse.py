"""Parse ISTAX CLI output into the YANG-shaped records the NETCONF path emits.

The point of this module is **uniformity**. Everything downstream --
`topology.py`, `cnc.py`, `render.py` -- must not care which transport a
switch was read over. So a record built here has exactly the keys
`capabilities.build()` produces, with the same units, the same decoded gate
bitmasks, and the same nested shapes, differing only in a `transport` field
and in per-item `evidence` naming the CLI command instead of a YANG path.

Where the CLI genuinely cannot supply something the NETCONF path can, the
field is `None` and the reason is recorded. It is never guessed. The two
places this bites are called out at their definitions:

* `supported-cycle-max` / `supported-interval-max` (Qbv) are not printed by
  `show tsn tas status`, which reports only `SupportedListMax`.
* YANG module names and revisions do not exist for a CLI-read switch, so
  feature support is evidenced by the command that answered instead.

Parsers are written against the real output in `docs/config.txt` from
KSwitchTSN-1 (GA-3.06) and the example outputs in Microchip AN1185 (TSN) and
AN1295 (PTP).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import capabilities as caps_mod
from .istax import CliResult, canonical_ifname, expand_port_list
from .xmlutil import norm_mac, to_int

NS_PER_SECOND = 1_000_000_000


# --- generic helpers -----------------------------------------------------

def _clean_lines(text: Optional[str]) -> List[str]:
    if not text:
        return []
    out = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.startswith("! command:"):
            continue
        out.append(line)
    return out


def parse_table(text: Optional[str]) -> List[Dict[str, str]]:
    """Parse an ISTAX ``show`` table using its dashed separator line.

    ISTAX prints a header row, a row of dash groups, then data. The dash row
    is the authoritative column geometry -- splitting data rows on
    whitespace breaks as soon as a value contains a space (``Media Type``,
    ``Operational Warnings``, interface ranges), and those are exactly the
    columns worth reading.

    The final column is treated as open-ended: ``show vlan`` prints a
    10-dash ``Interfaces`` column and then writes ``Gi 1/1-6 2.5G 1/1-2``
    into it.
    """
    lines = _clean_lines(text)
    sep_idx = None
    for i, line in enumerate(lines):
        if line.strip() and set(line.strip()) <= set("- "):
            sep_idx = i
            break

    if sep_idx is None:
        # Not every ISTAX table has a separator: `show mac address-table`
        # prints the header and goes straight into data. Fall back to the
        # header's own geometry, treating a run of two or more spaces as a
        # column break so that multi-word headings ("MAC Address") stay
        # one column.
        header_idx = next((i for i, l in enumerate(lines) if l.strip()), None)
        if header_idx is None:
            return []
        spans = _spans_from_header(lines[header_idx])
        if len(spans) < 2:
            return []
        header_line = lines[header_idx]
        data_lines = lines[header_idx + 1:]
        return _slice_rows(header_line, data_lines, spans)

    if sep_idx == 0:
        return []

    spans: List[Tuple[int, Optional[int]]] = []
    for m in re.finditer(r"-+", lines[sep_idx]):
        spans.append((m.start(), m.end()))
    if not spans:
        return []
    spans[-1] = (spans[-1][0], None)                 # last column runs on

    return _slice_rows(lines[sep_idx - 1], lines[sep_idx + 1:], spans)


def _spans_from_header(header: str) -> List[Tuple[int, Optional[int]]]:
    """Column spans inferred from a header line with no separator under it."""
    spans: List[Tuple[int, Optional[int]]] = []
    starts = [m.start() for m in re.finditer(r"(?<!\S)\S", header)
              if m.start() == 0 or header[max(0, m.start() - 2):m.start()] == "  "]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else None
        spans.append((start, end))
    return spans


def _slice_rows(header_line: str, data_lines: List[str],
                spans: List[Tuple[int, Optional[int]]]) -> List[Dict[str, str]]:
    headers = []
    for start, end in spans:
        headers.append(header_line[start:end].strip() if end
                       else header_line[start:].strip())

    rows: List[Dict[str, str]] = []
    for line in data_lines:
        if not line.strip():
            continue
        if set(line.strip()) <= set("- "):
            break
        row = {}
        for (start, end), key in zip(spans, headers):
            value = line[start:end] if end else line[start:]
            row[key or f"col{start}"] = value.strip()
        if any(row.values()):
            rows.append(row)
    return rows


def parse_tables(text: Optional[str]) -> List[List[Dict[str, str]]]:
    """Parse every table in one command's output.

    Several PTP commands print two or three tables back to back --
    ``show ptp 0 default`` prints clock identity, then clock quality, then
    protocol -- so a single-table parser silently loses two thirds of the
    answer.
    """
    lines = _clean_lines(text)
    seps = [i for i, line in enumerate(lines)
            if line.strip() and set(line.strip()) <= set("- ")]
    tables: List[List[Dict[str, str]]] = []
    for n, sep in enumerate(seps):
        if sep == 0:
            continue
        end = seps[n + 1] - 1 if n + 1 < len(seps) else len(lines)
        chunk = "\n".join(lines[sep - 1:end])
        rows = parse_table(chunk)
        if rows:
            tables.append(rows)
    return tables


def parse_kv_blocks(text: Optional[str],
                    block_start: str = "interface") -> Dict[str, Dict[str, str]]:
    """Parse ``key : value`` blocks introduced by an ``interface <name>`` line.

    Used by ``show tsn tas status`` and ``show tsn frame-preemption
    status``, both of which print one such block per port.
    """
    blocks: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for line in _clean_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(rf"^{block_start}\s+(\S.*)$", stripped, re.I)
        if m:
            current = canonical_ifname(m.group(1).strip())
            blocks.setdefault(current, {})
            continue
        if re.match(rf"^{block_start}\s*$", stripped, re.I):
            current = current or "__unnamed__"
            blocks.setdefault(current, {})
            continue
        if current is None:
            continue
        kv = re.match(r"^([A-Za-z][\w \-]*?)\s*:\s*(.*)$", stripped)
        if kv:
            key = kv.group(1).strip()
            blocks[current][key] = kv.group(2).strip()
            continue
        # Continuation of the previous value (AN1185 wraps GateControlEntry
        # lines onto a second line).
        if blocks[current]:
            last_key = list(blocks[current])[-1]
            blocks[current][last_key] += " " + stripped
    return blocks


def _flag(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "yes", "enabled", "up", "on"):
        return True
    if v in ("false", "no", "disabled", "down", "off"):
        return False
    return None


def _duration_ns(value: Optional[str]) -> Optional[int]:
    """"110 ms", "9000 nanoseconds", "20000000 nanoseconds" -> ns."""
    if not value:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([a-z]*)", value.strip(), re.I)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "ns").lower()
    factor = {
        "": 1, "ns": 1, "nanosecond": 1, "nanoseconds": 1,
        "us": 1_000, "usec": 1_000, "microseconds": 1_000,
        "ms": 1_000_000, "msec": 1_000_000, "milliseconds": 1_000_000,
        "s": NS_PER_SECOND, "sec": NS_PER_SECOND, "seconds": NS_PER_SECOND,
    }.get(unit)
    if factor is None:
        return None
    return int(round(num * factor))


def _seconds_nanos_ns(value: Optional[str]) -> Optional[int]:
    """"4300 seconds, 500 nanoseconds" -> absolute nanoseconds."""
    if not value:
        return None
    secs = re.search(r"(-?\d+)\s*second", value, re.I)
    nanos = re.search(r"(-?\d+)\s*nanosecond", value, re.I)
    if not secs and not nanos:
        return None
    return ((int(secs.group(1)) if secs else 0) * NS_PER_SECOND
            + (int(nanos.group(1)) if nanos else 0))


def _hex_or_int(value: Optional[str]) -> Optional[int]:
    """Gate-state masks arrive as 0x1f over CLI and as 31 over NETCONF."""
    if value is None:
        return None
    v = value.strip().split()[0] if value.strip() else ""
    try:
        return int(v, 16) if v.lower().startswith("0x") else int(v)
    except (ValueError, IndexError):
        return None


# GateOperation in the CLI vs operation-name in ieee802-dot1q-sched.
_GATE_OP_TO_YANG = {
    "set": "set-gate-states",
    "set-gate-states": "set-gate-states",
    "set-hold": "set-and-hold-mac",
    "set-and-hold-mac": "set-and-hold-mac",
    "set-release": "set-and-release-mac",
    "set-and-release-mac": "set-and-release-mac",
}


# --- interfaces ----------------------------------------------------------

_LINK_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([GM])(fdx|hdx)?$", re.I)


def _parse_link(value: Optional[str]) -> Tuple[Optional[str], Optional[int],
                                               Optional[str]]:
    """"1Gfdx" -> ("up", 1000, "full"); "Down" -> ("down", None, None)."""
    if not value:
        return None, None, None
    v = value.strip()
    if v.lower() in ("down", "", "-"):
        return "down", None, None
    m = _LINK_RE.match(v)
    if not m:
        return "up", None, None
    num = float(m.group(1))
    mbps = int(num * 1000) if m.group(2).upper() == "G" else int(num)
    duplex = {"fdx": "full", "hdx": "half"}.get((m.group(3) or "").lower())
    return "up", mbps, duplex


def parse_interfaces(result: CliResult) -> List[dict]:
    """``show interface * status`` -> the ietf-interfaces list shape.

    Port numbering: the CLI does not print an ifIndex, so the position in
    this table is used as both ``if-index`` and ``bridge-port/port-number``.
    That is what the hardware's own ordering implies (Gi 1/1..1/6 then
    2.5G 1/1..1/2 = ports 1..8) and it is corroborated by LLDP, which
    reports Port ID 4 for GigabitEthernet 1/4. The derivation is recorded in
    ``port_number_source`` so a reader is never misled into thinking the
    switch stated it.
    """
    rows = parse_table(result.text_of("interface-status"))
    out: List[dict] = []
    for position, row in enumerate(rows, start=1):
        raw_name = row.get("Interface") or ""
        name = canonical_ifname(raw_name)
        if not name:
            continue
        oper, mbps, duplex = _parse_link(row.get("Link"))
        enabled = _flag(row.get("Mode"))
        out.append({
            "name": name,
            "type": "ethernetCsmacd",
            "if_index": position,
            "enabled": enabled,
            "admin_status": "up" if enabled else "down" if enabled is False else None,
            "oper_status": oper,
            "description": (row.get("Operational Warnings") or "").strip() or None,
            "phys_address": None,
            "speed_mbps": mbps,
            "speed_source": "show interface * status (Link column)",
            "ethernet": {
                "duplex": duplex,
                "auto_negotiation": _flag(row.get("Aneg")),
                "media_type": (row.get("Media Type") or "").strip() or None,
                "sfp_family": (row.get("SFP Family") or "").strip() or None,
            },
            "is_bridge_port": True,
            "bridge_port": {
                "component_name": None,
                "port_number": position,
                "port_number_source": "derived from show interface * status "
                                      "ordering; corroborated by LLDP Port ID",
                "pvid": None,             # filled from running-config
                "port_type": None,
            },
            "qbv": {"present": False},
            "qbu": {"present": False},
            "qos": {"nodes_present": [], "values": {}},
        })
    out.sort(key=lambda e: e["name"])
    return out


# --- running-config ------------------------------------------------------

def parse_running_config(text: Optional[str]) -> dict:
    """Split `show running-config` into the parts the record needs."""
    cfg: dict = {
        "hostname": None,
        "bridge_address": None,
        "management_ip": None,
        "vlans_declared": [],
        "netconf_server_configured": False,
        "interfaces": {},
        "ptp_global": [],
    }
    current: Optional[str] = None
    in_vlan_iface = False

    for line in _clean_lines(text):
        stripped = line.strip()
        if not stripped or stripped == "!":
            if stripped == "!":
                current = None
                in_vlan_iface = False
            continue

        m = re.match(r"^interface\s+vlan\s+(\d+)", stripped, re.I)
        if m:
            current, in_vlan_iface = None, True
            continue
        m = re.match(r"^interface\s+(\S.*)$", stripped, re.I)
        if m:
            current = canonical_ifname(m.group(1).strip())
            in_vlan_iface = False
            cfg["interfaces"].setdefault(current, [])
            continue

        if in_vlan_iface:
            m = re.match(r"^ip address\s+(\d+\.\d+\.\d+\.\d+)", stripped, re.I)
            if m:
                cfg["management_ip"] = m.group(1)
            continue

        if current:
            cfg["interfaces"][current].append(stripped)
            continue

        # global lines
        m = re.match(r"^hostname\s+(\S+)", stripped, re.I)
        if m:
            cfg["hostname"] = m.group(1)
            continue
        # `spanning-tree mst name 00-80-82-b9-65-33` carries the bridge base
        # MAC on this platform -- the same value NETCONF reports as
        # ieee802-dot1q-bridge/bridge/address.
        m = re.match(r"^spanning-tree mst name\s+(\S+)", stripped, re.I)
        if m and norm_mac(m.group(1)):
            cfg["bridge_address"] = norm_mac(m.group(1))
            continue
        m = re.match(r"^vlan\s+([\d,\-]+)\s*$", stripped, re.I)
        if m:
            cfg["vlans_declared"] = _expand_id_list(m.group(1))
            continue
        if re.match(r"^netconf server\s*$", stripped, re.I):
            cfg["netconf_server_configured"] = True
            continue
        if re.match(r"^ptp\s+\d+\s", stripped, re.I):
            cfg["ptp_global"].append(stripped)
            continue
    return cfg


def _expand_id_list(value: str) -> List[int]:
    """"1,2" or "1-4,10" -> [1,2] / [1,2,3,4,10]."""
    out: List[int] = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            v = to_int(part)
            if v is not None:
                out.append(v)
    return sorted(set(out))


def apply_interface_config(interfaces: List[dict], cfg: dict) -> None:
    """Fold per-interface running-config lines into the interface records."""
    by_name = {i["name"]: i for i in interfaces}

    for name, lines in cfg["interfaces"].items():
        iface = by_name.get(name)
        if iface is None:
            continue
        bp = iface["bridge_port"]
        tas_lines, fp_queues, ptp_lines = [], [], []

        for line in lines:
            m = re.match(r"^switchport access vlan\s+(\d+)", line, re.I)
            if m:
                bp["pvid"] = to_int(m.group(1))
                continue
            m = re.match(r"^switchport hybrid native vlan\s+(\d+)", line, re.I)
            if m:
                bp["pvid"] = to_int(m.group(1))
                continue
            m = re.match(r"^switchport mode\s+(\S+)", line, re.I)
            if m:
                bp["port_type"] = m.group(1).lower()
                continue
            m = re.match(r"^switchport hybrid allowed vlan\s+([\d,\-]+)", line, re.I)
            if m:
                bp["allowed_vlans"] = _expand_id_list(m.group(1))
                continue
            if re.match(r"^switchport hybrid ingress-filtering", line, re.I):
                iface["qos"]["values"]["enable-ingress-filtering"] = "true"
                if "enable-ingress-filtering" not in iface["qos"]["nodes_present"]:
                    iface["qos"]["nodes_present"].append("enable-ingress-filtering")
                continue
            if re.match(r"^tsn tas\b", line, re.I):
                tas_lines.append(line)
                continue
            m = re.match(r"^tsn frame-preemption queue\s+([\d,\-]+)", line, re.I)
            if m:
                fp_queues.extend(_expand_id_list(m.group(1)))
                continue
            if re.match(r"^ptp\s+\d+", line, re.I):
                ptp_lines.append(line)
                continue

        iface["_tas_config_lines"] = tas_lines
        iface["_preemptable_queues"] = sorted(set(fp_queues))
        iface["_ptp_config_lines"] = ptp_lines

    if cfg.get("management_ip"):
        pass          # recorded at record level, not per interface


def parse_tas_config_lines(lines: List[str]) -> dict:
    """`tsn tas ...` running-config lines -> the Qbv configuration shape."""
    cfg: dict = {
        "gate_enabled": False,
        "admin_cycle_time_ns": None,
        "admin_cycle_time_extension_ns": None,
        "admin_base_time_ns": None,
        "admin_gate_states": None,
        "admin_control_list": [],
        "admin_control_list_length": None,
        "max_sdu": {},
    }
    entries: Dict[int, dict] = {}
    gate_states_bits = 0
    gate_states_seen = False

    for line in lines:
        if re.match(r"^tsn tas gate-enabled\s*$", line, re.I):
            cfg["gate_enabled"] = True
            continue
        m = re.match(r"^tsn tas cycle-time\s+(\d+)\s*(ms|us|ns)?", line, re.I)
        if m:
            cfg["admin_cycle_time_ns"] = _duration_ns(
                f"{m.group(1)} {m.group(2) or 'ns'}")
            continue
        m = re.match(r"^tsn tas cycle-time-extension\s+(\d+)", line, re.I)
        if m:
            cfg["admin_cycle_time_extension_ns"] = to_int(m.group(1))
            continue
        m = re.match(r"^tsn tas base-time seconds\s+(\d+)\s+nanoseconds\s+(\d+)",
                     line, re.I)
        if m:
            cfg["admin_base_time_ns"] = (int(m.group(1)) * NS_PER_SECOND
                                         + int(m.group(2)))
            continue
        m = re.match(r"^tsn tas control-list-length\s+(\d+)", line, re.I)
        if m:
            cfg["admin_control_list_length"] = to_int(m.group(1))
            continue
        m = re.match(r"^tsn tas max-sdu queue\s+([\d,\-]+)\s+(\d+)", line, re.I)
        if m:
            for q in _expand_id_list(m.group(1)):
                cfg["max_sdu"][f"queue{q}"] = to_int(m.group(2))
            continue
        m = re.match(r"^tsn tas gate-states queue\s+([\d,\-]+)\s+(open|closed)",
                     line, re.I)
        if m:
            gate_states_seen = True
            for q in _expand_id_list(m.group(1)):
                if m.group(2).lower() == "open":
                    gate_states_bits |= (1 << q)
                else:
                    gate_states_bits &= ~(1 << q)
            continue
        m = re.match(
            r"^tsn tas control-list index\s+(\d+)\s+gate-state queue\s+([\d,\-]+)"
            r"\s+(open|closed)\s+time-interval\s+(\d+)", line, re.I)
        if m:
            idx = int(m.group(1))
            entry = entries.setdefault(idx, {
                "index": idx, "operation": "set-gate-states",
                "gate_state_value": 0,
                "time_interval_ns": to_int(m.group(4)),
            })
            for q in _expand_id_list(m.group(2)):
                if m.group(3).lower() == "open":
                    entry["gate_state_value"] |= (1 << q)
                else:
                    entry["gate_state_value"] &= ~(1 << q)
            entry["time_interval_ns"] = to_int(m.group(4))
            continue

    if gate_states_seen:
        cfg["admin_gate_states"] = caps_mod.decode_gate_states(gate_states_bits)

    for idx in sorted(entries):
        entry = entries[idx]
        entry["gate_states"] = caps_mod.decode_gate_states(
            entry["gate_state_value"])
        cfg["admin_control_list"].append(entry)
    if cfg["admin_control_list_length"] is None and cfg["admin_control_list"]:
        cfg["admin_control_list_length"] = len(cfg["admin_control_list"])
    return cfg


# --- Qbv / Qbu / QoS status ---------------------------------------------

def _tas_status_to_qbv(block: Dict[str, str], config_lines: List[str]) -> dict:
    """`show tsn tas status` + running-config -> the qbv shape."""
    gcl: List[dict] = []
    for key, value in block.items():
        m = re.match(r"^GateControlEntry\s+(\d+)$", key)
        if not m:
            continue
        idx = int(m.group(1))
        states = re.search(r"GateStates\s+(0x[0-9a-fA-F]+|\d+)", value)
        interval = re.search(r"TimeInterval\s+(\d+)", value)
        op = re.search(r"GateOperation\s+([\w-]+)", value)
        mask = _hex_or_int(states.group(1)) if states else None
        gcl.append({
            "index": idx,
            "operation": _GATE_OP_TO_YANG.get(
                (op.group(1) if op else "set").lower(), "set-gate-states"),
            "gate_state_value": mask,
            "gate_states": caps_mod.decode_gate_states(mask),
            "time_interval_ns": to_int(interval.group(1)) if interval else None,
        })
    gcl.sort(key=lambda e: e["index"])

    admin = parse_tas_config_lines(config_lines)
    oper_cycle_ns = _duration_ns(block.get("OperCycleTime"))
    oper_states = _hex_or_int(block.get("OperGateStates"))

    capability = {
        "supported_gcl_entries_max": to_int(block.get("SupportedListMax")),
        # The CLI does not print these two. NETCONF's ieee802-dot1q-sched
        # does (supported-cycle-max, supported-interval-max). Left None
        # rather than guessed -- a CNC must know the difference between
        # "no limit reported" and "no limit".
        "supported_cycle_time_max_ns": None,
        "supported_interval_max_ns": None,
        "supports_cycle_time_extension":
            "OperCycleTimeExtension" in block or
            admin["admin_cycle_time_extension_ns"] is not None,
        "unavailable_over_cli": [
            "supported_cycle_time_max_ns", "supported_interval_max_ns"],
    }

    configuration = {
        "gate_enabled": _flag(block.get("GateEnabled")) if block
                        else admin["gate_enabled"],
        "config_change": None,
        "config_pending": _flag(block.get("ConfigPending")),
        "config_change_error": to_int(block.get("ConfigChangeError")),
        "admin_gate_states": admin["admin_gate_states"],
        "oper_gate_states": caps_mod.decode_gate_states(oper_states),
        "admin_cycle_time_ns": admin["admin_cycle_time_ns"],
        "oper_cycle_time_ns": oper_cycle_ns,
        "admin_cycle_time_extension_ns": admin["admin_cycle_time_extension_ns"],
        "oper_cycle_time_extension_ns": _duration_ns(
            block.get("OperCycleTimeExtension")),
        "admin_base_time_ns": admin["admin_base_time_ns"],
        "oper_base_time_ns": _seconds_nanos_ns(block.get("OperBaseTime")),
        "config_change_time_ns": _seconds_nanos_ns(block.get("ConfigChangeTime")),
        "current_time_ns": _seconds_nanos_ns(block.get("CurrentTime")),
        "tick_granularity": to_int(block.get("TickGranularity")),
        "admin_control_list_length": admin["admin_control_list_length"],
        "oper_control_list_length": to_int(block.get("OperControlListLength")),
        "admin_control_list": admin["admin_control_list"],
        "oper_control_list": gcl,
        "max_sdu": admin["max_sdu"],
    }
    if configuration["gate_enabled"] is None:
        configuration["gate_enabled"] = admin["gate_enabled"]

    # Same consistency checks the NETCONF path applies, on whichever list we
    # actually have.
    effective = gcl or admin["admin_control_list"]
    cycle = oper_cycle_ns or admin["admin_cycle_time_ns"]
    total = sum(e.get("time_interval_ns") or 0 for e in effective)
    warnings, notes = [], []
    sink = warnings if configuration["gate_enabled"] else notes
    if effective and cycle and total != cycle:
        sink.append(f"gate-control-list intervals sum to {total} ns but "
                    f"cycle time is {cycle} ns")
    limit = capability["supported_gcl_entries_max"]
    if limit is not None and len(effective) > limit:
        warnings.append(f"{len(effective)} gate-control entries exceed "
                        f"SupportedListMax {limit}")

    return {
        "present": True,
        "capability": capability,
        "configuration": configuration,
        "gcl_total_ns": total,
        "warnings": warnings,
        "notes": notes,
        "source": "show tsn tas status + show running-config",
    }


def _fp_status_to_qbu(block: Dict[str, str],
                      preemptable_queues: List[int]) -> dict:
    """`show tsn frame-preemption status` + running-config -> the qbu shape."""
    status = {}
    for q in range(8):
        status[f"priority{q}"] = ("preemptable" if q in preemptable_queues
                                  else "express")
    return {
        "present": True,
        "capability": {
            "hold_advance_ns": _duration_ns(block.get("HoldAdvance")),
            "release_advance_ns": _duration_ns(block.get("ReleaseAdvance")),
            "loc_preempt_supported": _flag(block.get("LocPreemptSupported")),
            "add_frag_size": to_int((block.get("LocAddFragSize") or "").split()[0]
                                    if block.get("LocAddFragSize") else None),
        },
        "configuration": {
            "frame_preemption_status": status,
            "preemptable_priorities": [f"priority{q}"
                                       for q in sorted(preemptable_queues)],
            "express_priorities": [f"priority{q}" for q in range(8)
                                   if q not in preemptable_queues],
            "preemptable_queues": sorted(preemptable_queues),
            "preemption_active": _flag(block.get("PreemptionActive")),
            "hold_request": block.get("HoldRequest"),
            "status_verify": block.get("StatusVerify"),
            "loc_preempt_enabled": _flag(block.get("LocPreemptEnabled")),
            "loc_preempt_active": _flag(block.get("LocPreemptActive")),
        },
        "source": "show tsn frame-preemption status + show running-config",
        "mapping_note":
            "The CLI expresses preemption per egress queue; "
            "ieee802-dot1q-preemption expresses it per priority. Queue N is "
            "reported as priorityN, which holds under the default 1:1 "
            "priority-to-traffic-class map. Verify the map before relying on "
            "this if priority regeneration has been changed.",
    }


_QOS_SHAPER_RE = re.compile(
    r"^qos queue-shaper queue\s+(\d+):\s*(\w+),\s*rate\s+(\d+)\s*(\w+),"
    r"\s*mode:\s*([\w-]+),\s*excess:\s*(\w+),\s*credit:\s*(\w+)", re.I)


def _parse_qos(text: Optional[str]) -> dict:
    out: dict = {"nodes_present": [], "values": {}}
    shapers = {}
    for line in _clean_lines(text):
        m = _QOS_SHAPER_RE.match(line.strip())
        if m:
            shapers[f"queue{m.group(1)}"] = {
                "enabled": m.group(2).lower() == "enabled",
                "rate": to_int(m.group(3)),
                "rate_unit": m.group(4),
                "mode": m.group(5),
                "excess": m.group(6).lower() == "enabled",
                "credit": m.group(7).lower() == "enabled",
            }
    if shapers:
        out["nodes_present"].append("queue-shaper")
        out["values"]["queue-shaper"] = shapers
        # A credit-enabled shaper is 802.1Qav (CBS) in all but name.
        if any(s["credit"] and s["enabled"] for s in shapers.values()):
            out["credit_based_shaper_active"] = True
    return out


# --- LLDP ----------------------------------------------------------------

_LLDP_FIELD_MAP = {
    "local interface": "local_port",
    "chassis id": "chassis_id",
    "port id": "port_id",
    "port description": "port_desc",
    "system name": "system_name",
    "system description": "system_description",
    "system capabilities": "system_capabilities",
    "management address": "management_address_raw",
}


def parse_lldp_neighbors(text: Optional[str]) -> List[dict]:
    """`show lldp neighbors` -> the neighbour dicts topology.py consumes.

    Output is one ``key : value`` block per neighbour, blocks separated by a
    blank line, and a new block always begins with ``Local Interface``.
    """
    neighbours: List[dict] = []
    current: Dict[str, str] = {}

    def flush():
        if not current:
            return
        local = canonical_ifname(current.get("local_port"))
        chassis = current.get("chassis_id")
        port_desc = canonical_ifname(current.get("port_desc"))
        mgmt = []
        raw = current.get("management_address_raw") or ""
        m = re.match(r"\s*(\S+)\s*(?:\(([^)]*)\))?", raw)
        if m and m.group(1):
            mgmt.append({"address": m.group(1),
                         "subtype": (m.group(2) or "").lower() or None})
        neighbours.append({
            "local_port": local,
            "remote_index": len(neighbours) + 1,
            "chassis_id": chassis,
            "chassis_id_mac": norm_mac(chassis),
            "chassis_id_subtype": "mac-address" if norm_mac(chassis) else None,
            "port_id": current.get("port_id"),
            "port_id_mac": norm_mac(current.get("port_id")),
            "port_id_subtype": "interface-name" if port_desc else "local",
            # Prefer the description: it is the interface name, while Port ID
            # on this firmware is the bare port number ("4").
            "port_desc": port_desc or current.get("port_id"),
            "system_name": current.get("system_name"),
            "system_description": current.get("system_description"),
            "system_capabilities": current.get("system_capabilities"),
            "management_addresses": mgmt,
            "source": "show lldp neighbors",
        })

    for line in _clean_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        kv = re.match(r"^([A-Za-z][\w \-]*?)\s*:\s*(.*)$", stripped)
        if not kv:
            continue
        key = kv.group(1).strip().lower()
        value = kv.group(2).strip()
        field = _LLDP_FIELD_MAP.get(key)
        if field == "local_port" and current:
            flush()
            current = {}
        if field:
            current[field] = value
    flush()
    return [n for n in neighbours if n.get("local_port")]


# --- MAC address table ---------------------------------------------------

def parse_mac_table(text: Optional[str],
                    port_number_of: Dict[str, int]) -> List[dict]:
    """`show mac address-table` -> 802.1Q filtering-entry shape.

    ``CPU`` entries are kept but carry an empty ``port_map``: the switch's
    own addresses are real filtering-database content, and dropping them
    would lose the bridge's base MAC, but they are not reachable through a
    bridge port and must never produce an adjacency.
    """
    rows = parse_table(text)
    out: List[dict] = []
    for row in rows:
        address = norm_mac(row.get("MAC Address"))
        if not address:
            continue
        ports = expand_port_list(row.get("Ports"))
        out.append({
            "database_id": 1,
            "vids": row.get("VID"),
            "address": address,
            "address_raw": (row.get("MAC Address") or "").strip(),
            "entry_type": (row.get("Type") or "").strip().lower() or None,
            "status": "learned" if (row.get("Type") or "").lower() == "dynamic"
                      else "permanent",
            "port_map": [{"port_ref": port_number_of[p], "port_name": p}
                         for p in ports if p in port_number_of],
            "cpu": "CPU" in (row.get("Ports") or ""),
            "source": "show mac address-table",
        })
    return out


def parse_vlans(text: Optional[str]) -> List[dict]:
    """`show vlan` -> the bridge-vlan list shape."""
    out: List[dict] = []
    for row in parse_table(text):
        vid = to_int(row.get("VLAN"))
        if vid is None:
            continue
        ports = expand_port_list(row.get("Interfaces"))
        out.append({
            "vid": vid,
            "name": (row.get("Name") or "").strip() or None,
            "egress_ports": ports,
            "untagged_ports": [],
            "source": "show vlan",
        })
    out.sort(key=lambda v: v["vid"])
    return out
