"""Human-readable renderings of the discovered topology and capabilities.

Three outputs:

* ``topology.md``   -- the topology file the subproject is asked to produce:
                      node and link tables, a Mermaid diagram that renders
                      in the repository viewer, and the SYSTEM.md cross-check.
* ``topology.dot``  -- Graphviz source, for ``dot -Tpng``.
* ``capabilities.md`` -- the per-switch capability summary in prose form.

Everything here reads from the JSON documents, never from the switches, so
the renderings can be regenerated offline from a saved run.
"""

from __future__ import annotations

from typing import Dict, List, Optional

BAR = "-" * 74


def _fmt_ns(value: Optional[int]) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value/1_000_000:g} ms"
    if value >= 1_000:
        return f"{value/1_000:g} us"
    return f"{value} ns"


def _safe(node_id: str) -> str:
    """Mermaid node identifiers cannot contain punctuation."""
    return "".join(ch if ch.isalnum() else "_" for ch in node_id)


def topology_markdown(topo: dict, run_meta: dict, inventory_path: str) -> str:
    lines: List[str] = []
    A = lines.append

    A("# Discovered topology — FlexTest TSN testbed")
    A("")
    A(f"Generated {run_meta.get('generated_utc')} by "
      f"`tsn_discovery` {run_meta.get('tool_version')} "
      f"(run `{run_meta.get('run_id')}`).")
    A("")
    A("**This file is generated. Do not edit it by hand** — re-run "
      "`scripts/run_discovery.sh` instead.")
    A("")
    A("Method: switch-to-switch links come from IEEE 802.1AB LLDP "
      "(`remote-systems-data`) read over NETCONF. Endpoint attachments come "
      "from the IEEE 802.1Q filtering database, and only on ports that LLDP "
      "has not already claimed as an inter-switch link. Names are resolved "
      f"against `{inventory_path}`, which is used as a cross-check oracle and "
      "never as a source of links.")
    A("")

    counts = topo["link_counts"]
    cc = topo["crosscheck"]
    A("## 1. Summary")
    A("")
    A(f"- Nodes: **{len(topo['nodes'])}** "
      f"({sum(1 for n in topo['nodes'] if n['role']=='switch')} switches, "
      f"{sum(1 for n in topo['nodes'] if n['role']=='endpoint')} endpoints, "
      f"{sum(1 for n in topo['nodes'] if n['role']=='unknown')} unidentified)")
    A(f"- Links: **{counts['total']}** "
      f"({counts['lldp']} from LLDP, of which "
      f"{counts['lldp_confirmed_bidirectional']} confirmed from both ends; "
      f"{counts['fdb_attachments']} endpoint attachments from the FDB)")
    A(f"- Cross-check against the inventory: "
      f"**{'agrees' if cc['agreement'] else 'DISAGREES — see §5'}**")
    A("")

    A("## 2. Diagram")
    A("")
    A("```mermaid")
    A("graph LR")
    for n in topo["nodes"]:
        if not n["discovered"] and not n["in_inventory"]:
            continue
        nid = _safe(n["id"])
        if n["role"] == "switch":
            sub = " / ".join(x for x in (n.get("hostname"), n.get("mgmt_ip")) if x)
            A(f'  {nid}["{n["label"]}<br/>{sub}"]')
        elif n["role"] == "endpoint":
            A(f'  {nid}("{n["label"]}")')
        else:
            A(f'  {nid}{{{{"{n["label"]}"}}}}')
    for link in topo["links"]:
        a, b = link["a"], link["b"]
        label = f'{a["port"] or "?"} — {b["port"] or "?"}'
        arrow = "---" if link["method"] == "lldp" else "-.-"
        A(f'  {_safe(a["node"])} {arrow}|"{label}"| {_safe(b["node"])}')
    A("```")
    A("")
    A("Solid edges are LLDP-discovered links; dotted edges are endpoint "
      "attachments inferred from the filtering database.")
    A("")

    A("## 3. Links")
    A("")
    if topo["links"]:
        A("| A | A port | B | B port | Method | Both ends | Resolution |")
        A("|---|---|---|---|---|---|---|")
        for l in topo["links"]:
            A(f'| {l["a"]["node"]} | `{l["a"]["port"] or "-"}` '
              f'| {l["b"]["node"]} | `{l["b"]["port"] or "-"}` '
              f'| {l["method"]} '
              f'| {"yes" if l.get("confirmed_bidirectional") else "no"} '
              f'| {l.get("resolution","-")} |')
    else:
        A("_No links discovered._")
    A("")

    A("## 4. Nodes")
    A("")
    A("| Node | Role | Hostname | Management IP | Discovered | In SYSTEM.md | MACs |")
    A("|---|---|---|---|---|---|---|")
    for n in topo["nodes"]:
        macs = ", ".join(f"`{m}`" for m in n["macs"][:3]) or "-"
        if len(n["macs"]) > 3:
            macs += f" (+{len(n['macs'])-3})"
        A(f'| {n["label"]} | {n["role"]} | {n.get("hostname") or "-"} '
          f'| {n.get("mgmt_ip") or "-"} '
          f'| {"yes" if n["discovered"] else "no"} '
          f'| {"yes" if n["in_inventory"] else "no"} | {macs} |')
    A("")

    A("## 5. Cross-check against the inventory")
    A("")
    A(f"- Devices in the inventory: {cc['inventory_devices']}")
    A(f"- Devices seen by discovery: {cc['discovered_devices']}")
    if cc["switches_unreachable"]:
        A(f"- **Switches not reachable over NETCONF:** "
          f"{', '.join(cc['switches_unreachable'])}")
    if cc["switches_in_inventory_not_discovered"]:
        A(f"- **Switches in SYSTEM.md that discovery did not see:** "
          f"{', '.join(cc['switches_in_inventory_not_discovered'])}")
    if cc["endpoints_in_inventory_not_observed"]:
        A(f"- Endpoints in SYSTEM.md not observed: "
          f"{', '.join(cc['endpoints_in_inventory_not_observed'])}")
        A("  (not necessarily a fault: an end station that neither runs LLDP "
          "nor has transmitted recently has no filtering-database entry and "
          "is invisible to both discovery methods. Generate traffic from it, "
          "or shorten the FDB aging time, and re-run.)")
    if cc["discovered_but_not_in_inventory"]:
        A(f"- **Discovered but not in SYSTEM.md:** "
          f"{', '.join(cc['discovered_but_not_in_inventory'])}")
        A("  (either an undocumented device on the network, or a name/MAC in "
          "SYSTEM.md that no longer matches the hardware)")
    if cc["unresolved_lldp_neighbours"]:
        A(f"- Unresolved LLDP neighbours: {cc['unresolved_lldp_neighbours']} "
          "(see `topology.json`)")
    if cc["agreement"]:
        A("- No discrepancies.")
    A("")

    if topo.get("notes"):
        A("## 6. Notes from the run")
        A("")
        for note in topo["notes"]:
            A(f"- {note}")
        A("")

    return "\n".join(lines) + "\n"


