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
    A("Method: switch-to-switch links come from IEEE 802.1AB LLDP — "
      "`remote-systems-data` over NETCONF, or `show lldp neighbors` where a "
      "switch fell back to the CLI. Endpoint attachments come from the IEEE "
      "802.1Q filtering database, and only on ports that LLDP has not already "
      "claimed as an inter-switch link. Names are resolved against "
      f"`{inventory_path}`, which is used as a cross-check oracle and never as "
      "a source of links.")
    A("")
    used = run_meta.get("transports_used") or {}
    if len(used) > 1 or "cli" in used:
        A("Transports this run: "
          + "; ".join(f"**{t}** on {', '.join(names)}"
                      for t, names in sorted(used.items()))
          + ". See `capabilities.md` §1 for why.")
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
        A(f"- **Switches not reachable by any transport:** "
          f"{', '.join(cc['switches_unreachable'])}")
    if cc["switches_in_inventory_not_discovered"]:
        A(f"- **Switches in SYSTEM.md that discovery did not see:** "
          f"{', '.join(cc['switches_in_inventory_not_discovered'])}")
    if cc["endpoints_in_inventory_not_observed"]:
        missing = cc["endpoints_in_inventory_not_observed"]
        A(f"- Endpoints in SYSTEM.md not observed: {', '.join(missing)}")
        A("  (not necessarily a fault: an end station that neither runs LLDP "
          "nor has transmitted recently has no filtering-database entry and "
          "is invisible to both discovery methods. Generate traffic from it, "
          "or shorten the FDB aging time, and re-run.)")
        silent = topo.get("silent_link_up_ports") or []
        if silent:
            where = ", ".join(f"`{p['switch']}:{p['port']}`" for p in silent)
            A(f"  There are {len(silent)} port(s) with a link and nothing "
              f"identified on them — {where}. "
              + ("That is the same count as the unobserved endpoints above, "
                 "so those devices are most likely cabled there and simply "
                 "silent. " if len(silent) == len(missing) else "")
              + "Ping from one and re-run to confirm which is which.")
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

    A("## 1. Reachability and transport")
    A("")
    A("| Switch | Host | Transport | Reachable | Session | YANG modules | "
      ":candidate | :writable-running | :xpath |")
    A("|---|---|---|---|---|---|---|---|---|")
    for name, rec in sorted(records.items()):
        nc = rec.get("netconf", {}) or {}
        transport = rec.get("transport", "netconf")
        cli = transport == "cli"
        A(f'| {name} | {rec.get("host")} '
          f'| {"**CLI**" if cli else "NETCONF"} '
          f'| {"yes" if rec.get("reachable") else "**no**"} '
          f'| {nc.get("session_id") or "-"} '
          f'| {"n/a" if cli else len(rec.get("yang_modules", {}))} '
          f'| {"n/a" if cli else ("yes" if nc.get("supports_candidate") else "no")} '
          f'| {"n/a" if cli else ("yes" if nc.get("supports_writable_running") else "no")} '
          f'| {"n/a" if cli else ("yes" if nc.get("supports_xpath") else "no")} |')
    A("")

    fellback = {n: r for n, r in records.items()
                if r.get("transport") == "cli" and r.get("reachable")}
    if fellback:
        A("> **Read over the switch CLI, not NETCONF.** "
          f"{', '.join(sorted(fellback))} did not answer on the NETCONF port, "
          "so the values below were parsed from `show` command output. They "
          "are field-for-field the same shape as a NETCONF read, but they "
          "carry no schema validation, the output format is not versioned, "
          "and two Qbv limits are unavailable (see §3). ")
        for name, rec in sorted(fellback.items()):
            probe = (rec.get("transport_detail") or {}).get("netconf_probe") or {}
            configured = (rec.get("transport_detail") or {}).get(
                "netconf_server_in_running_config")
            reason = probe.get("reason") or "NETCONF did not respond"
            A(f">")
            A(f"> - **{name}**: {reason}"
              + ("  \n>   `netconf server` **is** present in the running-config, "
                 "so this is a firmware fault rather than a missing setting."
                 if configured else ""))
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

    A("## 3. PTP")
    A("")
    line = ptp_availability_line(records)
    A(line[:1].upper() + line[1:] + ".")
    A("")
    A("No switch implements a PTP or gPTP YANG module — the module list is "
      "unchanged between AN001 v1.2 and v1.3 — so PTP is never readable over "
      "NETCONF on this hardware. Where it appears below it came from the CLI "
      "collector and is projected into three YANG models side by side. "
      "See REPORT.md §4.")
    A("")
    ptp_rows = [(n, r) for n, r in sorted(records.items())
                if (r.get("ptp") or {}).get("status") == "available-via-cli"]
    if ptp_rows:
        A("| Switch | Profile | Offset from master | Steps removed | "
          "Servo | as-capable ports | Lock verdict |")
        A("|---|---|---|---|---|---|---|")
        grandmasters = []
        for name, rec in ptp_rows:
            ptp = rec["ptp"]
            s = ptp.get("sync_summary", {})
            lock = ptp.get("lock", {})
            offset = s.get("offset_from_master_ns")
            as_cap = s.get("as_capable") or {}
            capable = sum(1 for v in as_cap.values() if v)
            if s.get("is_grandmaster"):
                grandmasters.append((name, lock))
            A(f'| {name} '
              f'| {"gPTP (802.1AS)" if ptp.get("gptp_profile") else "—"} '
              f'| {f"{offset:+.3f} ns" if offset is not None else "-"} '
              f'| {s.get("steps_removed") if s.get("steps_removed") is not None else "-"} '
              f'| {s.get("servo_state") or "-"} '
              f'| {f"{capable}/{len(as_cap)}" if as_cap else "-"} '
              f'| **{lock.get("verdict", "unknown")}** |')
        A("")

        gm_ids = {(r.get("ptp") or {}).get("sync_summary", {})
                  .get("grandmaster_identity") for _, r in ptp_rows}
        gm_ids.discard(None)
        if len(gm_ids) == 1:
            A(f"All {len(ptp_rows)} switches name the same grandmaster "
              f"(`{next(iter(gm_ids))}`), so they are one time domain, and "
              "steps removed gives each switch's depth below it.")
            A("")
        elif len(gm_ids) > 1:
            A(f"**These switches do not share a grandmaster** — "
              f"{len(gm_ids)} distinct identities are named: "
              + ", ".join(f"`{g}`" for g in sorted(gm_ids)) +
              ". Each domain can look healthy on its own while the network "
              "has no common time base, which is exactly the condition a "
              "per-switch offset reading cannot detect.")
            A("")

        for name, lock in grandmasters:
            A(f"**{name} is the grandmaster**, so its zero offset and "
              "free-running servo are definitional rather than measured, and "
              "it gets its own verdict rather than `locked`. "
              + (lock.get("caveat") or ""))
            A("")

        A("Every verdict is advisory and rests on a single sample. A "
          "measurement campaign should re-check before and after each point, "
          "as `i226-adaptation` does with `pmc` on the end stations. "
          "`as-capable` is read per port from the `802.1AS port status` "
          "table of `show ptp 0 port-state`; a switch whose synchronising "
          "port reports it false is reported `inconsistent`, not `locked`.")
        A("")
        models = (ptp_rows[0][1]["ptp"].get("models") or {})
        if models:
            A("**Projected into three YANG models.** The same readings appear "
              "under each model's own node names in `capabilities.json` at "
              "`ptp.models`, so a consumer can use whichever matches its data "
              "model without this tool having chosen for it:")
            A("")
            A("| Model | Reference | Path | Nodes the CLI cannot fill |")
            A("|---|---|---|---|")
            for key, m in models.items():
                A(f'| `{key}` | {m.get("reference","-")} '
                  f'| `{m.get("path","-")}` '
                  f'| {len(m.get("unavailable", []))} |')
            A("")
            A("`ieee802-dot1as-gptp` defines no top-level containers — it "
              "augments the `ieee1588-ptp-tt` tree — so its projection holds "
              "only the gPTP-specific additions and the base datasets live in "
              "the 1588 projection. Note also that IEEE 1588-2019 renamed the "
              "roles: `offset-from-master` is `offset-from-time-transmitter` "
              "there, and the port states `master`/`slave` are "
              "`time-transmitter`/`time-receiver`. Each projection lists what "
              "it could not fill and which values were inferred rather than "
              "read.")
            A("")
        for name, rec in ptp_rows:
            for warn in (rec["ptp"].get("warnings") or []):
                A(f"> **{name}:** {warn}")
                A("")

    A("## 4. Qbv (802.1Qbv time-aware shaper) capability envelope")
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

    # --- ingress classification ------------------------------------------
    # A gate-control list gates traffic classes. Whether a talker can reach
    # a given class is decided here, before Qbv ever sees the frame, so this
    # belongs next to the Qbv envelope and not buried in a per-port table.
    trust_rows = []
    for name, rec in sorted(records.items()):
        ports = [i for i in rec.get("interfaces", []) if i.get("is_bridge_port")]
        trusted, regen_identity, seen = 0, 0, 0
        for iface in ports:
            values = (iface.get("qos") or {}).get("values") or {}
            classification = values.get("ingress-classification") or {}
            if "trust_tag" not in classification:
                continue
            seen += 1
            if classification.get("trust_tag"):
                trusted += 1
            if values.get("priority_regeneration_is_identity"):
                regen_identity += 1
        if seen:
            trust_rows.append((name, trusted, regen_identity, seen))

    if trust_rows:
        A("## 5. Ingress classification — can a talker reach a gated class?")
        A("")
        A("| Switch | Ports trusting the VLAN tag | Ports with an identity "
          "PCP map |")
        A("|---|---|---|")
        for name, trusted, identity, seen in trust_rows:
            A(f"| {name} | {trusted}/{seen} | {identity}/{seen} |")
        A("")
        total_seen = sum(r[3] for r in trust_rows)
        total_trusted = sum(r[1] for r in trust_rows)
        total_identity = sum(r[2] for r in trust_rows)
        if total_trusted == 0:
            A("> **No port trusts the incoming VLAN tag.** Every frame is "
              "classified to the port's default traffic class whatever PCP "
              "the talker sets, so a gate-control list that opens classes "
              "1–7 opens them onto empty queues. This is configuration, not "
              "a missing capability — the hardware supports it — but it has "
              "to be changed before any schedule means anything. See the "
              "`ingress-classification` entry in §7.")
            A("")
        elif total_trusted < total_seen:
            A(f"> {total_seen - total_trusted} of {total_seen} bridge ports "
              "ignore the incoming VLAN tag and classify to the port "
              "default. Check that the ports carrying scheduled traffic are "
              "not among them.")
            A("")
        if total_identity < total_seen:
            A(f"> {total_seen - total_identity} of {total_seen} ports "
              "regenerate priority by something other than the identity map, "
              "so a stream's PCP is not its gate index. The per-port map is "
              "in `capabilities.json` under "
              "`interfaces[].qos.values.priority_regeneration`, and the Qbu "
              "per-priority record already applies it.")
            A("")

    A("## 6. Per-port state")
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

    A("## 7. Gaps for a Centralized Network Configuration entity")
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
        transport = rec.get("transport", "netconf")
        if rec.get("reachable"):
            s = rec.get("summary", {})
            detail = (f"{len(rec.get('yang_modules', {})):>3} modules"
                      if transport == "netconf" else "  CLI read")
            out.append(
                f"  {name:<6} {rec.get('host'):<15} [{transport:^7}] {detail}, "
                f"{s.get('bridge_port_count', 0)} bridge ports, "
                f"Qbv on {len(s.get('qbv_capable_ports', []))}, "
                f"Qbu on {len(s.get('qbu_capable_ports', []))}")
        else:
            err = (rec.get("errors") or [{}])[0]
            out.append(f"  {name:<6} {rec.get('host'):<15} [{transport:^7}] "
                       f"UNREACHABLE ({err.get('kind', 'unknown')})")

    fellback = sorted(n for n, r in records.items()
                      if r.get("transport") == "cli")
    if fellback:
        out.append(f"CLI fallback used  : {', '.join(fellback)} "
                   "— NETCONF did not answer")

    c = topo["link_counts"]
    out.append(f"Links              : {c['total']} "
               f"({c['lldp']} LLDP / {c['fdb_attachments']} FDB)")
    cc = topo["crosscheck"]
    out.append(f"Inventory agreement: "
               f"{'yes' if cc['agreement'] else 'NO — see topology.md §5'}")
    out.append(f"PTP                : {ptp_availability_line(records)}")
    lock = ptp_lock_line(records)
    if lock:
        out.append(f"PTP lock           : {lock}")
    out.append(f"CNC gaps recorded  : {len(cnc_doc['gaps'])}")
    out.append(BAR)
    return "\n".join(out)


