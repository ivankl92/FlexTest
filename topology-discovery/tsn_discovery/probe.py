"""Runtime YANG-model probe.

What a switch *can* be configured for is decided by which YANG modules its
NETCONF server actually implements, not by what the datasheet or the
application note says. Kontron AN001 v1.2 documents the module set for
Network OS release GA-2.03; firmware moves, and the testbed has five
switches that need not all be on the same release.

So the model set is established at runtime, from two independent sources:

* the NETCONF ``<hello>`` capability list, and
* ``/ietf-netconf-monitoring:netconf-state/schemas``.

Both are recorded. Where they disagree, the union is reported with the
source of each entry, because a disagreement is itself worth seeing.

This module also answers the specific question that motivated the
subproject's scope: **is PTP configurable over NETCONF on this hardware?**
That is decided by matching the discovered module set against the known
PTP/gPTP YANG modules, not by assuming either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import xmlutil as X
from .netconf import SwitchResult

# --- TSN feature -> YANG module mapping ----------------------------------
# Each entry: feature key -> (human label, [candidate module names])
# Candidates are matched case-insensitively as substrings of module names,
# so a vendor-prefixed or newer-revision module still matches.

FEATURE_MODULES: Dict[str, dict] = {
    "bridge": {
        "label": "802.1Q bridging / VLAN",
        "modules": ["ieee802-dot1q-bridge"],
        "std": "IEEE 802.1Q",
    },
    "qbv": {
        "label": "802.1Qbv time-aware shaper (TAS)",
        "modules": ["ieee802-dot1q-sched"],
        "std": "IEEE 802.1Qbv / 802.1Q-2018 §8.6.8.4",
    },
    "qbu": {
        "label": "802.1Qbu frame preemption",
        "modules": ["ieee802-dot1q-preemption"],
        "std": "IEEE 802.1Qbu / 802.3br",
    },
    "qci": {
        "label": "802.1Qci per-stream filtering and policing (PSFP)",
        "modules": ["ieee802-dot1q-psfp", "dot1q-psfp"],
        "std": "IEEE 802.1Qci",
    },
    "qav": {
        "label": "802.1Qav credit-based shaper (CBS)",
        "modules": ["ieee802-dot1q-cb", "dot1q-cbs", "ieee802-dot1q-qav"],
        "std": "IEEE 802.1Qav",
    },
    "qcc": {
        "label": "802.1Qcc stream / UNI configuration",
        "modules": ["ieee802-dot1q-tsn-config-uni", "ietf-detnet", "dot1q-stream"],
        "std": "IEEE 802.1Qcc",
    },
    "lldp": {
        "label": "802.1AB LLDP (topology discovery)",
        "modules": ["ieee802-dot1ab-lldp"],
        "std": "IEEE 802.1AB",
    },
    "ethernet": {
        "label": "802.3 Ethernet interface attributes",
        "modules": ["ieee802-ethernet-interface"],
        "std": "IEEE 802.3",
    },
    "interfaces": {
        "label": "IETF interface management",
        "modules": ["ietf-interfaces"],
        "std": "RFC 8343",
    },
    "system": {
        "label": "IETF system management",
        "modules": ["ietf-system"],
        "std": "RFC 7317",
    },
    "ptp": {
        "label": "PTP / gPTP time synchronisation",
        "modules": [
            "ieee1588-ptp",             # IEEE 1588-2019 YANG
            "ieee1588-ptp-dataset",
            "ietf-ptp",                 # RFC 8575
            "ieee802-dot1as",           # gPTP
            "ieee802-dot1as-ptp",
            "tsn-ptp",
        ],
        "std": "IEEE 1588 / 802.1AS",
    },
}


@dataclass
class FeatureSupport:
    key: str
    label: str
    standard: str
    supported: bool
    modules_found: List[dict] = field(default_factory=list)
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "feature": self.key,
            "label": self.label,
            "standard": self.standard,
            "supported": self.supported,
            "modules": self.modules_found,
            "evidence": self.evidence,
        }


def parse_schema_list(xml_text: Optional[str]) -> Dict[str, dict]:
    """Extract modules from ietf-netconf-monitoring ``schemas``."""
    if not xml_text:
        return {}
    out: Dict[str, dict] = {}
    try:
        root = X.parse(xml_text)
    except Exception:
        return {}
    for schema in X.descendants(root, "schema"):
        ident = X.text(schema, "identifier")
        if not ident:
            continue
        out[ident] = {
            "revision": X.text(schema, "version") or X.text(schema, "revision"),
            "namespace": X.text(schema, "namespace"),
            "format": X.strip_identity(X.text(schema, "format")),
        }
    return out


def merge_modules(result: SwitchResult) -> Dict[str, dict]:
    """Union of hello-advertised modules and monitoring schema list."""
    merged: Dict[str, dict] = {}
    for name, info in result.modules.items():
        merged[name] = dict(info)
        merged[name]["sources"] = ["hello"]

    schemas = parse_schema_list(result.xml_of("netconf-state-schemas"))
    for name, info in schemas.items():
        if name in merged:
            merged[name]["sources"].append("netconf-monitoring")
            if not merged[name].get("revision"):
                merged[name]["revision"] = info.get("revision")
            if not merged[name].get("namespace"):
                merged[name]["namespace"] = info.get("namespace")
        else:
            merged[name] = {
                "revision": info.get("revision"),
                "namespace": info.get("namespace"),
                "features": [],
                "deviations": [],
                "capability": None,
                "sources": ["netconf-monitoring"],
            }
    return merged


def match_feature(feature_key: str, spec: dict,
                  modules: Dict[str, dict]) -> FeatureSupport:
    found: List[dict] = []
    for candidate in spec["modules"]:
        for name, info in modules.items():
            if candidate.lower() in name.lower():
                found.append({
                    "module": name,
                    "revision": info.get("revision"),
                    "namespace": info.get("namespace"),
                    "sources": info.get("sources", []),
                })
    # de-duplicate, preserving order
    seen = set()
    uniq = []
    for f in found:
        if f["module"] not in seen:
            seen.add(f["module"])
            uniq.append(f)

    if uniq:
        evidence = "advertised as " + ", ".join(
            f"{f['module']}@{f['revision'] or 'no-revision'}"
            f" (via {'+'.join(f['sources']) or 'unknown'})" for f in uniq
        )
    else:
        evidence = (
            "no module matching "
            + "/".join(spec["modules"])
            + " in the NETCONF <hello> capability list or in "
              "/ietf-netconf-monitoring:netconf-state/schemas"
        )

    return FeatureSupport(
        key=feature_key,
        label=spec["label"],
        standard=spec["std"],
        supported=bool(uniq),
        modules_found=uniq,
        evidence=evidence,
    )


def probe(result: SwitchResult) -> dict:
    """Full model probe for one switch."""
    modules = merge_modules(result)
    features = {
        key: match_feature(key, spec, modules)
        for key, spec in FEATURE_MODULES.items()
    }

    base_caps = sorted(
        c for c in result.server_capabilities
        if c.startswith("urn:ietf:params:netconf:")
    )

    return {
        "netconf": {
            "session_id": result.session_id,
            "base_capabilities": base_caps,
            "supports_candidate": any(":candidate:" in c for c in base_caps),
            "supports_startup": any(":startup:" in c for c in base_caps),
            "supports_writable_running": any(":writable-running:" in c for c in base_caps),
            "supports_xpath": any(":xpath:" in c for c in base_caps),
            "supports_notification": any(":notification:" in c for c in base_caps),
            "supports_yang_library": any("yang-library" in c for c in base_caps),
            "capability_count": len(result.server_capabilities),
        },
        "yang_modules": {
            name: {k: v for k, v in info.items() if k != "capability"}
            for name, info in sorted(modules.items())
        },
        "module_count": len(modules),
        "features": {k: v.to_dict() for k, v in sorted(features.items())},
        "unsupported_features": sorted(
            k for k, v in features.items() if not v.supported
        ),
    }


def ptp_finding(probe_result: dict) -> dict:
    """The PTP question, answered explicitly.

    Called out separately because the testbed's latency measurement depends
    entirely on gPTP, so "can the CNC read or set PTP over NETCONF" is a
    first-order question rather than one feature among many.
    """
    feat = probe_result["features"]["ptp"]
    if feat["supported"]:
        return {
            "status": "available",
            "detail": "A PTP/gPTP YANG module is implemented by this NETCONF "
                      "server; PTP state and configuration are reachable over "
                      "NETCONF.",
            "modules": feat["modules"],
            "evidence": feat["evidence"],
            "consequence": None,
        }
    return {
        "status": "not-exposed-via-netconf",
        "detail": "This switch's NETCONF server implements no PTP or gPTP "
                  "YANG module. PTP is configured and monitored out of band "
                  "(ISTAX CLI, web UI or SNMP), not through NETCONF.",
        "modules": [],
        "evidence": feat["evidence"],
        "consequence": "A CNC built on this NETCONF surface can compute and "
                       "install Qbv schedules, but cannot verify or configure "
                       "the time base those schedules are anchored to. Base "
                       "times must be treated as unverified over NETCONF, and "
                       "gPTP lock must be checked by a separate mechanism "
                       "before a schedule is trusted.",
    }
