"""PTP / gPTP, read over the ISTAX CLI and shaped like RFC 8575.

This module closes the gap that REPORT §4 identified as
`blocking-for-verification`: the KSwitch's NETCONF server implements no PTP
or gPTP YANG module, in AN001 v1.2 and still in v1.3, so a CNC reading only
NETCONF cannot see the time base its Qbv schedules are anchored to. The CLI
can see it, and this module maps what it prints onto the node names of
**ietf-ptp (RFC 8575)** -- `default-ds`, `current-ds`, `parent-ds`,
`time-properties-ds`, `port-ds-list`.

Shaping it as a standard model rather than inventing field names is the
whole point. It keeps the discovery output uniform whatever the transport,
it gives a CNC one place to look for the time base, and if Kontron ever
ships a PTP YANG module the data will already be in its shape, so only the
collector changes and nothing downstream does.

Source formats: Microchip AN1295 (PTP Configuration Guide) §7, and the
`ptp 0 ...` lines of `show running-config`.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .istax import canonical_ifname
from .istax_parse import _clean_lines, parse_tables
from .xmlutil import to_int

NS_PER_SECOND = 1_000_000_000

# AN1295 prints abbreviated port states; RFC 8575's port-state enumeration
# is spelled out. Anything unrecognised is passed through verbatim rather
# than forced into the enum.
PORT_STATE_MAP = {
    "init": "initializing", "initializing": "initializing",
    "flty": "faulty", "faulty": "faulty",
    "dsbl": "disabled", "disabled": "disabled",
    "lstn": "listening", "listening": "listening",
    "pmst": "pre-master", "pre-master": "pre-master",
    "mstr": "master", "master": "master",
    "pasv": "passive", "passive": "passive",
    "uncl": "uncalibrated", "uncalibrated": "uncalibrated",
    "slve": "slave", "slave": "slave",
}


def _seconds_to_ns(value: Optional[str]) -> Optional[float]:
    """"-0.000,000,000,386" -> -0.386 (nanoseconds).

    AN1295 prints offsets as seconds with comma-grouped fractional digits.
    Returned in nanoseconds because every other time value in this project
    is in nanoseconds, and a CNC comparing a PTP offset against a Qbv
    interval should not have to convert units first.
    """
    if value is None:
        return None
    v = str(value).replace(",", "").strip()
    if not v or v in ("-", "N/A"):
        return None
    try:
        return float(v) * NS_PER_SECOND
    except ValueError:
        return None


def _flag(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("true", "yes", "1", "enabled"):
        return True
    if v in ("false", "no", "0", "disabled"):
        return False
    return None


def _clock_quality(value: Optional[str]) -> Optional[dict]:
    """"Cl:248 Ac:Unknwn Va:00000" -> the RFC 8575 clock-quality grouping."""
    if not value:
        return None
    cl = re.search(r"Cl:\s*(\d+)", value)
    ac = re.search(r"Ac:\s*(\S+)", value)
    va = re.search(r"Va:\s*(\w+)", value)
    if not (cl or ac or va):
        return None
    return {
        "clock-class": to_int(cl.group(1)) if cl else None,
        "clock-accuracy": ac.group(1) if ac else None,
        "offset-scaled-log-variance": to_int(va.group(1)) if va else None,
        "raw": value.strip(),
    }


def _row_get(row: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        for key, value in row.items():
            if key.strip().lower() == n.lower():
                return value
    return None


# --- individual datasets -------------------------------------------------

def parse_default_ds(text: Optional[str]) -> dict:
    """`show ptp 0 default` -> RFC 8575 default-ds."""
    tables = parse_tables(text)
    ds: dict = {}
    for rows in tables:
        for row in rows:
            ident = _row_get(row, "vtss_appl_clock_identity", "ClockIdentity")
            if ident:
                ds["clock-identity"] = ident.strip()
            if _row_get(row, "DeviceType"):
                ds["device-type"] = _row_get(row, "DeviceType")
            if _row_get(row, "Profile"):
                ds["profile"] = _row_get(row, "Profile")
            if _row_get(row, "2StepFlag"):
                ds["two-step-flag"] = _flag(_row_get(row, "2StepFlag"))
            if _row_get(row, "Ports"):
                ds["number-ports"] = to_int(_row_get(row, "Ports"))
            quality = _row_get(row, "vtss_appl_clock_quality",
                               "ClockQuality")
            if quality:
                ds["clock-quality"] = _clock_quality(quality)
            if _row_get(row, "Pri1"):
                ds["priority1"] = to_int(_row_get(row, "Pri1"))
            if _row_get(row, "Pri2"):
                ds["priority2"] = to_int(_row_get(row, "Pri2"))
            if _row_get(row, "Dom"):
                ds["domain-number"] = to_int(_row_get(row, "Dom"))
            if _row_get(row, "Protocol"):
                ds["transport-protocol"] = _row_get(row, "Protocol")
            if _row_get(row, "One-Way"):
                ds["one-way"] = _flag(_row_get(row, "One-Way"))
            if _row_get(row, "VID"):
                ds["vlan-id"] = to_int(_row_get(row, "VID"))
            if _row_get(row, "PCP"):
                ds["priority-code-point"] = to_int(_row_get(row, "PCP"))
    return ds


def parse_current_ds(text: Optional[str]) -> dict:
    """`show ptp 0 current` -> RFC 8575 current-ds.

    This is the dataset that matters most for the testbed: it carries the
    offset from the grandmaster, which is what says whether a Qbv schedule's
    base time means anything.
    """
    ds: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            steps = _row_get(row, "stpRm", "StepsRemoved")
            if steps is not None:
                ds["steps-removed"] = to_int(steps)
            offset = _row_get(row, "OffsetFromMaster")
            if offset is not None:
                ds["offset-from-master-ns"] = _seconds_to_ns(offset)
                ds["offset-from-master-raw"] = offset.strip()
            delay = _row_get(row, "MeanPathDelay")
            if delay is not None:
                ds["mean-path-delay-ns"] = _seconds_to_ns(delay)
                ds["mean-path-delay-raw"] = delay.strip()
    return ds


def parse_parent_ds(text: Optional[str]) -> dict:
    """`show ptp 0 parent` -> RFC 8575 parent-ds."""
    ds: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            if _row_get(row, "ParentPortIdentity"):
                ds["parent-port-identity"] = _row_get(row, "ParentPortIdentity")
            if _row_get(row, "port"):
                ds["parent-port-number"] = to_int(_row_get(row, "port"))
            if _row_get(row, "GrandmasterIdentity"):
                ds["grandmaster-identity"] = _row_get(row, "GrandmasterIdentity")
            gq = _row_get(row, "GrandmasterClockQuality")
            if gq:
                ds["grandmaster-clock-quality"] = _clock_quality(gq)
            if _row_get(row, "Pri1"):
                ds["grandmaster-priority1"] = to_int(_row_get(row, "Pri1"))
            if _row_get(row, "Pri2"):
                ds["grandmaster-priority2"] = to_int(_row_get(row, "Pri2"))
            if _row_get(row, "Var"):
                ds["observed-parent-offset-scaled-log-variance"] = to_int(
                    _row_get(row, "Var"))
            if _row_get(row, "ChangeRate"):
                ds["observed-parent-clock-phase-change-rate"] = to_int(
                    _row_get(row, "ChangeRate"))
    return ds


def parse_time_properties_ds(text: Optional[str]) -> dict:
    """`show ptp 0 time-property` -> RFC 8575 time-properties-ds."""
    ds: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            mapping = {
                "UtcOffset": ("current-utc-offset", to_int),
                "Valid": ("current-utc-offset-valid", _flag),
                "leap59": ("leap59", _flag),
                "leap61": ("leap61", _flag),
                "TimeTrac": ("time-traceable", _flag),
                "FreqTrac": ("frequency-traceable", _flag),
                "ptpTimeScale": ("ptp-timescale", _flag),
                "TimeSource": ("time-source", to_int),
            }
            for cli_key, (yang_key, conv) in mapping.items():
                raw = _row_get(row, cli_key)
                if raw is not None:
                    ds[yang_key] = conv(raw)
    return ds


def parse_port_ds_list(text: Optional[str],
                       interface: Optional[str] = None) -> List[dict]:
    """`show ptp 0 port-state` -> RFC 8575 port-ds-list entries.

    The command prints a physical-port table and then a virtual-port table.
    Only the first is a PTP port in the RFC 8575 sense; virtual ports are
    kept separately so they cannot be mistaken for network ports.
    """
    out: List[dict] = []
    for rows in parse_tables(text):
        for row in rows:
            if _row_get(row, "VirtualPort") is not None:
                continue
            port = _row_get(row, "Port")
            if port is None:
                continue
            state = (_row_get(row, "PTP-State") or "").strip().lower()
            entry = {
                "port-number": to_int(port),
                "port-state": PORT_STATE_MAP.get(state, state or None),
                "port-state-raw": state or None,
                "enabled": _flag(_row_get(row, "Enabled")),
                "internal": _flag(_row_get(row, "Internal")),
                "link": _row_get(row, "Link"),
                "port-timer": _row_get(row, "Port-Timer"),
                "vlan-forward": _row_get(row, "Vlan-forw"),
                "phy-timestamper": _flag(_row_get(row, "Phy-timestamper")),
                "peer-delay": _row_get(row, "Peer-delay"),
            }
            if interface:
                entry["interface"] = canonical_ifname(interface)
            out.append(entry)
    return out


def parse_slave(text: Optional[str]) -> dict:
    """`show ptp 0 slave` -> servo lock state.

    Not an RFC 8575 dataset -- it is vendor servo status -- so it is kept
    under its own key rather than smuggled into a standard one.
    """
    ds: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            if _row_get(row, "Slave port") is not None:
                ds["slave-port"] = to_int(_row_get(row, "Slave port"))
                ds["slave-state"] = _row_get(row, "Slave state")
                holdover = _row_get(row, "Holdover(ppb)", "Holdover")
                try:
                    ds["holdover-ppb"] = (float(holdover)
                                          if holdover not in (None, "") else None)
                except ValueError:
                    ds["holdover-ppb"] = None
    return ds


# --- running-config ------------------------------------------------------

def parse_ptp_config(global_lines: List[str],
                     per_interface: Dict[str, List[str]]) -> dict:
    """The `ptp 0 ...` lines of `show running-config`.

    Example from KSwitchTSN-1:
        ptp 0 mode boundary twostep ethernet twoway vid 1 6 profile 802.1as mep 1
    """
    cfg: dict = {"instances": {}, "interfaces": {}, "raw": list(global_lines)}

    for line in global_lines:
        m = re.match(r"^ptp\s+(\d+)\s+(.*)$", line.strip(), re.I)
        if not m:
            continue
        inst = int(m.group(1))
        rest = m.group(2)
        entry = cfg["instances"].setdefault(inst, {"instance-number": inst})
        mode = re.match(r"^mode\s+(\S+)", rest, re.I)
        if mode:
            entry["clock-mode"] = mode.group(1).lower()
            entry["two-step-flag"] = bool(re.search(r"\btwostep\b", rest, re.I))
            entry["transport-protocol"] = (
                "ethernet" if re.search(r"\bethernet\b", rest, re.I)
                else "ipv4" if re.search(r"\bipv4\b", rest, re.I) else None)
            entry["one-way"] = not bool(re.search(r"\btwoway\b", rest, re.I))
            prof = re.search(r"profile\s+(\S+)", rest, re.I)
            if prof:
                entry["profile"] = prof.group(1)
                # 802.1as is gPTP: the profile the testbed's latency
                # measurement depends on.
                entry["is_gptp"] = prof.group(1).lower() in (
                    "802.1as", "8021as", "g8275.1-gptp")
            vid = re.search(r"\bvid\s+(\d+)(?:\s+(\d+))?", rest, re.I)
            if vid:
                entry["vlan-id"] = to_int(vid.group(1))
                if vid.group(2):
                    entry["priority-code-point"] = to_int(vid.group(2))
            continue
        for key, yang in (("filter-type", "filter-type"),
                          ("source-time-inaccuracy", "source-time-inaccuracy"),
                          ("gm-time-inaccuracy", "gm-time-inaccuracy"),
                          ("dist-time-inaccuracy", "dist-time-inaccuracy")):
            m2 = re.match(rf"^{key}\s+(\S+)", rest, re.I)
            if m2:
                entry[yang] = m2.group(1)

    for iface, lines in per_interface.items():
        port: dict = {}
        for line in lines:
            m = re.match(r"^ptp\s+(\d+)\s*(.*)$", line.strip(), re.I)
            if not m:
                continue
            port.setdefault("instance-number", int(m.group(1)))
            rest = m.group(2).strip()
            if not rest:
                port["enabled"] = True
                continue
            for pattern, key, conv in (
                (r"^announce interval\s+(-?\d+)\s+timeout\s+(\d+)",
                 "log-announce-interval", None),
                (r"^sync-interval\s+(-?\d+)", "log-sync-interval", to_int),
                (r"^delay-mechanism\s+(\S+)", "delay-mechanism", str),
                (r"^delay-req interval\s+(-?\d+)",
                 "log-min-delay-req-interval", to_int),
                (r"^delay-asymmetry\s+(-?\d+)", "delay-asymmetry", to_int),
                (r"^ingress-latency\s+(-?\d+)", "ingress-latency", to_int),
                (r"^egress-latency\s+(-?\d+)", "egress-latency", to_int),
                (r"^gptp-interval\s+(-?\d+)", "log-gptp-interval", to_int),
                (r"^mcast-dest\s+(\S+)", "mcast-dest", str),
            ):
                m2 = re.match(pattern, rest, re.I)
                if not m2:
                    continue
                if key == "log-announce-interval":
                    port["log-announce-interval"] = to_int(m2.group(1))
                    port["announce-receipt-timeout"] = to_int(m2.group(2))
                else:
                    port[key] = conv(m2.group(1)) if conv else m2.group(1)
                break
        if port:
            # p2p delay mechanism is what 802.1AS requires; surfacing it
            # named means a CNC can check the profile is actually gPTP.
            cfg["interfaces"][canonical_ifname(iface)] = port
    return cfg


# --- assembly ------------------------------------------------------------

def build(result, running_cfg: dict, interfaces: List[dict]) -> dict:
    """Assemble the PTP record for one CLI-read switch."""
    per_iface_ptp = {i["name"]: i.get("_ptp_config_lines", [])
                     for i in interfaces if i.get("_ptp_config_lines")}
    config = parse_ptp_config(running_cfg.get("ptp_global", []), per_iface_ptp)

    default_ds = parse_default_ds(result.text_of("ptp-default"))
    current_ds = parse_current_ds(result.text_of("ptp-current"))
    parent_ds = parse_parent_ds(result.text_of("ptp-parent"))
    props_ds = parse_time_properties_ds(result.text_of("ptp-time-property"))
    slave = parse_slave(result.text_of("ptp-slave"))

    port_ds: List[dict] = []
    global_ports = result.text_of("ptp-port-state")
    if global_ports:
        port_ds.extend(parse_port_ds_list(global_ports))
    for iface in interfaces:
        slug = iface["name"].replace(" ", "_").replace("/", "-")
        text = result.text_of(f"ptp-port-state--{slug}")
        if text:
            port_ds.extend(parse_port_ds_list(text, interface=iface["name"]))

    configured = bool(config.get("instances"))
    has_state = bool(default_ds or current_ds or parent_ds or port_ds)

    instances = []
    for number, inst_cfg in sorted(config.get("instances", {}).items()):
        instances.append({
            "instance-number": number,
            "default-ds": {**inst_cfg, **default_ds},
            "current-ds": current_ds,
            "parent-ds": parent_ds,
            "time-properties-ds": props_ds,
            "port-ds-list": port_ds,
            "servo": slave or None,
        })
    if not instances and has_state:
        instances.append({
            "instance-number": 0,
            "default-ds": default_ds,
            "current-ds": current_ds,
            "parent-ds": parent_ds,
            "time-properties-ds": props_ds,
            "port-ds-list": port_ds,
            "servo": slave or None,
        })

    gptp = any(i.get("default-ds", {}).get("is_gptp") for i in instances)
    offset_ns = current_ds.get("offset-from-master-ns")

    if not (configured or has_state):
        return {
            "status": "not-configured",
            "source": "cli",
            "model": "ietf-ptp (RFC 8575) shape",
            "detail": "No PTP instance is configured and no PTP state was "
                      "returned by the CLI on this switch.",
            "instances": [],
            "evidence": "show ptp 0 default / current / parent returned nothing "
                        "and show running-config contains no 'ptp' lines",
        }

    return {
        "status": "available-via-cli",
        "source": "cli",
        "model": "ietf-ptp (RFC 8575) shape, populated from ISTAX CLI output",
        "detail": "PTP is not exposed by this switch's NETCONF server (no PTP "
                  "YANG module in AN001 v1.2 or v1.3). These values come from "
                  "the CLI and are mapped onto RFC 8575 node names so that "
                  "the record is uniform across transports.",
        "gptp_profile": gptp,
        "instances": instances,
        "interface_config": config.get("interfaces", {}),
        "sync_summary": {
            "offset_from_master_ns": offset_ns,
            "mean_path_delay_ns": current_ds.get("mean-path-delay-ns"),
            "steps_removed": current_ds.get("steps-removed"),
            "servo_state": (slave or {}).get("slave-state"),
            "grandmaster_identity": parent_ds.get("grandmaster-identity"),
            "port_states": {p.get("interface") or p.get("port-number"):
                            p.get("port-state") for p in port_ds},
        },
        "evidence": "show ptp 0 default / current / parent / time-property / "
                    "port-state / slave, plus 'ptp' lines in show running-config",
    }


def lock_assessment(ptp_record: dict,
                    threshold_ns: float = 1000.0) -> dict:
    """Is the time base good enough to trust a Qbv schedule against?

    This is the check REPORT §8.1 names as the highest-value next step. It
    is advisory and it says so: a single offset sample is not a
    synchronisation guarantee, and the honest answer when PTP is not
    readable at all is "unknown", not "fine".
    """
    if ptp_record.get("status") != "available-via-cli":
        return {
            "verdict": "unknown",
            "reason": ptp_record.get("detail", "PTP state not available"),
            "safe_to_schedule": False,
        }
    summary = ptp_record.get("sync_summary", {})
    offset = summary.get("offset_from_master_ns")
    states = [s for s in (summary.get("port_states") or {}).values() if s]

    if offset is None and not states:
        return {"verdict": "unknown",
                "reason": "no offset and no port state reported",
                "safe_to_schedule": False}

    if states and all(s in ("disabled", "initializing", "faulty")
                      for s in states):
        return {"verdict": "not-synchronised",
                "reason": f"every PTP port is in state {sorted(set(states))}",
                "safe_to_schedule": False}

    if offset is not None and abs(offset) > threshold_ns:
        return {"verdict": "out-of-tolerance",
                "reason": f"offset from master {offset:.1f} ns exceeds the "
                          f"{threshold_ns:.0f} ns threshold",
                "offset_ns": offset,
                "safe_to_schedule": False}

    return {
        "verdict": "locked" if offset is not None else "running",
        "reason": (f"offset from master {offset:.1f} ns within "
                   f"{threshold_ns:.0f} ns" if offset is not None
                   else "PTP ports active, no offset sample"),
        "offset_ns": offset,
        "safe_to_schedule": offset is not None,
        "caveat": "One sample. A schedule campaign should re-check before and "
                  "after each measurement point, as i226-adaptation does with "
                  "pmc on the end stations.",
    }
