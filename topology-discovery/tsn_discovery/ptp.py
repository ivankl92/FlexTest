"""PTP / gPTP, read over the ISTAX CLI and projected into three YANG models.

This closes the gap REPORT §4 identified as `blocking-for-verification`: the
KSwitch's NETCONF server implements no PTP or gPTP YANG module, in AN001
v1.2 and still in v1.3, so a CNC reading only NETCONF cannot see the time
base its Qbv schedules are anchored to. The CLI can see it.

**Why three models and not one.** There is no single obvious target, and
picking one silently would bake a judgement call into the data:

* ``ietf-ptp`` (RFC 8575) models IEEE 1588-2008. Freely available, widely
  implemented, and the shape most existing tooling expects.
* ``ieee1588-ptp-tt`` (IEEE 1588-2019) is the IEEE's own model. It renames
  the 2008 terminology -- ``offset-from-master`` becomes
  ``offset-from-time-transmitter``, ``slave-only`` becomes
  ``time-receiver-only``, ``mean-path-delay`` becomes ``mean-delay`` -- and
  restructures ports from a flat ``port-ds-list`` into ``ports/port/port-ds``.
* ``ieee802-dot1as-gptp`` (IEEE 802.1AS-2020) is what this testbed actually
  runs: the switch is configured ``profile 802.1as``. It defines **no
  top-level containers of its own** and instead augments the 1588-2019 tree,
  adding the gPTP-specific nodes (``as-capable``, ``neighbor-rate-ratio``,
  ``mean-link-delay-thresh``, the ``current-log-*-interval`` family) and
  moving the time-properties leaves up into ``default-ds``.

So the three are not alternatives at the same level: 802.1AS is a layer on
1588-2019, and RFC 8575 is a separate lineage from 1588-2008. Emitting all
three costs little -- the parsing happens once -- and lets a consumer use
whichever matches its own model without this tool having decided for it.

**How it is structured.** ``_observe()`` parses the CLI into one
vendor-neutral intermediate. Three ``project_*()`` functions then map that
intermediate onto each model's node names. Nothing is parsed twice, and a
field that exists in one model but not another is simply absent there rather
than invented.

Each projection carries two honesty lists:

* ``unavailable`` -- nodes the model defines that the CLI does not print.
* ``derived`` -- nodes filled by inference rather than read directly, with
  the inference stated.

Source formats: Microchip AN1295 (PTP Configuration Guide) §7, and the
``ptp 0 ...`` lines of ``show running-config``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .istax import canonical_ifname
from .istax_parse import _clean_lines, parse_tables
from .xmlutil import to_int

NS_PER_SECOND = 1_000_000_000

# AN1295 prints abbreviated port states; every model spells the enumeration
# out. Anything unrecognised passes through verbatim rather than being
# forced into the enum.
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

# IEEE 1588-2019 renamed the roles. A consumer of the 1588-tt or 802.1AS
# projection expects the new spelling; one of RFC 8575 expects the old.
PORT_STATE_2019 = {
    "master": "time-transmitter",
    "slave": "time-receiver",
    "pre-master": "pre-time-transmitter",
}


# --- scalar helpers ------------------------------------------------------

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
    if v in ("true", "yes", "1", "enabled", "ok"):
        return True
    if v in ("false", "no", "0", "disabled"):
        return False
    return None


def _clock_quality(value: Optional[str]) -> Optional[dict]:
    """"Cl:248 Ac:Unknwn Va:00000" -> the clock-quality grouping.

    The grouping has the same three leaf names in all three models.
    """
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


def _prune(obj: dict) -> dict:
    """Drop keys whose value is None, so a projection shows only what the
    CLI actually supplied. What is missing is listed in ``unavailable``
    rather than left as a field full of nulls."""
    return {k: v for k, v in obj.items() if v is not None}


# --- observation: one parse, vendor-neutral ------------------------------

def _observe_default(text: Optional[str]) -> dict:
    out: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            ident = _row_get(row, "vtss_appl_clock_identity", "ClockIdentity")
            if ident:
                out["clock_identity"] = ident.strip()
            for cli, key, conv in (
                ("DeviceType", "device_type", str),
                ("Profile", "profile", str),
                ("2StepFlag", "two_step", _flag),
                ("Ports", "number_ports", to_int),
                ("Dom", "domain", to_int),
                ("Pri1", "priority1", to_int),
                ("Pri2", "priority2", to_int),
                ("Lpri", "local_priority", to_int),
                ("Protocol", "transport", str),
                ("One-Way", "one_way", _flag),
                ("VID", "vlan_id", to_int),
                ("PCP", "pcp", to_int),
                ("DSCP", "dscp", to_int),
                ("PathTraceEnable", "path_trace", _flag),
            ):
                raw = _row_get(row, cli)
                if raw not in (None, ""):
                    out[key] = conv(raw)
            quality = _row_get(row, "vtss_appl_clock_quality", "ClockQuality")
            if quality:
                out["clock_quality"] = _clock_quality(quality)
    return out


def _observe_current(text: Optional[str]) -> dict:
    out: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            steps = _row_get(row, "stpRm", "StepsRemoved")
            if steps is not None:
                out["steps_removed"] = to_int(steps)
            offset = _row_get(row, "OffsetFromMaster")
            if offset is not None:
                out["offset_ns"] = _seconds_to_ns(offset)
                out["offset_raw"] = offset.strip()
            delay = _row_get(row, "MeanPathDelay")
            if delay is not None:
                out["mean_path_delay_ns"] = _seconds_to_ns(delay)
                out["mean_path_delay_raw"] = delay.strip()
    return out


def _observe_parent(text: Optional[str]) -> dict:
    out: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            for cli, key, conv in (
                ("ParentPortIdentity", "parent_port_identity", str),
                ("port", "parent_port_number", to_int),
                ("Pstat", "parent_stats", _flag),
                ("Var", "observed_variance", to_int),
                ("ChangeRate", "observed_phase_change_rate", to_int),
                ("GrandmasterIdentity", "gm_identity", str),
                ("Pri1", "gm_priority1", to_int),
                ("Pri2", "gm_priority2", to_int),
            ):
                raw = _row_get(row, cli)
                if raw not in (None, ""):
                    out[key] = conv(raw)
            gq = _row_get(row, "GrandmasterClockQuality")
            if gq:
                out["gm_clock_quality"] = _clock_quality(gq)
    return out


def _observe_time_properties(text: Optional[str]) -> dict:
    out: dict = {}
    mapping = {
        "UtcOffset": ("current_utc_offset", to_int),
        "Valid": ("current_utc_offset_valid", _flag),
        "leap59": ("leap59", _flag),
        "leap61": ("leap61", _flag),
        "TimeTrac": ("time_traceable", _flag),
        "FreqTrac": ("frequency_traceable", _flag),
        "ptpTimeScale": ("ptp_timescale", _flag),
        "TimeSource": ("time_source", to_int),
    }
    for rows in parse_tables(text):
        for row in rows:
            for cli, (key, conv) in mapping.items():
                raw = _row_get(row, cli)
                if raw not in (None, ""):
                    out[key] = conv(raw)
    return out


def _observe_ports(text: Optional[str],
                   interface: Optional[str] = None) -> List[dict]:
    """`show ptp 0 port-state`. The virtual-port table that follows the
    physical one is skipped: a virtual port is not a PTP network port and
    carrying it into port-ds would invent an interface."""
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
                "port_number": to_int(port),
                "port_state": PORT_STATE_MAP.get(state, state or None),
                "port_state_raw": state or None,
                "enabled": _flag(_row_get(row, "Enabled")),
                "internal": _flag(_row_get(row, "Internal")),
                "link": _row_get(row, "Link"),
                "port_timer": _row_get(row, "Port-Timer"),
                "vlan_forward": _row_get(row, "Vlan-forw"),
                "phy_timestamper": _flag(_row_get(row, "Phy-timestamper")),
                "peer_delay_status": _row_get(row, "Peer-delay"),
            }
            if interface:
                entry["interface"] = canonical_ifname(interface)
            out.append(entry)
    return out


def _observe_servo(text: Optional[str]) -> dict:
    """`show ptp 0 slave` -- vendor servo status. No model defines it, so it
    stays under its own key rather than being smuggled into a standard
    dataset."""
    out: dict = {}
    for rows in parse_tables(text):
        for row in rows:
            if _row_get(row, "Slave port") is None:
                continue
            out["slave_port"] = to_int(_row_get(row, "Slave port"))
            out["slave_state"] = _row_get(row, "Slave state")
            holdover = _row_get(row, "Holdover(ppb)", "Holdover")
            try:
                out["holdover_ppb"] = (float(holdover)
                                       if holdover not in (None, "") else None)
            except ValueError:
                out["holdover_ppb"] = None
    return out


# Per-interface `ptp 0 ...` running-config lines -> neutral keys.
_IFACE_PATTERNS = [
    (r"^announce interval\s+(-?\d+)\s+timeout\s+(\d+)", None),
    (r"^sync-interval\s+(-?\d+)", "log_sync_interval"),
    (r"^delay-mechanism\s+(\S+)", "delay_mechanism"),
    (r"^delay-req interval\s+(-?\d+)", "log_min_delay_req_interval"),
    (r"^delay-asymmetry\s+(-?\d+)", "delay_asymmetry"),
    (r"^ingress-latency\s+(-?\d+)", "ingress_latency"),
    (r"^egress-latency\s+(-?\d+)", "egress_latency"),
    (r"^gptp-interval\s+(-?\d+)", "log_gptp_cap_interval"),
    (r"^mcast-dest\s+(\S+)", "mcast_dest"),
]


def _observe_interface_config(lines: List[str]) -> dict:
    """One interface's `ptp 0 ...` lines."""
    out: dict = {}
    for line in lines:
        m = re.match(r"^ptp\s+(\d+)\s*(.*)$", line.strip(), re.I)
        if not m:
            continue
        out.setdefault("instance_number", int(m.group(1)))
        rest = m.group(2).strip()
        if not rest:
            out["ptp_enabled"] = True
            continue
        for pattern, key in _IFACE_PATTERNS:
            m2 = re.match(pattern, rest, re.I)
            if not m2:
                continue
            if key is None:                      # announce interval + timeout
                out["log_announce_interval"] = to_int(m2.group(1))
                out["announce_receipt_timeout"] = to_int(m2.group(2))
            elif key in ("delay_mechanism", "mcast_dest"):
                out[key] = m2.group(1).lower()
            else:
                out[key] = to_int(m2.group(1))
            break
    return out


