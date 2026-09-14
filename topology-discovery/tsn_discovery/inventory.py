"""Parse the testbed inventory out of SYSTEM.md.

SYSTEM.md is the human-maintained description of the testbed. It is used
here as a *cross-check oracle*, not as a source of truth for the topology:
discovery reports what the network says, and the inventory is what we
compare that against. Disagreements are reported, never silently resolved.

The address table in SYSTEM.md looks like this (tab/space separated, with
continuation lines for devices that have a second port)::

            IPv4            MAC
    UP-1    192.168.1.61    00:07:32:C1:43:30
            192.168.1.62    00:07:32:C1:43:31
    RPI1    192.168.1.51    88:a2:9e:4b:97:1b
    SW1     192.168.1.10    00:80:82:b9:65:33
    CTRL    192.168.1.120   00-BB-CC-DD-EE-12

A blank name continues the previous device, adding another interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .xmlutil import norm_mac

IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
# A device name is the first token on the line and is not an IP address.
NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]*)\b")

SWITCH_NAME_RE = re.compile(r"^(SW|KSW|KSWITCH)\d+$", re.IGNORECASE)


@dataclass
class Interface:
    ipv4: Optional[str] = None
    mac: Optional[str] = None


@dataclass
class Device:
    name: str
    role: str                       # "switch" | "endpoint"
    interfaces: List[Interface] = field(default_factory=list)

    @property
    def primary_ip(self) -> Optional[str]:
        for i in self.interfaces:
            if i.ipv4:
                return i.ipv4
        return None

    @property
    def macs(self) -> List[str]:
        return [i.mac for i in self.interfaces if i.mac]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "interfaces": [{"ipv4": i.ipv4, "mac": i.mac} for i in self.interfaces],
        }


@dataclass
class Inventory:
    devices: List[Device] = field(default_factory=list)
    source: Optional[str] = None

    # --- lookups ---------------------------------------------------------
    def by_name(self, name: str) -> Optional[Device]:
        for d in self.devices:
            if d.name.lower() == name.lower():
                return d
        return None

    def by_mac(self, mac: Optional[str]) -> Optional[Device]:
        n = norm_mac(mac)
        if not n:
            return None
        for d in self.devices:
            if n in d.macs:
                return d
        return None

    def by_ip(self, ip: Optional[str]) -> Optional[Device]:
        if not ip:
            return None
        for d in self.devices:
            for i in d.interfaces:
                if i.ipv4 == ip:
                    return d
        return None

    def by_mac_prefix(self, mac: Optional[str], octets: int = 3) -> Optional[Device]:
        """Match on the first ``octets`` bytes only.

        A switch's LLDP chassis-id is the bridge base address, which is
        usually -- but not always -- exactly the management MAC listed in
        SYSTEM.md. Ports of the same switch commonly differ in the last
        octet. This is used only as a *fallback* and the result is flagged
        as such by the caller.
        """
        n = norm_mac(mac)
        if not n:
            return None
        prefix = n.split(":")[:octets]
        for d in self.devices:
            for m in d.macs:
                if m.split(":")[:octets] == prefix:
                    return d
        return None

    @property
    def switches(self) -> List[Device]:
        return [d for d in self.devices if d.role == "switch"]

    @property
    def endpoints(self) -> List[Device]:
        return [d for d in self.devices if d.role == "endpoint"]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "devices": [d.to_dict() for d in self.devices],
        }


def classify(name: str) -> str:
    return "switch" if SWITCH_NAME_RE.match(name) else "endpoint"


def parse_system_md(path: str) -> Inventory:
    """Parse the address table out of SYSTEM.md.

    Tolerant by design: any line carrying an IPv4 address is considered a
    table row, everything else is ignored. This survives reformatting of
    the surrounding prose, which is the realistic failure mode.
    """
    inv = Inventory(source=path)
    current: Optional[Device] = None

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            ip_match = IPV4_RE.search(line)
            if not ip_match:
                continue

            # Anything before the IP address on the line is the device name;
            # if that is empty the line continues the previous device.
            head = line[: ip_match.start()]
            name_match = NAME_RE.match(head.strip())
            mac_match = MAC_RE.search(line)

            iface = Interface(
                ipv4=ip_match.group(1),
                mac=norm_mac(mac_match.group(1)) if mac_match else None,
            )

            if name_match:
                name = name_match.group(1)
                existing = inv.by_name(name)
                if existing is not None:
                    current = existing
                else:
                    current = Device(name=name, role=classify(name))
                    inv.devices.append(current)
                current.interfaces.append(iface)
            elif current is not None:
                current.interfaces.append(iface)
            # a continuation line with no preceding device is discarded

    return inv


def switch_targets(inv: Inventory, override: Optional[List[str]] = None) -> List[dict]:
    """Produce the list of NETCONF targets to contact.

    ``override`` is a list of ``ip`` or ``name=ip`` strings from the command
    line; when given it replaces the inventory-derived list entirely, so a
    single switch can be probed without editing SYSTEM.md.
    """
    if override:
        targets = []
        for i, item in enumerate(override, start=1):
            if "=" in item:
                name, ip = item.split("=", 1)
            else:
                name, ip = f"SW{i}", item
            targets.append({"name": name.strip(), "host": ip.strip()})
        return targets

    targets = []
    for d in inv.switches:
        ip = d.primary_ip
        if not ip:
            continue
        targets.append({"name": d.name, "host": ip})
    return targets
