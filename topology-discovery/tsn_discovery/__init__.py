"""Automated NETCONF/YANG topology discovery and TSN capability retrieval
for the FlexTest TSN testbed (Kontron KSwitch D10 MMT).

Package layout:

    inventory     parse SYSTEM.md into a device inventory (names, IPs, MACs)
    netconf       ncclient session wrapper; raw XML capture; never raises
                  through to the caller
    xmlutil       namespace-agnostic XML helpers (everything is matched on
                  local-name, because vendor namespace prefixes vary)
    probe         runtime YANG-module / capability probe per switch
    capabilities  extract system, interfaces, VLAN, Qbv, Qbu, QoS, PTP
    topology      build the network graph from LLDP + FDB + inventory
    cnc           normalise everything into a CNC input document
    render        human-readable topology (Markdown + Mermaid + Graphviz)
    cli           orchestration and command-line entry point

Design rule: discovery is *evidence-based*. Nothing is reported as a switch
capability unless the switch itself said so in a NETCONF reply, and every
reported item carries the source it came from.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = "tsn-testbed/discovery/v1"