def _observe_global_config(lines: List[str]) -> dict:
    """The global `ptp 0 mode ...` line and its followers."""
    out: dict = {"raw": list(lines)}
    for line in lines:
        m = re.match(r"^ptp\s+(\d+)\s+(.*)$", line.strip(), re.I)
        if not m:
            continue
        out.setdefault("instance_number", int(m.group(1)))
        rest = m.group(2)
        mode = re.match(r"^mode\s+(\S+)", rest, re.I)
        if mode:
            out["clock_mode"] = mode.group(1).lower()
            out["two_step"] = bool(re.search(r"\btwostep\b", rest, re.I))
            out["transport"] = (
                "ethernet" if re.search(r"\bethernet\b", rest, re.I)
                else "ipv4" if re.search(r"\bipv4\b", rest, re.I) else None)
            out["one_way"] = not bool(re.search(r"\btwoway\b", rest, re.I))
            prof = re.search(r"profile\s+(\S+)", rest, re.I)
            if prof:
                out["profile"] = prof.group(1)
                out["is_gptp"] = prof.group(1).lower() in (
                    "802.1as", "8021as", "g8275.1-gptp")
            vid = re.search(r"\bvid\s+(\d+)(?:\s+(\d+))?", rest, re.I)
            if vid:
                out["vlan_id"] = to_int(vid.group(1))
                if vid.group(2):
                    out["pcp"] = to_int(vid.group(2))
            mep = re.search(r"\bmep\s+(\d+)", rest, re.I)
            if mep:
                out["mep"] = to_int(mep.group(1))
            continue
        for key in ("filter-type", "source-time-inaccuracy",
                    "gm-time-inaccuracy", "dist-time-inaccuracy"):
            m2 = re.match(rf"^{key}\s+(\S+)", rest, re.I)
            if m2:
                out[key.replace("-", "_")] = m2.group(1)
    return out


