#!/usr/bin/env python3
"""Generate a SYNTHETIC raw-capture set for testing the discovery pipeline.

There is no way to unit-test a NETCONF parser against hardware that is on
someone else's bench. This generator produces raw XML in the exact shape the
Kontron AN001 v1.2 application note documents -- same namespaces, same
element names, same default values (``supported-list-max`` 128,
``supported-cycle-max`` 33538048/1000000000, ``admin-gate-states`` 255,
port names ``Gi 1/1..1/6`` and ``2.5G 1/1..1/2``) -- plus LLDP neighbour
entries, which the application note does not show an example of.

**Everything it writes is fabricated.** Each generated run directory gets a
``SYNTHETIC`` marker file, and the run id is prefixed ``synthetic-``, so a
fixture run can never be mistaken for a measurement. This mirrors the
interlock in ``i226-adaptation/analysis/make_fixture.py``.

Usage:
    python3 tests/make_fixture.py --out results/synthetic-001
    python3 -m tsn_discovery.cli --reanalyse results/synthetic-001 \
            --inventory ../SYSTEM.md --no-publish
"""

from __future__ import annotations

import argparse
import json
import os

NS = {
    "if": "urn:ietf:params:xml:ns:yang:ietf-interfaces",
    "sys": "urn:ietf:params:xml:ns:yang:ietf-system",
    "dot1q": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-bridge",
    "sched": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-sched",
    "preempt": "urn:ieee:std:802.1Q:yang:ieee802-dot1q-preemption",
    "lldp": "urn:ieee:std:802.1AB:yang:ieee802-dot1ab-lldp",
    "eth": "urn:ieee:std:802.3:yang:ieee802-ethernet-interface",
    "mon": "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring",
    "ianaift": "urn:ietf:params:xml:ns:yang:iana-if-type",
}

MODULES = [
    ("iana-if-type", "2017-01-19", NS["ianaift"]),
    ("ietf-system", "2014-08-06", NS["sys"]),
    ("ietf-interfaces", "2018-02-20", NS["if"]),
    ("ietf-ip", "2018-02-22", "urn:ietf:params:xml:ns:yang:ietf-ip"),
    ("ietf-yang-types", "2013-07-15",
     "urn:ietf:params:xml:ns:yang:ietf-yang-types"),
    ("ietf-netconf-monitoring", "2010-10-04", NS["mon"]),
    ("ieee802-dot1ab-lldp", "2018-11-13", NS["lldp"]),
    ("ieee802-dot1ab-types", "2018-10-03",
     "urn:ieee:std:802.1AB:yang:ieee802-dot1ab-types"),
    ("ieee802-dot1q-bridge", "2020-02-15", NS["dot1q"]),
    ("ieee802-dot1q-preemption", "2018-09-10", NS["preempt"]),
    ("ieee802-dot1q-sched", "2020-02-20", NS["sched"]),
    ("ieee802-ethernet-interface", "2019-06-21", NS["eth"]),
]

BASE_CAPS = [
    "urn:ietf:params:netconf:base:1.0",
    "urn:ietf:params:netconf:base:1.1",
    "urn:ietf:params:netconf:capability:writable-running:1.0",
    "urn:ietf:params:netconf:capability:candidate:1.0",
    "urn:ietf:params:netconf:capability:confirmed-commit:1.1",
    "urn:ietf:params:netconf:capability:rollback-on-error:1.0",
    "urn:ietf:params:netconf:capability:validate:1.1",
    "urn:ietf:params:netconf:capability:startup:1.0",
    "urn:ietf:params:netconf:capability:xpath:1.0",
    "urn:ietf:params:netconf:capability:notification:1.0",
    "urn:ietf:params:netconf:capability:interleave:1.0",
    "urn:ietf:params:netconf:capability:with-defaults:1.0",
]

PORTS = [f"Gi 1/{n}" for n in range(1, 7)] + ["2.5G 1/1", "2.5G 1/2"]

# Fabricated testbed: a chain with a spur, endpoints on edge ports.
#   SW1 2.5G1/1 -- 2.5G1/2 SW2 2.5G1/1 -- 2.5G1/2 SW3
#                  SW2 Gi1/6 -- Gi1/5 SW4 2.5G1/1 -- 2.5G1/2 SW5
SWITCHES = {
    "SW1": {"ip": "192.168.1.10", "mac": "00:80:82:b9:65:33", "host": "kswitch-1"},
    "SW2": {"ip": "192.168.1.11", "mac": "00:80:82:bd:25:7c", "host": "kswitch-2"},
    "SW3": {"ip": "192.168.1.12", "mac": "00:80:82:bd:25:a2", "host": "kswitch-3"},
    "SW4": {"ip": "192.168.1.13", "mac": "00:80:82:bd:25:80", "host": "kswitch-4"},
    "SW5": {"ip": "192.168.1.14", "mac": "00:80:82:bd:25:72", "host": "kswitch-5"},
}