def ptp_availability_line(records: Dict[str, dict]) -> str:
    """PTP availability stated per transport, never as a single word.

    Which interface answered is the whole point: this hardware has no PTP
    YANG module, so "available" can only ever mean the CLI supplied it, and
    a summary that hides that would misrepresent what a NETCONF-only CNC
    can actually see.
    """
    via_cli, via_netconf, absent = [], [], []
    for name, rec in sorted(records.items()):
        if not rec.get("reachable"):
            continue
        status = (rec.get("ptp") or {}).get("status")
        if status == "available-via-cli":
            via_cli.append(name)
        elif status == "available":
            via_netconf.append(name)
        else:
            absent.append(name)
    parts = []
    if via_netconf:
        parts.append(f"via NETCONF on {', '.join(via_netconf)}")
    if via_cli:
        parts.append(f"via CLI on {', '.join(via_cli)}")
    if absent:
        parts.append(f"NOT AVAILABLE on {', '.join(absent)}")
    return "; ".join(parts) or "no switch answered"


def ptp_lock_line(records: Dict[str, dict]) -> Optional[str]:
    verdicts = []
    for name, rec in sorted(records.items()):
        lock = ((rec.get("ptp") or {}).get("lock") or {})
        if lock.get("verdict"):
            verdicts.append(f"{name}={lock['verdict']}")
    return ", ".join(verdicts) if verdicts else None
