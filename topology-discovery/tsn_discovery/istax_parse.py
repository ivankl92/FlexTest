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
    # A column's territory runs from its own start to the *next* column's
    # start, not to the end of its dash group. The firmware sizes the dashes
    # to the heading, not to the data, and overflows the difference into the
    # gap: `show ptp 0 parent` gives ParentPortIdentity 22 dashes and then
    # writes a 23-character clock identity, so slicing to the dash width
    # silently drops the last character of every clock identity.
    ends: List[Optional[int]] = []
    for n, (start, end) in enumerate(spans):
        if n + 1 < len(spans):
            ends.append(max(end or 0, spans[n + 1][0]))
        else:
            ends.append(None)                     # last column runs on

    headers = []
    for (start, _), end in zip(spans, ends):
        headers.append(header_line[start:end].strip() if end
                       else header_line[start:].strip())

    def slice_one(line: str) -> Dict[str, str]:
        row = {}
        for (start, _), end, key in zip(spans, ends, headers):
            value = line[start:end] if end else line[start:]
            row[key or f"col{start}"] = value.strip()
        return row

    rows: List[Dict[str, str]] = []
    for line in data_lines:
        if not line.strip():
            continue
        if set(line.strip()) <= set("- "):
            break
        row = slice_one(line)

        # Pager residue: the firmware erases its `-- more --` prompt with a
        # run of spaces and then writes the next row on that same line, so
        # one row per page arrives pushed ~50 columns to the right. It is
        # recognised by geometry rather than by counting spaces -- a row
        # whose content begins past the LAST column's start, and which
        # re-aligns into two or more columns once the run is removed, was
        # displaced. Indentation alone would not do: `show ptp 0 current`
        # right-aligns a genuine value 31 columns in.
        if len(spans) > 1 and line[:spans[-1][0]].strip() == "":
            candidate = line.lstrip()
            if candidate:
                realigned = slice_one(candidate)
                if sum(1 for v in realigned.values() if v) > \
                        sum(1 for v in row.values() if v):
                    row = realigned

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
        "mst_region_name": None,
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
        # `spanning-tree mst name 00-80-82-b9-65-33` LOOKS like the bridge
        # base MAC, and on a switch nobody has touched it is one -- the
        # firmware seeds the MST region name from it. It is still only a
        # region name: four of the five switches in this testbed carry
        # `00-22-33-44-55-66`, which is not any of their addresses. So it is
        # recorded as what it is, and the bridge address is taken from the
        # static CPU entry in the filtering database instead.
        m = re.match(r"^spanning-tree mst name\s+(\S+)", stripped, re.I)
        if m:
            cfg["mst_region_name"] = m.group(1)
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

def _tick_granularity(value: Optional[str]) -> Optional[int]:
    """`TickGranularity : 1 tenths of nanoseconds`.

    802.1Q's tick-granularity leaf is already expressed in tenths of a
    nanosecond, so when the firmware names that unit the leading integer is
    the value. Any other unit is left unread rather than silently rescaled.
    """
    if not value:
        return None
    m = re.match(r"^\s*(\d+)\s*(.*)$", value)
    if not m:
        return None
    unit = m.group(2).strip().lower()
    if not unit or unit.startswith("tenths of nanosecond"):
        return to_int(m.group(1))
    return None


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
        # "1 tenths of nanoseconds" -- the number and its unit share the
        # field, so to_int() on the whole string yields nothing. 802.1Q's
        # tick-granularity is in tenths of a nanosecond, which is the unit
        # the firmware is already printing, so the leading integer is the
        # value; anything else is left unread rather than converted blind.
        "tick_granularity": _tick_granularity(block.get("TickGranularity")),
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
                      preemptable_queues: List[int],
                      priority_regeneration: Optional[Dict[str, int]] = None
                      ) -> dict:
    """`show tsn frame-preemption status` + running-config -> the qbu shape.

    The CLI configures preemption per egress QUEUE; `ieee802-dot1q-preemption`
    expresses it per PRIORITY. Those are the same thing only when the port's
    priority regeneration map is the identity, and on this hardware it is
    not: the 802.1Q-recommended default swaps PCP 0 and PCP 1. So the map is
    read from `show qos interface` and applied, rather than assumed.
    """
    queue_of_priority = {p: p for p in range(8)}
    mapping_is_identity = True
    if priority_regeneration:
        mapping_is_identity = all(int(p) == q for p, q
                                  in priority_regeneration.items())
        for p, q in priority_regeneration.items():
            queue_of_priority[int(p)] = q

    status = {}
    for p in range(8):
        status[f"priority{p}"] = (
            "preemptable" if queue_of_priority[p] in preemptable_queues
            else "express")
    preemptable_priorities = [f"priority{p}" for p in range(8)
                              if queue_of_priority[p] in preemptable_queues]

    if priority_regeneration is None:
        note = ("Preemption is configured per egress queue and reported here "
                "per priority. The port's priority regeneration map was not "
                "read, so queue N is reported as priorityN -- correct only if "
                "the map is the identity. Read `show qos interface` to "
                "confirm.")
    elif mapping_is_identity:
        note = ("Preemption is configured per egress queue and reported here "
                "per priority, using this port's priority regeneration map "
                "from `show qos interface`, which is the identity.")
    else:
        swapped = ", ".join(f"priority {p} -> queue {q}" for p, q
                            in sorted(((int(k), v) for k, v
                                       in priority_regeneration.items()))
                            if int(p) != q)
        note = ("Preemption is configured per egress queue and reported here "
                "per priority, using this port's priority regeneration map "
                f"from `show qos interface`, which is NOT the identity "
                f"({swapped}). Reading queue N as priority N would report "
                "these priorities wrongly.")

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
            "preemptable_priorities": preemptable_priorities,
            "express_priorities": [f"priority{p}" for p in range(8)
                                   if f"priority{p}" not in
                                   preemptable_priorities],
            "preemptable_queues": sorted(preemptable_queues),
            "priority_regeneration_applied": priority_regeneration,
            "priority_regeneration_is_identity": mapping_is_identity,
            "preemption_active": _flag(block.get("PreemptionActive")),
            "hold_request": block.get("HoldRequest"),
            "status_verify": block.get("StatusVerify"),
            "loc_preempt_enabled": _flag(block.get("LocPreemptEnabled")),
            "loc_preempt_active": _flag(block.get("LocPreemptActive")),
        },
        "source": "show tsn frame-preemption status + show running-config"
                  + (" + show qos interface" if priority_regeneration else ""),
        "mapping_note": note,
    }


