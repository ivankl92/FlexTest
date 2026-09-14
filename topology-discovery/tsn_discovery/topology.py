"""Build the network topology.

Three sources, in decreasing order of authority:

1. **LLDP** (IEEE 802.1AB) -- a neighbour entry is a switch telling us what
   it can actually see on a port. This is the only source that yields a
   *link*, and it is the only one used for switch-to-switch links. A link
   seen from both ends is marked ``confirmed`` -- LLDP is bidirectional, so
   a one-sided link means either the far end does not run LLDP or its
   advertisement has not been received yet.

2. **Filtering database** (802.1Q FDB) -- a learned or static MAC on a port.
   This is *not* a link: a MAC learned on a trunk port arrived through the
   trunk, not from a device attached to it. It is used only to attach
   endpoints, and only on ports that LLDP has not already claimed as an
   inter-switch link.

3. **SYSTEM.md** -- names and MACs. Used to put readable labels on what was
   discovered and as a cross-check oracle. It never creates a link and never
   overrides discovery; where it disagrees with the network, the
   disagreement is reported.

Every node and every link in the output carries the method and the evidence
that produced it, so that a disputed result can be traced back to a specific
NETCONF reply in the ``raw/`` capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import xmlutil as X
from .inventory import Inventory


# --- LLDP ----------------------------------------------------------------

def _owning_port_name(remote_el) -> Optional[str]:
    """Find the local port a ``remote-systems-data`` entry belongs to.

    802.1AB-2016 nests it as ``lldp/port[name]/remote-systems-data``. Some
    implementations flatten it and carry a ``local-port-num`` /
    ``local-interface`` leaf instead. Both are handled by walking up.
    """
    node = remote_el.getparent() if hasattr(remote_el, "getparent") else None
    while node is not None:
        if X.lname(node) in ("port", "interface", "lldp-port"):
            name = X.text(node, "name") or X.text(node, "if-name")
            if name:
                return name.strip()
        node = node.getparent() if hasattr(node, "getparent") else None
    # flattened form
    for key in ("local-port-num", "local-interface", "local-port-id", "port-name"):
        v = X.text(remote_el, key)
        if v:
            return v.strip()
    return None


def parse_lldp(xml_text: Optional[str]) -> Tuple[List[dict], dict]:
    """Return (neighbours, local_lldp_info)."""
    if not xml_text:
        return [], {}
    try:
        root = X.parse(xml_text)
    except Exception:
        return [], {}

    local: dict = {}
    lldp_el = X.first(root, "lldp")
    if lldp_el is not None:
        local = {
            "chassis_id": X.text(lldp_el, "chassis-id"),
            "chassis_id_mac": X.norm_mac(X.text(lldp_el, "chassis-id")),
            "chassis_id_subtype": X.strip_identity(X.text(lldp_el, "chassis-id-subtype")),
            "system_name": X.text(lldp_el, "system-name"),
            "message_tx_interval": X.to_int(X.text(lldp_el, "message-tx-interval")),
            "message_tx_hold_multiplier": X.to_int(
                X.text(lldp_el, "message-tx-hold-multiplier")),
        }
        local = {k: v for k, v in local.items() if v is not None}

    neighbours: List[dict] = []
    seen = set()
    for name in ("remote-systems-data", "remote-system-data", "remote-systems"):
        for rem in X.descendants(root, name):
            chassis_id = X.text(rem, "chassis-id")
            port_id = X.text(rem, "port-id")
            local_port = _owning_port_name(rem)
            key = (local_port, chassis_id, port_id, X.text(rem, "remote-index"))
            if key in seen:
                continue
            seen.add(key)

            mgmt = []
            for ma in X.descendants(rem, "management-address"):
                addr = X.text(ma, "address") or (ma.text or "").strip()
                if addr:
                    mgmt.append({
                        "address": addr,
                        "subtype": X.strip_identity(X.text(ma, "address-subtype")),
                    })

            neighbours.append({
                "local_port": local_port,
                "remote_index": X.to_int(X.text(rem, "remote-index")),
                "chassis_id": chassis_id,
                "chassis_id_mac": X.norm_mac(chassis_id),
                "chassis_id_subtype": X.strip_identity(
                    X.text(rem, "chassis-id-subtype")),
                "port_id": port_id,
                "port_id_mac": X.norm_mac(port_id),
                "port_id_subtype": X.strip_identity(X.text(rem, "port-id-subtype")),
                "port_desc": X.text(rem, "port-desc"),
                "system_name": X.text(rem, "system-name"),
                "system_description": X.text(rem, "system-description"),
                "management_addresses": mgmt,
            })
    return neighbours, local


# --- node identity -------------------------------------------------------

@dataclass
class Node:
    id: str
    role: str                              # switch | endpoint | unknown
    label: str
    discovered: bool = False
    in_inventory: bool = False
    mgmt_ip: Optional[str] = None
    macs: List[str] = field(default_factory=list)
    bridge_address: Optional[str] = None
    hostname: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    ports: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "label": self.label,
            "discovered": self.discovered,
            "in_inventory": self.in_inventory,
            "mgmt_ip": self.mgmt_ip,
            "macs": sorted(set(self.macs)),
            "bridge_address": self.bridge_address,
            "hostname": self.hostname,
            "evidence": self.evidence,
            "ports": self.ports,
        }


class TopologyBuilder:
    def __init__(self, inventory: Inventory, switch_records: Dict[str, dict],
                 lldp_by_switch: Dict[str, List[dict]]):
        self.inv = inventory
        self.records = switch_records          # switch name -> capability record
        self.lldp = lldp_by_switch
        self.nodes: Dict[str, Node] = {}
        self.links: List[dict] = []
        self.unresolved: List[dict] = []
        self.notes: List[str] = []

        # MAC -> switch name, from the discovered bridge base addresses
        self.bridge_mac: Dict[str, str] = {}
        for sw, rec in self.records.items():
            for b in rec.get("bridges", []):
                if b.get("address"):
                    self.bridge_mac[b["address"]] = sw
        # hostname -> switch name
        self.hostname_map: Dict[str, str] = {}
        for sw, rec in self.records.items():
            hn = (rec.get("system") or {}).get("hostname")
            if hn:
                self.hostname_map[hn.strip().lower()] = sw

    # --- nodes -----------------------------------------------------------
    def node(self, node_id: str, role: str = "unknown",
             label: Optional[str] = None) -> Node:
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(id=node_id, role=role,
                                       label=label or node_id)
        n = self.nodes[node_id]
        if role != "unknown" and n.role == "unknown":
            n.role = role
        # The inventory name wins as the label: it is the name the operator
        # uses ("SW1", "IO-D0"). A hostname learned from LLDP or ietf-system
        # is kept in its own field rather than replacing it.
        if label and not n.in_inventory and n.label == n.id:
            n.label = label
        return n

    def seed_from_inventory(self) -> None:
        for dev in self.inv.devices:
            n = self.node(dev.name, dev.role, dev.name)
            n.in_inventory = True
            n.mgmt_ip = n.mgmt_ip or dev.primary_ip
            n.macs.extend(dev.macs)

    def seed_from_discovery(self) -> None:
        for sw, rec in self.records.items():
            n = self.node(sw, "switch", sw)
            n.discovered = bool(rec.get("reachable"))
            n.mgmt_ip = n.mgmt_ip or rec.get("host")
            n.hostname = (rec.get("system") or {}).get("hostname")
            bridges = rec.get("bridges", [])
            if bridges:
                n.bridge_address = bridges[0].get("address")
                if n.bridge_address:
                    n.macs.append(n.bridge_address)
            if n.discovered:
                n.evidence.append("NETCONF session established")
            n.ports = [
                {
                    "name": i["name"],
                    "if_index": i.get("if_index"),
                    "port_number": (i.get("bridge_port") or {}).get("port_number"),
                    "enabled": i.get("enabled"),
                    "oper_status": i.get("oper_status"),
                    "speed_mbps": i.get("speed_mbps"),
                    "pvid": (i.get("bridge_port") or {}).get("pvid"),
                    "qbv": bool(i.get("qbv", {}).get("present")),
                    "qbu": bool(i.get("qbu", {}).get("present")),
                }
                for i in rec.get("interfaces", [])
                if i.get("is_bridge_port")
            ]

    # --- remote resolution ----------------------------------------------
    def resolve_remote(self, nb: dict) -> Tuple[str, str, str]:
        """Resolve an LLDP neighbour to (node_id, role, how)."""
        mac = nb.get("chassis_id_mac")

        if mac and mac in self.bridge_mac:
            return self.bridge_mac[mac], "switch", "bridge-address match"

        if mac:
            dev = self.inv.by_mac(mac)
            if dev:
                return dev.name, dev.role, "SYSTEM.md MAC match"

        sysname = (nb.get("system_name") or "").strip()
        if sysname:
            if sysname.lower() in self.hostname_map:
                return (self.hostname_map[sysname.lower()], "switch",
                        "ietf-system hostname match")
            dev = self.inv.by_name(sysname)
            if dev:
                return dev.name, dev.role, "SYSTEM.md name match"

        for ma in nb.get("management_addresses", []):
            dev = self.inv.by_ip(ma.get("address"))
            if dev:
                return dev.name, dev.role, "LLDP management-address match"

        if mac:
            dev = self.inv.by_mac_prefix(mac, octets=3)
            if dev:
                return (dev.name, dev.role,
                        "SYSTEM.md OUI+prefix match (last 3 octets differ) "
                        "-- weak, verify manually")

        label = sysname or nb.get("chassis_id") or "unknown"
        return f"lldp:{label}", "unknown", "unresolved"

    # --- links -----------------------------------------------------------
    def add_lldp_links(self) -> None:
        raw: List[dict] = []
        for sw, neighbours in self.lldp.items():
            for nb in neighbours:
                remote_id, role, how = self.resolve_remote(nb)
                if how == "unresolved":
                    self.unresolved.append({"switch": sw, "neighbour": nb})
                rn = self.node(remote_id, role,
                               nb.get("system_name") or remote_id)
                rn.discovered = True
                if nb.get("chassis_id_mac"):
                    rn.macs.append(nb["chassis_id_mac"])
                ev = f"seen by {sw} via LLDP"
                if ev not in rn.evidence:
                    rn.evidence.append(ev)
                if not rn.hostname and nb.get("system_name"):
                    rn.hostname = nb["system_name"]

                raw.append({
                    "a_node": sw,
                    "a_port": nb.get("local_port"),
                    "b_node": remote_id,
                    "b_port": nb.get("port_desc") or nb.get("port_id"),
                    "b_port_id": nb.get("port_id"),
                    "b_port_id_subtype": nb.get("port_id_subtype"),
                    "resolution": how,
                    "chassis_id": nb.get("chassis_id"),
                    "remote_system_name": nb.get("system_name"),
                })

        # Merge the two directions of each link.
        merged: Dict[Tuple, dict] = {}
        for r in raw:
            a = (r["a_node"], r["a_port"])
            b = (r["b_node"], r["b_port"])
            key = tuple(sorted([a, b], key=lambda t: (str(t[0]), str(t[1]))))
            if key in merged:
                merged[key]["confirmed_bidirectional"] = True
                merged[key]["evidence"].append(
                    f"{r['a_node']}:{r['a_port']} reports "
                    f"{r['b_node']}:{r['b_port']}")
            else:
                merged[key] = {
                    "a": {"node": key[0][0], "port": key[0][1]},
                    "b": {"node": key[1][0], "port": key[1][1]},
                    "method": "lldp",
                    "confirmed_bidirectional": False,
                    "resolution": r["resolution"],
                    "evidence": [
                        f"{r['a_node']}:{r['a_port']} reports "
                        f"{r['b_node']}:{r['b_port']}"
                    ],
                }
        self.links.extend(merged.values())

    def lldp_ports(self) -> Dict[str, set]:
        """Ports already explained by an LLDP link, per switch."""
        out: Dict[str, set] = {}
        for link in self.links:
            for end in ("a", "b"):
                node = link[end]["node"]
                port = link[end]["port"]
                if node in self.records and port:
                    out.setdefault(node, set()).add(str(port))
        return out

    def add_fdb_attachments(self) -> None:
        """Attach endpoints using the filtering database.

        Deliberately conservative. An entry only produces an attachment when
        all of the following hold:
          * the MAC resolves to a device in SYSTEM.md that is not a switch,
          * the port it was seen on is not an LLDP-confirmed inter-switch
            link, and
          * no LLDP link already exists for that endpoint.
        Anything else is reported as an observation, not a link.
        """
        claimed = self.lldp_ports()
        # An endpoint may legitimately be multi-homed -- UP-1 and UP-2 each
        # have two NICs in this testbed -- so de-duplication is per
        # (switch, port, device), not per device. What we must not do is add
        # an FDB attachment where LLDP has already established the same
        # adjacency, hence the lldp_pairs check below.
        lldp_pairs = {
            (l["a"]["node"], str(l["a"]["port"]), l["b"]["node"])
            for l in self.links if l["method"] == "lldp"
        } | {
            (l["b"]["node"], str(l["b"]["port"]), l["a"]["node"])
            for l in self.links if l["method"] == "lldp"
        }
        seen_attachments = set()

        for sw, rec in self.records.items():
            port_names = self._port_ref_map(rec)
            for bridge in rec.get("bridges", []):
                for comp in bridge.get("components", []):
                    for fe in comp.get("filtering_entries", []):
                        mac = fe.get("address")
                        if not mac:
                            continue
                        dev = self.inv.by_mac(mac)
                        if dev is None or dev.role == "switch":
                            continue
                        for pm in fe.get("port_map", []):
                            ref = pm.get("port_ref")
                            pname = port_names.get(ref, f"port-ref {ref}")
                            attachment = (sw, str(pname), dev.name)
                            if attachment in seen_attachments:
                                continue
                            if attachment in lldp_pairs:
                                continue      # LLDP already said this
                            if str(pname) in claimed.get(sw, set()):
                                self.notes.append(
                                    f"{dev.name} ({mac}) learned on {sw}:{pname}, "
                                    "which is an inter-switch link -- not treated "
                                    "as an attachment")
                                continue
                            n = self.node(dev.name, dev.role, dev.name)
                            n.discovered = True
                            n.evidence.append(
                                f"MAC {mac} in {sw} filtering database on {pname}")
                            self.links.append({
                                "a": {"node": sw, "port": pname},
                                "b": {"node": dev.name, "port": None},
                                "method": "fdb",
                                "confirmed_bidirectional": False,
                                "resolution": "SYSTEM.md MAC match in "
                                              "802.1Q filtering database",
                                "evidence": [
                                    f"filtering-entry address={mac} "
                                    f"vids={fe.get('vids')} port-ref={ref} on {sw}"
                                ],
                            })
                            seen_attachments.add(attachment)

    @staticmethod
    def _port_ref_map(rec: dict) -> Dict[int, str]:
        """Map an 802.1Q ``port-ref`` onto an interface name.

        ``port-ref`` is the bridge port number. Where the switch exposes
        ``bridge-port/port-number`` that is authoritative; otherwise
        ``if-index`` is the usual stand-in on this platform.
        """
        out: Dict[int, str] = {}
        for i in rec.get("interfaces", []):
            bp = i.get("bridge_port") or {}
            for key in (bp.get("port_number"), i.get("if_index")):
                if key is not None and key not in out:
                    out[key] = i["name"]
        return out

    # --- cross-check -----------------------------------------------------
    def crosscheck(self) -> dict:
        """Compare discovery against the inventory.

        The severity of a mismatch depends on what is missing, and the two
        cases are kept apart deliberately:

        * A **switch** in SYSTEM.md that discovery did not see is a fault:
          it is supposed to answer NETCONF.
        * An **endpoint** that discovery did not see is usually not a fault.
          An end station that neither runs LLDP nor has transmitted a frame
          recently has no entry in any filtering database and is invisible
          to both methods. That is a property of the method, not a defect
          in the network, so it is reported but does not break agreement.
        """
        inv_switches = {d.name for d in self.inv.switches}
        inv_endpoints = {d.name for d in self.inv.endpoints}
        discovered = {n.id for n in self.nodes.values() if n.discovered}

        missing_switches = sorted(inv_switches - discovered)
        missing_endpoints = sorted(inv_endpoints - discovered)
        extra = sorted(n.id for n in self.nodes.values()
                       if n.discovered and not n.in_inventory)
        unreachable = sorted(sw for sw, rec in self.records.items()
                             if not rec.get("reachable"))

        return {
            "inventory_devices": len(inv_switches) + len(inv_endpoints),
            "discovered_devices": len(discovered),
            "switches_unreachable": unreachable,
            "switches_in_inventory_not_discovered": missing_switches,
            "endpoints_in_inventory_not_observed": missing_endpoints,
            "discovered_but_not_in_inventory": extra,
            "unresolved_lldp_neighbours": len(self.unresolved),
            "agreement": not (unreachable or missing_switches or extra
                              or self.unresolved),
            "agreement_definition":
                "true when every switch in SYSTEM.md answered NETCONF, every "
                "LLDP neighbour resolved to a known device, and nothing was "
                "discovered that SYSTEM.md does not list. Endpoints that were "
                "not observed do not affect it -- see "
                "endpoints_in_inventory_not_observed.",
        }

    def build(self) -> dict:
        self.seed_from_inventory()
        self.seed_from_discovery()
        self.add_lldp_links()
        self.add_fdb_attachments()

        lldp_links = [l for l in self.links if l["method"] == "lldp"]
        fdb_links = [l for l in self.links if l["method"] == "fdb"]

        if not lldp_links:
            self.notes.append(
                "No LLDP neighbours were reported by any switch. Either LLDP "
                "is administratively disabled, the NETCONF plugin does not "
                "populate remote-systems-data, or no adjacency has been "
                "learned yet. Switch-to-switch links cannot be discovered "
                "without it -- see RUNBOOK.md §7.")

        return {
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(),
                                                  key=lambda x: (x.role, x.id))],
            "links": sorted(self.links,
                            key=lambda l: (l["a"]["node"], str(l["a"]["port"]))),
            "link_counts": {
                "total": len(self.links),
                "lldp": len(lldp_links),
                "lldp_confirmed_bidirectional": sum(
                    1 for l in lldp_links if l["confirmed_bidirectional"]),
                "fdb_attachments": len(fdb_links),
            },
            "unresolved_lldp_neighbours": self.unresolved,
            "crosscheck": self.crosscheck(),
            "notes": self.notes,
        }