def _observe(result, running_cfg: dict, interfaces: List[dict]) -> dict:
    """Parse every PTP source once into a vendor-neutral intermediate."""
    obs: dict = {
        "clock": {},
        "current": _observe_current(result.text_of("ptp-current")),
        "parent": _observe_parent(result.text_of("ptp-parent")),
        "time_properties": _observe_time_properties(
            result.text_of("ptp-time-property")),
        "servo": _observe_servo(result.text_of("ptp-slave")),
        "ports": [],
    }

    obs["clock"].update(_observe_default(result.text_of("ptp-default")))
    obs["clock"].update(
        _observe_global_config(running_cfg.get("ptp_global", [])))

    # Port state, switch-wide form then per-interface form.
    ports: List[dict] = list(_observe_ports(result.text_of("ptp-port-state")))
    for iface in interfaces:
        slug = iface["name"].replace(" ", "_").replace("/", "-")
        text = result.text_of(f"ptp-port-state--{slug}")
        if text:
            ports.extend(_observe_ports(text, interface=iface["name"]))

    # Join port-state rows to interfaces by port number, and fold in the
    # per-interface running-config. The port number in `show ptp 0
    # port-state` is the bridge port number, which is how the interface
    # records are keyed too.
    by_number = {i["bridge_port"]["port_number"]: i for i in interfaces
                 if (i.get("bridge_port") or {}).get("port_number") is not None}
    seen = set()
    unmatched: List[int] = []
    for port in ports:
        number = port.get("port_number")
        iface = by_number.get(number)
        if iface is not None:
            port.setdefault("interface", iface["name"])
            port.update(_observe_interface_config(
                iface.get("_ptp_config_lines", [])))
        elif number is not None and not port.get("interface"):
            # A PTP port the interface table does not account for. Left
            # unnamed rather than guessed: attaching PTP state to the wrong
            # interface is worse than leaving it unattributed, and silently
            # dropping it would hide a real inconsistency.
            unmatched.append(number)
        key = (number, port.get("interface"))
        if key in seen:
            continue
        seen.add(key)
        obs["ports"].append(port)

    if unmatched:
        obs.setdefault("warnings", []).append(
            f"`show ptp 0 port-state` reported port(s) {sorted(unmatched)} "
            "that `show interface * status` does not list. PTP state for "
            "those ports is recorded without an interface name. Port numbers "
            "are derived from the interface table's ordering, so an "
            "incomplete table shifts them -- check both outputs in raw/.")

    # Interfaces configured for PTP but absent from the port-state table
    # still belong in port-ds: the configuration is real even if no state
    # was returned.
    for iface in interfaces:
        lines = iface.get("_ptp_config_lines", [])
        if not lines:
            continue
        number = (iface.get("bridge_port") or {}).get("port_number")
        if (number, iface["name"]) in seen or number in {
                p.get("port_number") for p in obs["ports"]}:
            continue
        entry = {"port_number": number, "interface": iface["name"]}
        entry.update(_observe_interface_config(lines))
        obs["ports"].append(entry)

    obs["ports"].sort(key=lambda p: (p.get("port_number") is None,
                                     p.get("port_number") or 0))
    return obs