def topology_dot(topo: dict) -> str:
    lines = ["graph tsn_topology {", '  rankdir=LR;',
             '  node [fontname="Helvetica"];',
             '  edge [fontname="Helvetica", fontsize=9];']
    for n in topo["nodes"]:
        if not n["discovered"] and not n["in_inventory"]:
            continue
        if n["role"] == "switch":
            shape = 'shape=box, style="rounded,filled", fillcolor="#d8e6f3"'
        elif n["role"] == "endpoint":
            shape = 'shape=ellipse, style=filled, fillcolor="#e8f3d8"'
        else:
            shape = 'shape=diamond, style=filled, fillcolor="#f3e6d8"'
        label = n["label"]
        if n.get("mgmt_ip"):
            label += f'\\n{n["mgmt_ip"]}'
        lines.append(f'  "{n["id"]}" [label="{label}", {shape}];')
    for l in topo["links"]:
        style = "solid" if l["method"] == "lldp" else "dashed"
        lines.append(
            f'  "{l["a"]["node"]}" -- "{l["b"]["node"]}" '
            f'[label="{l["a"]["port"] or "?"} / {l["b"]["port"] or "?"}", '
            f'style={style}];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def capabilities_markdown(records: Dict[str, dict], cnc_doc: dict,
                          run_meta: dict) -> str:
    lines: List[str] = []
    A = lines.append

    A("# Switch capabilities — FlexTest TSN testbed")
    A("")
    A(f"Generated {run_meta.get('generated_utc')} by `tsn_discovery` "
      f"{run_meta.get('tool_version')} (run `{run_meta.get('run_id')}`).")
    A("")
    A("**This file is generated.** Every value below came from a NETCONF "
      "reply captured in `raw/`; nothing is inferred from documentation.")
    A("")

    A("## 1. Reachability and NETCONF")
    A("")
    A("| Switch | Host | Reachable | Session | YANG modules | :candidate | "
      ":writable-running | :xpath |")
    A("|---|---|---|---|---|---|---|---|")
    for name, rec in sorted(records.items()):
        nc = rec.get("netconf", {}) or {}
        A(f'| {name} | {rec.get("host")} '
          f'| {"yes" if rec.get("reachable") else "**no**"} '
          f'| {nc.get("session_id") or "-"} '
          f'| {len(rec.get("yang_modules", {}))} '
          f'| {"yes" if nc.get("supports_candidate") else "no"} '
          f'| {"yes" if nc.get("supports_writable_running") else "no"} '
          f'| {"yes" if nc.get("supports_xpath") else "no"} |')
    A("")

    A("## 2. TSN feature support")
    A("")
    A("Derived at runtime from each switch's `<hello>` capability list and "
      "`/ietf-netconf-monitoring:netconf-state/schemas`.")
    A("")
    feature_keys = ["bridge", "qbv", "qbu", "qci", "qav", "qcc", "lldp", "ptp"]
    header = "| Switch | " + " | ".join(k.upper() for k in feature_keys) + " |"
    A(header)
    A("|" + "---|" * (len(feature_keys) + 1))
    for name, rec in sorted(records.items()):
        if not rec.get("reachable"):
            A(f"| {name} | " + " | ".join(["n/a"] * len(feature_keys)) + " |")
            continue
        cells = []
        for k in feature_keys:
            sup = rec.get("features", {}).get(k, {}).get("supported")
            cells.append("yes" if sup else "**no**")
        A(f"| {name} | " + " | ".join(cells) + " |")
    A("")

    ptp_any = cnc_doc["network_wide_support"]["ptp"]["any_bridge"]
    if not ptp_any:
        A("> **PTP is not exposed over NETCONF on this hardware.** No switch "
          "implements a PTP or gPTP YANG module. Qbv base times can be "
          "written, but the time base they refer to cannot be read or "
          "verified through NETCONF. See REPORT.md §4.")
        A("")

    A("## 3. Qbv (802.1Qbv time-aware shaper) capability envelope")
    A("")
    env = cnc_doc["network_capability_envelope"]
    A(f"- Maximum gate-control-list entries per port: "
      f"**{env['min_supported_gcl_entries_max'] or 'unknown'}**")
    A(f"- Maximum cycle time: "
      f"**{_fmt_ns(env['min_supported_cycle_time_max_ns'])}**")
    A(f"- Maximum single interval: "
      f"**{_fmt_ns(env['min_supported_interval_max_ns'])}**")
    A(f"- Traffic classes: **{env['traffic_classes']}**")
    A("")
    A("These are the smallest limits found across every reachable bridge "
      "port. A schedule that fits inside them is installable network-wide.")
    A("")

    A("## 4. Per-port state")
    A("")
    for name, rec in sorted(records.items()):
        if not rec.get("reachable"):
            A(f"### {name} — not reachable")
            A("")
            for err in rec.get("errors", []):
                A(f"- `{err['scope']}`: {err['kind']} — {err['message']}")
            A("")
            continue

        sysinfo = rec.get("system", {}) or {}
        bridge0 = (rec.get("bridges") or [{}])[0]
        A(f"### {name} ({rec.get('host')})")
        A("")
        A(f"- Hostname: `{sysinfo.get('hostname') or '-'}`, "
          f"location: `{sysinfo.get('location') or '-'}`")
        A(f"- Bridge `{bridge0.get('name') or '-'}` "
          f"address `{bridge0.get('address') or '-'}`, "
          f"type `{bridge0.get('bridge_type') or '-'}`, "
          f"{bridge0.get('ports') or '?'} ports, "
          f"up {bridge0.get('up_time_s') or '?'} s")
        vlans = rec.get("summary", {}).get("vlans", [])
        A(f"- VLANs: {', '.join(str(v) for v in vlans) if vlans else 'none'}")
        A("")
        A("| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | "
          "Cycle | GCL | Qbu | Preemptable |")
        A("|---|---|---|---|---|---|---|---|---|---|---|")
        for i in rec.get("interfaces", []):
            if not i.get("is_bridge_port"):
                continue
            bp = i.get("bridge_port") or {}
            qbv = i.get("qbv", {})
            qbu = i.get("qbu", {})
            cfg = qbv.get("configuration", {}) if qbv.get("present") else {}
            qcfg = qbu.get("configuration", {}) if qbu.get("present") else {}
            A(f'| `{i["name"]}` | {i.get("if_index") or "-"} '
              f'| {i.get("speed_mbps") or "-"} '
              f'| {bp.get("pvid") or "-"} '
              f'| {i.get("oper_status") or "-"} '
              f'| {"yes" if qbv.get("present") else "no"} '
              f'| {"yes" if cfg.get("gate_enabled") else "no"} '
              f'| {_fmt_ns(cfg.get("admin_cycle_time_ns"))} '
              f'| {len(cfg.get("admin_control_list", []) or [])} '
              f'| {"yes" if qbu.get("present") else "no"} '
              f'| {", ".join(qcfg.get("preemptable_priorities", []) or []) or "-"} |')
        A("")
        if rec.get("warnings"):
            A("**Warnings** (affect a port whose gate is enabled):")
            A("")
            for w in rec["warnings"]:
                A(f"- {w}")
            A("")
        if rec.get("notes"):
            A("**Notes** (inert — gate disabled on those ports):")
            A("")
            for n in rec["notes"]:
                A(f"- {n}")
            A("")

    A("## 5. Gaps for a Centralized Network Configuration entity")
    A("")
    for gap in cnc_doc["gaps"]:
        A(f"### `{gap['capability']}` — {gap['impact']}")
        A("")
        A(gap["detail"])
        if gap.get("workaround"):
            A("")
            A(f"*Workaround:* {gap['workaround']}")
        A("")

    return "\n".join(lines) + "\n"