_QOS_QUEUE_SHAPER_RE = re.compile(
    r"^qos queue-shaper queue\s+(\d+):\s*(\w+),\s*rate:?\s+(\d+)\s*(\w+),"
    r"\s*mode:\s*([\w-]+),\s*excess:\s*(\w+),\s*credit:\s*(\w+)", re.I)
_QOS_PORT_SHAPER_RE = re.compile(
    r"^qos port shaper:\s*(\w+),\s*rate:?\s+(\d+)\s*(\w+),"
    r"\s*mode:\s*([\w-]+)", re.I)
_QOS_QUEUE_POLICER_RE = re.compile(
    r"^qos queue-policer queue\s+(\d+)\s+mode:\s*(\w+),\s*rate:?\s+"
    r"(\d+)\s*(\w+)", re.I)
_QOS_POLICER_RE = re.compile(
    r"^qos policer mode:\s*(\w+),\s*rate:?\s+(\d+)\s*(\w+)", re.I)
_QOS_TAG_COS_RE = re.compile(
    r"^qos map tag-cos pcp\s+(\d+)\s+dei\s+(\d+)\s+cos\s+(\d+)"
    r"\s+dpl\s+(\d+)", re.I)
_QOS_COS_TAG_RE = re.compile(
    r"^qos map cos-tag cos\s+(\d+)\s+dpl\s+(\d+)\s+pcp\s+(\d+)"
    r"\s+dei\s+(\d+)", re.I)
_QOS_CUT_THROUGH_RE = re.compile(
    r"^qos cut-through queue\s+(\d+):\s*(\w+)", re.I)
_QOS_SCALAR_RE = re.compile(
    r"^qos (cos|pcp|dpl|dei)\s+(\d+)$", re.I)
_QOS_TRUST_RE = re.compile(r"^qos trust (tag|dscp)\s+(\w+)", re.I)
_QOS_SIMPLE_RE = re.compile(
    r"^qos (dscp-translate|dscp-classify|dscp-remark|tag-remark|wrr mode:|"
    r"qce addr|qce key)\s*:?\s*(.+)$", re.I)

_RATE_TO_KBPS = {"kbps": 1, "mbps": 1000, "gbps": 1000000}


def _rate_kbps(value: Optional[int], unit: Optional[str]) -> Optional[int]:
    if value is None or not unit:
        return None
    factor = _RATE_TO_KBPS.get(unit.lower())
    return value * factor if factor else None