# --- projection 1: ietf-ptp (RFC 8575) -----------------------------------

def project_ietf_ptp(obs: dict) -> dict:
    """RFC 8575 -- IEEE 1588-2008 terminology, flat ``port-ds-list``."""
    clock, current, parent = obs["clock"], obs["current"], obs["parent"]

    default_ds = _prune({
        "two-step-flag": clock.get("two_step"),
        "clock-identity": clock.get("clock_identity"),
        "number-ports": clock.get("number_ports"),
        "clock-quality": clock.get("clock_quality"),
        "priority1": clock.get("priority1"),
        "priority2": clock.get("priority2"),
        "domain-number": clock.get("domain"),
    })

    current_ds = _prune({
        "steps-removed": current.get("steps_removed"),
        "offset-from-master": current.get("offset_ns"),
        "mean-path-delay": current.get("mean_path_delay_ns"),
    })

    parent_ds = _prune({
        "parent-port-identity": parent.get("parent_port_identity"),
        "parent-stats": parent.get("parent_stats"),
        "observed-parent-offset-scaled-log-variance":
            parent.get("observed_variance"),
        "observed-parent-clock-phase-change-rate":
            parent.get("observed_phase_change_rate"),
        "grandmaster-identity": parent.get("gm_identity"),
        "grandmaster-clock-quality": parent.get("gm_clock_quality"),
        "grandmaster-priority1": parent.get("gm_priority1"),
        "grandmaster-priority2": parent.get("gm_priority2"),
    })

    tp = obs["time_properties"]
    time_properties_ds = _prune({
        "current-utc-offset-valid": tp.get("current_utc_offset_valid"),
        "current-utc-offset": tp.get("current_utc_offset"),
        "leap59": tp.get("leap59"),
        "leap61": tp.get("leap61"),
        "time-traceable": tp.get("time_traceable"),
        "frequency-traceable": tp.get("frequency_traceable"),
        "ptp-timescale": tp.get("ptp_timescale"),
        "time-source": tp.get("time_source"),
    })

    port_ds_list = []
    for p in obs["ports"]:
        port_ds_list.append(_prune({
            "port-number": p.get("port_number"),
            "port-state": p.get("port_state"),
            "underlying-interface": p.get("interface"),
            "log-min-delay-req-interval": p.get("log_min_delay_req_interval"),
            "peer-mean-path-delay": None,     # not printed per port
            "log-announce-interval": p.get("log_announce_interval"),
            "announce-receipt-timeout": p.get("announce_receipt_timeout"),
            "log-sync-interval": p.get("log_sync_interval"),
            "delay-mechanism": p.get("delay_mechanism"),
            "log-min-pdelay-req-interval": (
                p.get("log_min_delay_req_interval")
                if p.get("delay_mechanism") == "p2p" else None),
        }))

    return {
        "module": "ietf-ptp",
        "revision": "2019-05-06",
        "reference": "RFC 8575 (models IEEE 1588-2008)",
        "namespace": "urn:ietf:params:xml:ns:yang:ietf-ptp",
        "path": "/ptp/instance-list",
        "instance-list": [_prune({
            "instance-number": clock.get("instance_number", 0),
            "default-ds": default_ds or None,
            "current-ds": current_ds or None,
            "parent-ds": parent_ds or None,
            "time-properties-ds": time_properties_ds or None,
            "port-ds-list": port_ds_list or None,
        })],
        "unavailable": [
            "default-ds/slave-only — not printed by `show ptp 0 default`",
            "port-ds-list/peer-mean-path-delay — the CLI reports mean path "
            "delay once per instance (`show ptp 0 current`), not per port",
            "port-ds-list/version-number — not printed",
        ],
        "derived": [
            "port-ds-list/log-min-pdelay-req-interval — taken from "
            "`ptp 0 delay-req interval` where the delay mechanism is p2p; "
            "the CLI uses one knob for both mechanisms",
        ],
    }