def console_summary(records: Dict[str, dict], topo: dict,
                    cnc_doc: dict) -> str:
    out: List[str] = [BAR, "DISCOVERY SUMMARY", BAR]
    ok = sum(1 for r in records.values() if r.get("reachable"))
    out.append(f"Switches contacted : {ok}/{len(records)}")
    for name, rec in sorted(records.items()):
        if rec.get("reachable"):
            s = rec.get("summary", {})
            out.append(
                f"  {name:<6} {rec.get('host'):<15} "
                f"{len(rec.get('yang_modules', {})):>3} modules, "
                f"{s.get('bridge_port_count', 0)} bridge ports, "
                f"Qbv on {len(s.get('qbv_capable_ports', []))}, "
                f"Qbu on {len(s.get('qbu_capable_ports', []))}")
        else:
            err = (rec.get("errors") or [{}])[0]
            out.append(f"  {name:<6} {rec.get('host'):<15} "
                       f"UNREACHABLE ({err.get('kind', 'unknown')})")
    c = topo["link_counts"]
    out.append(f"Links              : {c['total']} "
               f"({c['lldp']} LLDP / {c['fdb_attachments']} FDB)")
    cc = topo["crosscheck"]
    out.append(f"Inventory agreement: "
               f"{'yes' if cc['agreement'] else 'NO — see topology.md §5'}")
    ptp = cnc_doc["network_wide_support"]["ptp"]["any_bridge"]
    out.append(f"PTP over NETCONF   : {'available' if ptp else 'NOT EXPOSED'}")
    out.append(f"CNC gaps recorded  : {len(cnc_doc['gaps'])}")
    out.append(BAR)
    return "\n".join(out)