def _parse_qos(text: Optional[str]) -> dict:
    """`show qos interface <if>` in full.

    The command prints the port's whole QoS configuration: the ingress
    classification path, the egress remarking path, policers, shapers,
    scheduling and cut-through. An earlier version read only the queue
    shapers, which meant the record could not answer the one question a CNC
    has to ask -- which traffic class does a tagged frame land in -- and
    silently hid that ingress tags are not trusted on any port here.

    Where 802.1Q defines a node the value goes under that name; the rest
    keeps its ISTAX name under `vendor`, because inventing a standard-looking
    home for a vendor knob is worse than admitting it is one.
    """
    out: dict = {"nodes_present": [], "values": {}}
    classification: dict = {}
    remark: dict = {}
    pcp_to_cos: Dict[str, dict] = {}
    cos_to_pcp: Dict[str, dict] = {}
    queue_shapers: Dict[str, dict] = {}
    queue_policers: Dict[str, dict] = {}
    cut_through: Dict[str, bool] = {}
    vendor: dict = {}

    for raw in _clean_lines(text):
        line = raw.strip()
        if not line.lower().startswith("qos "):
            continue

        m = _QOS_SCALAR_RE.match(line)
        if m:
            classification[f"default_{m.group(1).lower()}"] = to_int(m.group(2))
            continue

        m = _QOS_TRUST_RE.match(line)
        if m:
            classification[f"trust_{m.group(1).lower()}"] = \
                m.group(2).lower() == "enabled"
            continue

        m = _QOS_TAG_COS_RE.match(line)
        if m:
            pcp, dei, cos, dpl = (to_int(g) for g in m.groups())
            pcp_to_cos[f"pcp{pcp}-dei{dei}"] = {
                "pcp": pcp, "dei": dei,
                "traffic_class": cos, "drop_precedence": dpl}
            continue

        m = _QOS_COS_TAG_RE.match(line)
        if m:
            cos, dpl, pcp, dei = (to_int(g) for g in m.groups())
            cos_to_pcp[f"cos{cos}-dpl{dpl}"] = {
                "traffic_class": cos, "drop_precedence": dpl,
                "pcp": pcp, "dei": dei}
            continue

        m = _QOS_QUEUE_SHAPER_RE.match(line)
        if m:
            queue_shapers[f"queue{m.group(1)}"] = {
                "enabled": m.group(2).lower() == "enabled",
                "rate": to_int(m.group(3)),
                "rate_unit": m.group(4),
                "rate_kbps": _rate_kbps(to_int(m.group(3)), m.group(4)),
                "mode": m.group(5),
                "excess": m.group(6).lower() == "enabled",
                "credit": m.group(7).lower() == "enabled",
            }
            continue

        m = _QOS_PORT_SHAPER_RE.match(line)
        if m:
            out["values"]["port-shaper"] = {
                "enabled": m.group(1).lower() == "enabled",
                "rate": to_int(m.group(2)),
                "rate_unit": m.group(3),
                "rate_kbps": _rate_kbps(to_int(m.group(2)), m.group(3)),
                "mode": m.group(4),
            }
            out["nodes_present"].append("port-shaper")
            continue

        m = _QOS_QUEUE_POLICER_RE.match(line)
        if m:
            queue_policers[f"queue{m.group(1)}"] = {
                "enabled": m.group(2).lower() != "disabled",
                "mode": m.group(2),
                "rate": to_int(m.group(3)),
                "rate_unit": m.group(4),
                "rate_kbps": _rate_kbps(to_int(m.group(3)), m.group(4)),
            }
            continue

        m = _QOS_POLICER_RE.match(line)
        if m:
            out["values"]["port-policer"] = {
                "enabled": m.group(1).lower() != "disabled",
                "mode": m.group(1),
                "rate": to_int(m.group(2)),
                "rate_unit": m.group(3),
                "rate_kbps": _rate_kbps(to_int(m.group(2)), m.group(3)),
            }
            out["nodes_present"].append("port-policer")
            continue

        m = _QOS_CUT_THROUGH_RE.match(line)
        if m:
            cut_through[f"queue{m.group(1)}"] = \
                m.group(2).lower() == "enabled"
            continue

        m = _QOS_SIMPLE_RE.match(line)
        if m:
            key = m.group(1).strip().rstrip(":").replace(" ", "-").lower()
            value = m.group(2).strip()
            if key == "tag-remark":
                remark["mode"] = value
            else:
                vendor[key] = value
            continue

        vendor.setdefault("unparsed", []).append(line)

    if classification:
        # 802.1Q calls this the priority regeneration / PCP decoding table.
        # ISTAX calls the result "cos"; it is the traffic class.
        out["values"]["ingress-classification"] = classification
        out["nodes_present"].append("ingress-classification")
    if pcp_to_cos:
        out["values"]["pcp-decoding-map"] = pcp_to_cos
        out["nodes_present"].append("pcp-decoding-map")
        out["values"]["priority_regeneration"] = {
            str(e["pcp"]): e["traffic_class"]
            for e in pcp_to_cos.values() if e["dei"] == 0}
        identity = all(int(k) == v for k, v in
                       out["values"]["priority_regeneration"].items())
        out["values"]["priority_regeneration_is_identity"] = identity
    if cos_to_pcp:
        out["values"]["pcp-encoding-map"] = cos_to_pcp
        out["nodes_present"].append("pcp-encoding-map")
    if remark:
        out["values"]["egress-tag-remark"] = remark
        out["nodes_present"].append("egress-tag-remark")
    if queue_shapers:
        out["nodes_present"].append("queue-shaper")
        out["values"]["queue-shaper"] = queue_shapers
        # A credit-enabled shaper is 802.1Qav (CBS) in all but name.
        if any(s["credit"] and s["enabled"] for s in queue_shapers.values()):
            out["credit_based_shaper_active"] = True
    if queue_policers:
        out["nodes_present"].append("queue-policer")
        out["values"]["queue-policer"] = queue_policers
    if cut_through:
        out["nodes_present"].append("cut-through")
        out["values"]["cut-through"] = cut_through
    if vendor:
        out["values"]["vendor"] = vendor
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