LINKS = [
    ("SW1", "2.5G 1/1", "SW2", "2.5G 1/2"),
    ("SW2", "2.5G 1/1", "SW3", "2.5G 1/2"),
    ("SW2", "Gi 1/6", "SW4", "Gi 1/5"),
    ("SW4", "2.5G 1/1", "SW5", "2.5G 1/2"),
]

# endpoint MAC -> (switch, port); taken from SYSTEM.md so the cross-check
# has something to resolve.
ENDPOINTS = {
    "00:07:32:c1:43:30": ("SW1", "Gi 1/1"),   # UP-1 stream
    "00:07:32:c1:43:31": ("SW1", "Gi 1/2"),   # UP-1 background
    "00:07:32:c1:29:69": ("SW3", "Gi 1/1"),   # UP-2 stream
    "00:07:32:c1:29:6a": ("SW3", "Gi 1/2"),   # UP-2 background
    "88:a2:9e:4b:97:1b": ("SW5", "Gi 1/1"),   # RPI1
    "88:a2:9e:a6:d1:5f": ("SW5", "Gi 1/2"),   # RPI2
    "00:bb:cc:dd:ee:12": ("SW4", "Gi 1/1"),   # CTRL
    "00:bb:cc:dd:ee:13": ("SW4", "Gi 1/2"),   # IO-D0
}


def dashed(mac: str) -> str:
    return mac.upper().replace(":", "-")


def wrap(payload: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" '
            'message-id="1">\n<data>\n' + payload + "\n</data>\n</rpc-reply>\n")


def gate_parameters(port: str, scheduled: bool) -> str:
    """A Qbv gate-parameters block. One port per switch carries a real
    8-entry gate control list, exactly as AN001 §gate-parameters shows."""
    common = (
        "          <admin-cycle-time-extension>256</admin-cycle-time-extension>\n"
        "          <admin-base-time><seconds>0</seconds>"
        "<nanoseconds>0</nanoseconds></admin-base-time>\n"
        "          <supported-list-max>128</supported-list-max>\n"
        "          <supported-cycle-max><numerator>33538048</numerator>"
        "<denominator>1000000000</denominator></supported-cycle-max>\n"
        "          <supported-interval-max>33538048</supported-interval-max>\n"
    )
    if not scheduled:
        return (
            f'        <gate-parameters xmlns="{NS["sched"]}">\n'
            "          <gate-enabled>false</gate-enabled>\n"
            "          <admin-gate-states>255</admin-gate-states>\n"
            "          <admin-cycle-time><numerator>100</numerator>"
            "<denominator>1000</denominator></admin-cycle-time>\n"
            "          <config-change>false</config-change>\n"
            + common +
            "        </gate-parameters>\n")

    entries = [(0, 32, 300000), (1, 16, 200000), (2, 32, 300000),
               (3, 72, 200000), (4, 32, 300000), (5, 131, 200000),
               (6, 32, 300000), (7, 0, 200000)]
    gcl = "".join(
        "            <gate-control-entry>\n"
        f"              <index>{i}</index>\n"
        f'              <operation-name xmlns:sched="{NS["sched"]}">'
        "sched:set-gate-states</operation-name>\n"
        f"              <gate-state-value>{g}</gate-state-value>\n"
        f"              <time-interval-value>{t}</time-interval-value>\n"
        "            </gate-control-entry>\n"
        for i, g, t in entries)
    return (
        f'        <gate-parameters xmlns="{NS["sched"]}">\n'
        "          <gate-enabled>true</gate-enabled>\n"
        "          <admin-gate-states>255</admin-gate-states>\n"
        "          <admin-cycle-time><numerator>2000000</numerator>"
        "<denominator>1000000000</denominator></admin-cycle-time>\n"
        "          <config-change>true</config-change>\n"
        "          <admin-control-list-length>8</admin-control-list-length>\n"
        "          <admin-control-list>\n" + gcl + "          </admin-control-list>\n"
        + common +
        "        </gate-parameters>\n")