# --- projection 2: ieee1588-ptp-tt (IEEE 1588-2019) ----------------------

def project_ieee1588(obs: dict) -> dict:
    """IEEE 1588-2019. Note the renamed leaves and the nested port list."""
    clock, current, parent = obs["clock"], obs["current"], obs["parent"]

    default_ds = _prune({
        "two-step-flag": clock.get("two_step"),
        "clock-identity": clock.get("clock_identity"),
        "number-ports": clock.get("number_ports"),
        "clock-quality": clock.get("clock_quality"),
        "priority1": clock.get("priority1"),
        "priority2": clock.get("priority2"),
        "domain-number": clock.get("domain"),
        "instance-type": clock.get("clock_mode"),
    })

    current_ds = _prune({
        "steps-removed": current.get("steps_removed"),
        # 1588-2019 renamed offsetFromMaster and meanPathDelay.
        "offset-from-time-transmitter": current.get("offset_ns"),
        "mean-delay": current.get("mean_path_delay_ns"),
    })

    parent_ds = _prune({
        "parent-port-identity": parent.get("parent_port_identity"),
        "parent-stats": parent.get("parent_stats"),
        "observed-parent-offset-scaled-log-variance":
            parent.get("observed_variance"),
        "observed-parent-clock-phase-change-rate":
            parent.get("observed_phase_change_rate"),
        "grandmaster-identity": parent.get("gm_identity"),
        "grandmaster-clock-quality": parent.get("gm_clock_quality"),
        "grandmaster-priority1": parent.get("gm_priority1"),
        "grandmaster-priority2": parent.get("gm_priority2"),
    })

    tp = obs["time_properties"]
    time_properties_ds = _prune({
        "current-utc-offset": tp.get("current_utc_offset"),
        "current-utc-offset-valid": tp.get("current_utc_offset_valid"),
        "leap59": tp.get("leap59"),
        "leap61": tp.get("leap61"),
        "time-traceable": tp.get("time_traceable"),
        "frequency-traceable": tp.get("frequency_traceable"),
        "ptp-timescale": tp.get("ptp_timescale"),
        "time-source": tp.get("time_source"),
    })

    ports = []
    for p in obs["ports"]:
        state = p.get("port_state")
        ports.append(_prune({
            "port-number": p.get("port_number"),
            "underlying-interface": p.get("interface"),
            "port-ds": _prune({
                "port-identity": (
                    f"{clock.get('clock_identity')}/{p.get('port_number')}"
                    if clock.get("clock_identity")
                    and p.get("port_number") is not None else None),
                "port-state": PORT_STATE_2019.get(state, state),
                "log-min-delay-req-interval":
                    p.get("log_min_delay_req_interval"),
                "log-announce-interval": p.get("log_announce_interval"),
                "announce-receipt-timeout": p.get("announce_receipt_timeout"),
                "log-sync-interval": p.get("log_sync_interval"),
                "delay-mechanism": p.get("delay_mechanism"),
            }) or None,
        }))

    return {
        "module": "ieee1588-ptp-tt",
        "revision": "2023-08-14",
        "reference": "IEEE Std 1588-2019",
        "namespace": "urn:ieee:std:1588:yang:ieee1588-ptp-tt",
        "path": "/ptp/instances/instance",
        "terminology_note":
            "IEEE 1588-2019 replaced the master/slave terminology. "
            "offset-from-master is offset-from-time-transmitter here, "
            "mean-path-delay is mean-delay, slave-only is "
            "time-receiver-only, and the port states master/slave are "
            "time-transmitter/time-receiver. The values are the same "
            "readings as the ietf-ptp projection.",
        "instances": {"instance": [_prune({
            "instance-number": clock.get("instance_number", 0),
            "default-ds": default_ds or None,
            "current-ds": current_ds or None,
            "parent-ds": parent_ds or None,
            "time-properties-ds": time_properties_ds or None,
            "ports": {"port": ports} if ports else None,
        })]},
        "unavailable": [
            "default-ds/time-receiver-only, sdo-id, instance-enable, "
            "external-port-config-enable, max-steps-removed — not printed",
            "current-ds/synchronization-uncertain — not printed",
            "parent-ds/protocol-address — not printed",
            "port-ds/mean-link-delay — the CLI reports mean path delay once "
            "per instance, not per port",
            "common-services/cmlds — the CLI exposes no CMLDS state",
        ],
        "derived": [
            "default-ds/instance-type — from `ptp 0 mode <boundary|…>` in the "
            "running configuration",
            "port-ds/port-identity — composed from the clock identity and the "
            "port number; the CLI prints them separately",
            "port-ds/port-state — the 2019 role names, remapped from the "
            "CLI's mstr/slve",
        ],
    }