def preemption(port: str) -> str:
    return (
        f'        <frame-preemption-parameters xmlns="{NS["preempt"]}">\n'
        "          <frame-preemption-status-table>\n"
        "            <priority0>preemptable</priority0>\n"
        "            <priority1>preemptable</priority1>\n"
        "            <priority2>preemptable</priority2>\n"
        "            <priority3>express</priority3>\n"
        "            <priority4>express</priority4>\n"
        "            <priority5>express</priority5>\n"
        "            <priority6>express</priority6>\n"
        "            <priority7>express</priority7>\n"
        "          </frame-preemption-status-table>\n"
        "          <hold-advance>12000</hold-advance>\n"
        "          <release-advance>7000</release-advance>\n"
        "          <preemption-active>false</preemption-active>\n"
        "          <hold-request>release</hold-request>\n"
        "        </frame-preemption-parameters>\n")


def interfaces_xml(sw: str) -> str:
    scheduled_port = "2.5G 1/1"
    body = [f'  <interfaces xmlns="{NS["if"]}">']
    for idx, port in enumerate(PORTS, start=1):
        speed = 2_500_000_000 if port.startswith("2.5G") else 1_000_000_000
        body.append(
            "    <interface>\n"
            f"      <name>{port}</name>\n"
            f'      <type xmlns:ianaift="{NS["ianaift"]}">'
            "ianaift:ethernetCsmacd</type>\n"
            f"      <if-index>{idx}</if-index>\n"
            "      <enabled>true</enabled>\n"
            "      <oper-status>up</oper-status>\n"
            f"      <speed>{speed}</speed>\n"
            f'      <ethernet xmlns="{NS["eth"]}">\n'
            "        <duplex>full</duplex>\n"
            "        <auto-negotiation><enable>true</enable></auto-negotiation>\n"
            "        <max-frame-length>9600</max-frame-length>\n"
            "      </ethernet>\n"
            f'      <bridge-port xmlns="{NS["dot1q"]}">\n'
            "        <component-name>bridge0</component-name>\n"
            f"        <port-number>{idx}</port-number>\n"
            "        <pvid>1</pvid>\n"
            "        <default-priority>0</default-priority>\n"
            "        <acceptable-frame-types>admit-all-frames"
            "</acceptable-frame-types>\n"
            "        <enable-ingress-filtering>false</enable-ingress-filtering>\n"
            + gate_parameters(port, port == scheduled_port)
            + preemption(port) +
            "      </bridge-port>\n"
            "    </interface>")
    body.append("    <interface><name>bridge0</name></interface>")
    body.append("  </interfaces>")
    return "\n".join(body)


def bridges_xml(sw: str) -> str:
    mac = SWITCHES[sw]["mac"]
    fdb = []
    for emac, (esw, eport) in ENDPOINTS.items():
        if esw != sw:
            continue
        ref = PORTS.index(eport) + 1
        fdb.append(
            "          <filtering-entry>\n"
            "            <database-id>1</database-id>\n"
            "            <vids>1</vids>\n"
            f"            <address>{dashed(emac)}</address>\n"
            "            <entry-type>dynamic</entry-type>\n"
            "            <status>learned</status>\n"
            f"            <port-map><port-ref>{ref}</port-ref></port-map>\n"
            "          </filtering-entry>\n")
    return (
        f'  <bridges xmlns="{NS["dot1q"]}">\n'
        "    <bridge>\n"
        "      <name>bridge0</name>\n"
        f"      <address>{dashed(mac)}</address>\n"
        f'      <bridge-type xmlns:dot1q="{NS["dot1q"]}">'
        "dot1q:two-port-mac-relay-bridge</bridge-type>\n"
        "      <ports>8</ports>\n"
        "      <up-time>1539</up-time>\n"
        "      <component>\n"
        "        <name>bridge0</name>\n"
        "        <id>1</id>\n"
        f'        <type xmlns:dot1q="{NS["dot1q"]}">'
        "dot1q:c-vlan-component</type>\n"
        "        <filtering-database>\n"
        "          <static-entries>0</static-entries>\n"
        f"          <dynamic-entries>{len(fdb)}</dynamic-entries>\n"
        "          <aging-time>300</aging-time>\n"
        + "".join(fdb) +
        "        </filtering-database>\n"
        "        <bridge-vlan>\n"
        "          <vlan><vid>1</vid><name>default</name></vlan>\n"
        "          <vlan><vid>100</vid><name>tsn-stream</name></vlan>\n"
        "        </bridge-vlan>\n"
        "      </component>\n"
        "    </bridge>\n"
        "  </bridges>")


def lldp_xml(sw: str) -> str:
    mac = SWITCHES[sw]["mac"]
    per_port = {}
    for a, ap, b, bp in LINKS:
        if a == sw:
            per_port.setdefault(ap, []).append((b, bp))
        if b == sw:
            per_port.setdefault(bp, []).append((a, ap))

    body = [f'  <lldp xmlns="{NS["lldp"]}">',
            "    <chassis-id-subtype>mac-address</chassis-id-subtype>",
            f"    <chassis-id>{dashed(mac)}</chassis-id>",
            f'    <system-name>{SWITCHES[sw]["host"]}</system-name>',
            "    <message-tx-interval>30</message-tx-interval>",
            "    <message-tx-hold-multiplier>4</message-tx-hold-multiplier>"]
    for idx, port in enumerate(PORTS, start=1):
        body.append("    <port>")
        body.append(f"      <name>{port}</name>")
        body.append("      <dest-mac-address>01-80-C2-00-00-0E</dest-mac-address>")
        body.append("      <admin-status>tx-and-rx</admin-status>")
        body.append("      <port-id-subtype>interface-name</port-id-subtype>")
        body.append(f"      <port-id>{port}</port-id>")
        for n, (peer, peer_port) in enumerate(per_port.get(port, []), start=1):
            body.append(
                "      <remote-systems-data>\n"
                "        <time-mark>0</time-mark>\n"
                f"        <remote-index>{n}</remote-index>\n"
                "        <chassis-id-subtype>mac-address</chassis-id-subtype>\n"
                f'        <chassis-id>{dashed(SWITCHES[peer]["mac"])}</chassis-id>\n'
                "        <port-id-subtype>interface-name</port-id-subtype>\n"
                f"        <port-id>{peer_port}</port-id>\n"
                f"        <port-desc>{peer_port}</port-desc>\n"
                f'        <system-name>{SWITCHES[peer]["host"]}</system-name>\n'
                "        <system-description>Kontron KSwitch D10 MMT "
                "(SYNTHETIC FIXTURE)</system-description>\n"
                "        <management-address>\n"
                "          <address-subtype>ipv4</address-subtype>\n"
                f'          <address>{SWITCHES[peer]["ip"]}</address>\n'
                "        </management-address>\n"
                "      </remote-systems-data>")
        body.append("    </port>")
    body.append("  </lldp>")
    return "\n".join(body)


def system_xml(sw: str) -> str:
    return (f'  <system xmlns="{NS["sys"]}">\n'
            "    <contact>SYNTHETIC FIXTURE — not a real device</contact>\n"
            f'    <hostname>{SWITCHES[sw]["host"]}</hostname>\n'
            "    <location>lab bench (fixture)</location>\n"
            "  </system>")


def schemas_xml() -> str:
    rows = "".join(
        "      <schema>\n"
        f"        <identifier>{name}</identifier>\n"
        f"        <version>{rev}</version>\n"
        "        <format>yang</format>\n"
        f"        <namespace>{ns}</namespace>\n"
        "        <location>NETCONF</location>\n"
        "      </schema>\n"
        for name, rev, ns in MODULES)
    return (f'  <netconf-state xmlns="{NS["mon"]}">\n'
            "    <schemas>\n" + rows + "    </schemas>\n"
            "  </netconf-state>")


def hello_caps() -> list:
    caps = list(BASE_CAPS)
    for name, rev, ns in MODULES:
        caps.append(f"{ns}?module={name}&revision={rev}")
    return caps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="results/synthetic-fixture",
                    help="run directory to create")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.out)
    raw = os.path.join(run_dir, "raw")
    os.makedirs(raw, exist_ok=True)

    with open(os.path.join(run_dir, "SYNTHETIC"), "w", encoding="utf-8") as fh:
        fh.write(
            "Every file in this run directory is FABRICATED by "
            "tests/make_fixture.py.\n"
            "No switch was contacted. Do not cite anything here as a "
            "property of the testbed.\n")

    for sw in SWITCHES:
        d = os.path.join(raw, sw)
        os.makedirs(d, exist_ok=True)
        for key, payload in (
            ("system", system_xml(sw)),
            ("interfaces", interfaces_xml(sw)),
            ("bridges", bridges_xml(sw)),
            ("lldp", lldp_xml(sw)),
            ("netconf-state-schemas", schemas_xml()),
        ):
            with open(os.path.join(d, f"{key}.xml"), "w", encoding="utf-8") as fh:
                fh.write(wrap(payload))
        with open(os.path.join(d, "hello-capabilities.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"host": SWITCHES[sw]["ip"],
                       "session_id": "fixture",
                       "capabilities": hello_caps()}, fh, indent=2)

    print(f"SYNTHETIC fixture written to {run_dir}")
    print("Analyse it with:")
    print(f"  python3 -m tsn_discovery.cli --reanalyse {args.out} "
          "--inventory ../SYSTEM.md --no-publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