# --- projection 3: ieee802-dot1as-gptp (IEEE 802.1AS-2020) ---------------

def project_dot1as(obs: dict) -> dict:
    """IEEE 802.1AS-2020, as augmentations on the 1588-2019 tree.

    This module defines no top-level containers: it augments
    ``/ptp-tt:ptp/ptp-tt:instances/ptp-tt:instance/...``. So this projection
    emits only the augmentation content, and points at the 1588 projection
    for the base datasets. Two structural differences from 1588 are worth
    noticing: 802.1AS lifts the time-properties leaves up into
    ``default-ds``, and it adds the ``current-log-*-interval`` family to
    ``port-ds``.
    """
    clock, tp = obs["clock"], obs["time_properties"]

    # 802.1AS augments default-ds with gm-capable and the time properties.
    default_ds_aug = _prune({
        "gm-capable": None,
        "current-utc-offset": tp.get("current_utc_offset"),
        "current-utc-offset-valid": tp.get("current_utc_offset_valid"),
        "leap59": tp.get("leap59"),
        "leap61": tp.get("leap61"),
        "time-traceable": tp.get("time_traceable"),
        "frequency-traceable": tp.get("frequency_traceable"),
        "ptp-timescale": tp.get("ptp_timescale"),
        "time-source": tp.get("time_source"),
    })

    port_ds_aug = []
    for p in obs["ports"]:
        measuring = _flag(p.get("peer_delay_status"))
        port_ds_aug.append(_prune({
            "port-number": p.get("port_number"),
            "underlying-interface": p.get("interface"),
            "is-measuring-delay": measuring,
            "as-capable": None,
            "current-log-sync-interval": p.get("log_sync_interval"),
            "current-log-announce-interval": p.get("log_announce_interval"),
            "current-log-pdelay-req-interval": (
                p.get("log_min_delay_req_interval")
                if p.get("delay_mechanism") == "p2p" else None),
            "current-log-gptp-cap-interval": p.get("log_gptp_cap_interval"),
            "sync-receipt-timeout": p.get("announce_receipt_timeout"),
        }))

    return {
        "module": "ieee802-dot1as-gptp",
        "revision": "2025-02-04",
        "reference": "IEEE Std 802.1AS-2020",
        "namespace": "urn:ieee:std:802.1AS:yang:ieee802-dot1as-gptp",
        "path": "augments /ptp-tt:ptp/ptp-tt:instances/ptp-tt:instance",
        "structure_note":
            "This module defines no top-level containers. It augments the "
            "ieee1588-ptp-tt tree, so the base datasets are in the "
            "ieee1588-ptp-tt projection and only the augmentation content "
            "appears here. Note that 802.1AS moves the time-properties "
            "leaves into default-ds rather than keeping a separate "
            "time-properties-ds.",
        "profile_active": bool(clock.get("is_gptp")),
        "profile_reported": clock.get("profile"),
        "augments": {
            "default-ds": default_ds_aug or None,
            "current-ds": None,
            "parent-ds": None,
            "ports/port/port-ds": port_ds_aug or None,
        },
        "unavailable": [
            "port-ds/as-capable — the single most useful gPTP predicate, and "
            "the CLI does not print it. `show ptp 0 port-state` reports "
            "Peer-delay status, which is evidence that the peer delay "
            "mechanism is running but is not the same assertion.",
            "port-ds/neighbor-rate-ratio, mean-link-delay-thresh, "
            "neg-mean-link-delay-thresh, sync-locked, allowed-lost-responses, "
            "one-step-tx-oper — not printed",
            "port-statistics-ds — the counters exist in the web UI "
            "(Monitor → Ports → Detailed Statistics) but not in a `show` "
            "command this collector issues",
            "default-ds/gm-capable — not printed",
            "current-ds/last-gm-phase-change, gm-timebase-indicator, "
            "gm-change-count — not printed",
            "parent-ds/cumulative-rate-ratio — not printed",
            "common-services/cmlds — not exposed",
        ],
        "derived": [
            "port-ds/is-measuring-delay — from the Peer-delay column of "
            "`show ptp 0 port-state` (OK is read as measuring)",
            "port-ds/current-log-pdelay-req-interval — from "
            "`ptp 0 delay-req interval` where the delay mechanism is p2p",
            "port-ds/sync-receipt-timeout — from the timeout operand of "
            "`ptp 0 announce interval <n> timeout <m>`; the CLI does not "
            "separate announce and sync receipt timeouts",
        ],
    }


# --- assembly ------------------------------------------------------------

def build(result, running_cfg: dict, interfaces: List[dict]) -> dict:
    """Assemble the PTP record for one CLI-read switch."""
    obs = _observe(result, running_cfg, interfaces)

    configured = bool(running_cfg.get("ptp_global"))
    has_state = bool(obs["current"] or obs["parent"] or obs["ports"]
                     or obs["clock"].get("clock_identity"))

    if not (configured or has_state):
        return {
            "status": "not-configured",
            "source": "cli",
            "detail": "No PTP instance is configured and no PTP state was "
                      "returned by the CLI on this switch.",
            "models": {},
            "observation": obs,
            "evidence": "show ptp 0 default / current / parent returned "
                        "nothing and show running-config contains no 'ptp' "
                        "lines",
        }

    models = {
        "ietf-ptp": project_ietf_ptp(obs),
        "ieee1588-ptp-tt": project_ieee1588(obs),
        "ieee802-dot1as-gptp": project_dot1as(obs),
    }

    offset_ns = obs["current"].get("offset_ns")
    servo = obs["servo"]

    return {
        "status": "available-via-cli",
        "source": "cli",
        "detail": "PTP is not exposed by this switch's NETCONF server (no PTP "
                  "YANG module in AN001 v1.2 or v1.3). These values come from "
                  "the CLI and are projected into three YANG models so that a "
                  "consumer can use whichever matches its own data model.",
        "models_note":
            "The three are not alternatives at the same level. "
            "ieee802-dot1as-gptp augments ieee1588-ptp-tt and carries only "
            "the gPTP-specific nodes; ietf-ptp is a separate lineage from "
            "IEEE 1588-2008 and uses the older terminology. The same readings "
            "appear in each, under that model's own node names.",
        "gptp_profile": bool(obs["clock"].get("is_gptp")),
        "models": models,
        "warnings": obs.get("warnings", []),
        "observation": obs,
        "sync_summary": {
            "offset_from_master_ns": offset_ns,
            "mean_path_delay_ns": obs["current"].get("mean_path_delay_ns"),
            "steps_removed": obs["current"].get("steps_removed"),
            "servo_state": servo.get("slave_state"),
            "holdover_ppb": servo.get("holdover_ppb"),
            "grandmaster_identity": obs["parent"].get("gm_identity"),
            "port_states": {p.get("interface") or p.get("port_number"):
                            p.get("port_state") for p in obs["ports"]
                            if p.get("port_state")},
        },
        "evidence": "show ptp 0 default / current / parent / time-property / "
                    "port-state / slave, plus 'ptp' lines in "
                    "show running-config",
    }


def lock_assessment(ptp_record: dict,
                    threshold_ns: float = 1000.0) -> dict:
    """Is the time base good enough to trust a Qbv schedule against?

    The verification gate REPORT §8 named as the highest-value next step. It
    is advisory and says so: a single offset sample is not a synchronisation
    guarantee, and the honest answer when PTP is not readable at all is
    "unknown", not "fine".
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
        "caveat": "One sample, and as-capable is not readable over the CLI. A "
                  "schedule campaign should re-check before and after each "
                  "measurement point, as i226-adaptation does with pmc on the "
                  "end stations.",
    }
