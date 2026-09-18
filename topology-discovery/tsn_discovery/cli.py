"""Command-line entry point for TSN topology and capability discovery.

    python3 -m tsn_discovery.cli --help

Exit codes are meaningful, so this can be used as a gate in a campaign
script:

    0  every target switch answered and the topology agrees with SYSTEM.md
    1  discovery ran, but something needs a human: a switch was unreachable,
       or the discovered topology disagrees with SYSTEM.md
    2  discovery could not run at all (no ncclient, no inventory, bad args)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import SCHEMA_VERSION, __version__
from . import capabilities as caps_mod
from . import cli_record as cli_record_mod
from . import cnc as cnc_mod
from . import istax
from . import istax_parse as istax_parse_mod
from . import netconf as nc
from . import probe as probe_mod
from . import render as render_mod
from . import topology as topo_mod
from .inventory import parse_system_md, switch_targets

DEFAULT_USER = "netconf"
# AN001 v1.3 documents the NETCONF default as netconf/netconf; SYSTEM.md
# records geheim for this testbed. The env var overrides either.
DEFAULT_PASSWORD = "geheim"
DEFAULT_PORT = 830

DEFAULT_CLI_USER = "admin"
DEFAULT_CLI_PORT = 22


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=False)
        fh.write("\n")


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tsn_discovery",
        description="Automated NETCONF/YANG topology discovery and TSN "
                    "capability retrieval for the FlexTest TSN testbed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # discover every switch listed in SYSTEM.md
  python3 -m tsn_discovery.cli --inventory ../SYSTEM.md

  # one switch only, with a name
  python3 -m tsn_discovery.cli --switch SW1=192.168.1.10

  # capture everything, including an unfiltered <get>, for offline analysis
  python3 -m tsn_discovery.cli --full-dump

  # re-render reports from a previous run without touching the network
  python3 -m tsn_discovery.cli --reanalyse results/20260914-120000
""")
    p.add_argument("--inventory", default="../SYSTEM.md",
                   help="path to SYSTEM.md (default: ../SYSTEM.md)")
    p.add_argument("--switch", action="append", metavar="[NAME=]IP",
                   help="target a specific switch; repeatable. Overrides the "
                        "inventory-derived switch list entirely.")
    p.add_argument("--user", default=os.environ.get("NETCONF_USER", DEFAULT_USER),
                   help="NETCONF username (env NETCONF_USER)")
    p.add_argument("--password",
                   default=os.environ.get("NETCONF_PASSWORD", DEFAULT_PASSWORD),
                   help="NETCONF password (env NETCONF_PASSWORD). Prefer the "
                        "environment variable over the command line.")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("NETCONF_PORT", DEFAULT_PORT)),
                   help=f"NETCONF port (default {DEFAULT_PORT})")
    p.add_argument("--timeout", type=int, default=30,
                   help="per-RPC timeout in seconds (default 30)")

    t = p.add_argument_group(
        "transport",
        "NETCONF is preferred and is always tried first. The CLI collector "
        "exists because the GA-3.06 firmware stopped starting the NETCONF "
        "server even with `netconf server` in the running-config; it is a "
        "fallback, never a replacement.")
    t.add_argument("--transport", choices=("auto", "netconf", "cli"),
                   default="auto",
                   help="auto (default): probe NETCONF per switch and fall "
                        "back to the ISTAX CLI only where it does not answer. "
                        "netconf: never fall back, report the failure. "
                        "cli: skip the probe and read over the CLI.")
    t.add_argument("--probe-timeout", type=float, default=3.0,
                   help="seconds to wait on the NETCONF port probe (default 3)")
    t.add_argument("--cli-user",
                   default=os.environ.get("ISTAX_USER", DEFAULT_CLI_USER),
                   help=f"switch CLI username (env ISTAX_USER, "
                        f"default {DEFAULT_CLI_USER})")
    t.add_argument("--cli-password",
                   default=os.environ.get("ISTAX_PASSWORD", ""),
                   help="switch CLI password (env ISTAX_PASSWORD). Prefer the "
                        "environment variable over the command line.")
    t.add_argument("--cli-port", type=int,
                   default=int(os.environ.get("ISTAX_PORT", DEFAULT_CLI_PORT)),
                   help=f"switch SSH port (default {DEFAULT_CLI_PORT})")
    t.add_argument("--cli-all-defaults", action="store_true",
                   help="also capture `show running-config all-defaults`. "
                        "Port-level frame preemption is omitted from the plain "
                        "running-config (AN1185 §6), so this completes Qbu.")
    t.add_argument("--no-legacy-ssh", action="store_true",
                   help="do not retry SSH with SHA-1 era algorithms when "
                        "negotiation fails")
    p.add_argument("--workers", type=int, default=5,
                   help="how many switches to query concurrently (default 5)")
    p.add_argument("--out", default="results",
                   help="output root directory (default: results)")
    p.add_argument("--run-id", default=None,
                   help="run identifier (default: UTC timestamp)")
    p.add_argument("--publish-dir", default="topology",
                   help="directory that receives the latest topology and "
                        "capability documents (default: topology). Use "
                        "--no-publish to skip.")
    p.add_argument("--no-publish", action="store_true",
                   help="do not copy results into the publish directory")
    p.add_argument("--full-dump", action="store_true",
                   help="additionally issue an unfiltered <get> per switch")
    p.add_argument("--reanalyse", metavar="RUNDIR", default=None,
                   help="re-run parsing and rendering against the raw XML "
                        "captured in a previous run directory; contacts "
                        "nothing")
    p.add_argument("--quiet", action="store_true", help="less console output")
    p.add_argument("--version", action="version",
                   version=f"tsn_discovery {__version__}")
    return p


def netconf_port_open(host: str, port: int, timeout: float) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_netconf(target: dict, args, log) -> dict:
    """Decide, per switch, whether NETCONF is usable before committing to it.

    Two stages, because they fail differently and the difference is
    diagnostic. A refused TCP connection means nothing is listening -- the
    symptom of the GA-3.06 regression. A TCP connection that opens but whose
    session then fails means the server is there and something else is
    wrong (credentials, SSH negotiation), which the CLI fallback would not
    fix and which the operator should see rather than have papered over.
    """
    host = target["host"]
    probe = {"port": args.port, "tcp_open": None, "responding": False,
             "kind": None, "reason": None}

    probe["tcp_open"] = netconf_port_open(host, args.port, args.probe_timeout)
    if not probe["tcp_open"]:
        probe["kind"] = "port-closed"
        probe["reason"] = (
            f"nothing is listening on {host}:{args.port}. If `netconf server` "
            "is present in the running-config, this is the GA-3.06 firmware "
            "regression, not a configuration problem.")
        return probe

    probe["responding"] = True
    probe["reason"] = f"TCP {args.port} accepted the connection"
    return probe


def discover_one(target: dict, args, raw_root: str, log):
    """Collect one switch, choosing the transport.

    Returns ``(transport, result, probe)`` where *result* is either a
    ``netconf.SwitchResult`` or an ``istax.CliResult``.
    """
    name, host = target["name"], target["host"]
    raw_dir = os.path.join(raw_root, name)
    probe: Optional[dict] = None

    if args.transport in ("auto", "netconf"):
        probe = probe_netconf(target, args, log)
        if probe["responding"]:
            session = nc.SwitchSession(
                name=name, host=host, port=args.port,
                username=args.user, password=args.password,
                timeout=args.timeout, raw_dir=raw_dir, logger=log,
            )
            try:
                session.connect()
                if session.result.reachable:
                    nc.collect(session, full_dump=args.full_dump)
                    return "netconf", session.result, probe
                probe["responding"] = False
                probe["kind"] = session.result.connect_error_kind
                probe["reason"] = session.result.connect_error
            finally:
                session.close()

        if args.transport == "netconf":
            failed = nc.SwitchResult(name=name, host=host, port=args.port,
                                     raw_dir=raw_dir)
            failed.connect_error = probe["reason"]
            failed.connect_error_kind = probe["kind"] or "no-netconf"
            return "netconf", failed, probe

        log(f"  [{name}] NETCONF not responding ({probe['kind']}); "
            "falling back to the ISTAX CLI")

    # --- CLI fallback -----------------------------------------------------
    session = istax.IstaxSession(
        name=name, host=host, port=args.cli_port,
        username=args.cli_user, password=args.cli_password,
        timeout=args.timeout, raw_dir=raw_dir, logger=log,
        legacy_algorithms=not args.no_legacy_ssh,
    )
    try:
        session.connect()
        istax.collect(
            session,
            interface_lister=lambda r: [i["name"]
                                        for i in istax_parse_mod.parse_interfaces(r)],
            all_defaults=args.cli_all_defaults,
        )
    finally:
        session.close()
    return "cli", session.result, probe


def cli_result_from_raw(name: str, host: str, raw_dir: str) -> istax.CliResult:
    """Rebuild a CliResult from a previous run's captured command output."""
    result = istax.CliResult(name=name, host=host, port=DEFAULT_CLI_PORT,
                             raw_dir=raw_dir)
    files = [f for f in sorted(os.listdir(raw_dir)) if f.endswith(".txt")]
    if not files:
        result.connect_error = f"no CLI output captured in {raw_dir}"
        result.connect_error_kind = "no-raw-data"
        return result
    result.reachable = True
    for fname in files:
        key = fname[:-4]
        with open(os.path.join(raw_dir, fname), "r", encoding="utf-8") as fh:
            body = fh.read()
        command = ""
        if body.startswith("! command:"):
            first, _, body = body.partition("\n")
            command = first.split(":", 1)[1].strip()
        result.captures[key] = istax.CliCapture(
            key=key, command=command, ok=True, text=body)
    return result


def result_from_raw(name: str, host: str, raw_dir: str):
    """Rebuild a result from a previous run, whichever transport made it.

    Returns ``(transport, result)``. A directory holding ``.xml`` is a
    NETCONF capture; one holding ``.txt`` is a CLI capture. A run that fell
    back mid-way can contain both, in which case NETCONF wins -- it is the
    better record and the fallback only ran because NETCONF had failed
    before any XML was written.
    """
    has_xml = has_txt = False
    if os.path.isdir(raw_dir):
        contents = os.listdir(raw_dir)
        has_xml = any(f.endswith(".xml") for f in contents)
        has_txt = any(f.endswith(".txt") for f in contents)
    if has_txt and not has_xml:
        return "cli", cli_result_from_raw(name, host, raw_dir)

    result = nc.SwitchResult(name=name, host=host, port=DEFAULT_PORT,
                             raw_dir=raw_dir)
    if not os.path.isdir(raw_dir):
        result.connect_error = f"no raw capture directory {raw_dir}"
        result.connect_error_kind = "no-raw-data"
        return "netconf", result
    contents = sorted(os.listdir(raw_dir))
    if not any(f.endswith(".xml") for f in contents):
        # An empty capture directory means the live run never got a reply
        # from this switch. Replaying it as "reachable" would quietly turn a
        # failed run into a clean one.
        result.connect_error = (f"no NETCONF replies captured in {raw_dir} "
                                "-- the switch did not answer during the "
                                "original run")
        result.connect_error_kind = "no-raw-data"
        return "netconf", result
    result.reachable = True

    hello = os.path.join(raw_dir, "hello-capabilities.json")
    if os.path.isfile(hello):
        with open(hello, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        result.server_capabilities = saved.get("capabilities", [])
        result.session_id = saved.get("session_id")
        if saved.get("host"):
            result.host = saved["host"]
        result.modules, result.namespaces = nc.parse_capabilities(
            result.server_capabilities)

    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".xml"):
            continue
        key = fname[:-4]
        with open(os.path.join(raw_dir, fname), "r", encoding="utf-8") as fh:
            xml = fh.read()
        result.captures[key] = nc.Capture(
            key=key, operation="replay", filter_xml=None, ok=True,
            xml=xml, bytes=len(xml))
    return "netconf", result


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    run_id = args.run_id or _ts()

    # --- inventory ------------------------------------------------------
    inv_path = os.path.abspath(args.inventory)
    if not os.path.isfile(inv_path):
        print(f"error: inventory not found: {inv_path}", file=sys.stderr)
        print("       pass --inventory /path/to/SYSTEM.md", file=sys.stderr)
        return 2
    inventory = parse_system_md(inv_path)
    log(f"Inventory: {len(inventory.devices)} devices from {inv_path} "
        f"({len(inventory.switches)} switches, "
        f"{len(inventory.endpoints)} endpoints)")

    # --- replay mode ----------------------------------------------------
    if args.reanalyse:
        run_dir = os.path.abspath(args.reanalyse)
        raw_root = os.path.join(run_dir, "raw")
        if not os.path.isdir(raw_root):
            print(f"error: no raw/ directory under {run_dir}", file=sys.stderr)
            return 2
        targets = []
        for name in sorted(os.listdir(raw_root)):
            dev = inventory.by_name(name)
            targets.append({"name": name,
                            "host": dev.primary_ip if dev else "replay"})
        results = []
        for t in targets:
            transport, result = result_from_raw(
                t["name"], t["host"], os.path.join(raw_root, t["name"]))
            results.append((transport, result, None))
        log(f"Re-analysing {len(results)} switches from {raw_root} "
            "(no network access)")
    else:
        targets = switch_targets(inventory, args.switch)
        if not targets:
            print("error: no switches to contact. SYSTEM.md lists none "
                  "matching SW<n>, and no --switch was given.", file=sys.stderr)
            return 2
        if args.transport != "cli" and not nc.NCCLIENT_AVAILABLE:
            print(f"error: ncclient is not importable "
                  f"({nc.NCCLIENT_IMPORT_ERROR}).", file=sys.stderr)
            print("       run scripts/setup_env.sh, then re-run from the venv,",
                  file=sys.stderr)
            print("       or pass --transport cli to read over the switch CLI.",
                  file=sys.stderr)
            return 2
        if args.transport != "netconf" and not istax.PARAMIKO_AVAILABLE:
            print(f"error: paramiko is not importable "
                  f"({istax.PARAMIKO_IMPORT_ERROR}); the CLI fallback needs it.",
                  file=sys.stderr)
            print("       run scripts/setup_env.sh.", file=sys.stderr)
            return 2
        if args.transport != "netconf" and not args.cli_password:
            # An empty password is a legitimate configuration -- these
            # switches accept the `admin` account with no password unless one
            # has been set. Saying the fallback "cannot log in" and then
            # logging in teaches the reader to ignore the warnings.
            print("note: no CLI password set; the fallback will try the "
                  f"'{args.cli_user}' account with an empty password. Export "
                  "ISTAX_PASSWORD or pass --cli-password if your switches "
                  "have one.", file=sys.stderr)

        run_dir = os.path.abspath(os.path.join(args.out, run_id))
        raw_root = os.path.join(run_dir, "raw")
        log(f"Run {run_id} -> {run_dir}")
        log(f"Contacting {len(targets)} switches ({args.workers} in parallel), "
            f"transport={args.transport}")
        if args.transport == "auto":
            log(f"  NETCONF as '{args.user}' on port {args.port}; "
                f"CLI fallback as '{args.cli_user}' on port {args.cli_port}")

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(
                lambda t: discover_one(t, args, raw_root, log), targets))

    # --- probe, extract, assemble ---------------------------------------
    records: Dict[str, dict] = {}
    lldp_by_switch: Dict[str, List[dict]] = {}

    for transport, result, probe in results:
        if transport == "cli":
            record = cli_record_mod.build(result, netconf_probe=probe)
            neighbours = record["lldp"]["neighbours"]
        else:
            probe_result = probe_mod.probe(result)
            record = caps_mod.build(result, probe_result)
            record["ptp"] = probe_mod.ptp_finding(probe_result)
            record["transport"] = "netconf"
            record["transport_detail"] = {"protocol": "ssh",
                                          "port": result.port,
                                          "netconf_probe": probe}
            record.setdefault("notes", [])
            neighbours, local_lldp = topo_mod.parse_lldp(result.xml_of("lldp"))
            record["lldp"] = {"local": local_lldp, "neighbours": neighbours}

        lldp_by_switch[result.name] = neighbours
        records[result.name] = record

    builder = topo_mod.TopologyBuilder(inventory, records, lldp_by_switch)
    topo = builder.build()

    run_meta = {
        "run_id": run_id,
        "schema": SCHEMA_VERSION,
        "tool_version": __version__,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "inventory": inv_path,
        "mode": "reanalyse" if args.reanalyse else "live",
        "transport_requested": args.transport,
        "targets": [{"name": r.name, "host": r.host, "transport": t}
                    for t, r, _ in results],
        "transports_used": {
            t: sorted(r.name for tt, r, _ in results if tt == t)
            for t in sorted({t for t, _, _ in results})
        },
        "ncclient_available": nc.NCCLIENT_AVAILABLE,
        "paramiko_available": istax.PARAMIKO_AVAILABLE,
    }

    cnc_doc = cnc_mod.build(records, topo, inventory, run_meta)

    # --- write ----------------------------------------------------------
    topo_doc = dict(topo)
    topo_doc["run"] = run_meta
    topo_doc["inventory"] = inventory.to_dict()

    _write_json(os.path.join(run_dir, "topology.json"), topo_doc)
    _write_json(os.path.join(run_dir, "capabilities.json"),
                {"run": run_meta, "switches": records})
    _write_json(os.path.join(run_dir, "cnc-input.json"), cnc_doc)
    _write_json(os.path.join(run_dir, "run.json"), {
        "run": run_meta,
        "errors": {name: rec.get("errors", []) for name, rec in records.items()},
        "durations_s": {name: rec.get("collection_duration_s")
                        for name, rec in records.items()},
    })
    for name, rec in records.items():
        _write_json(os.path.join(run_dir, "switches", f"{name}.json"), rec)

    _write_text(os.path.join(run_dir, "topology.md"),
                render_mod.topology_markdown(topo, run_meta, inv_path))
    _write_text(os.path.join(run_dir, "topology.dot"),
                render_mod.topology_dot(topo))
    _write_text(os.path.join(run_dir, "capabilities.md"),
                render_mod.capabilities_markdown(records, cnc_doc, run_meta))

    # --- publish ---------------------------------------------------------
    if not args.no_publish:
        pub = os.path.abspath(args.publish_dir)
        os.makedirs(pub, exist_ok=True)
        for fname in ("topology.json", "topology.md", "topology.dot",
                      "capabilities.json", "capabilities.md", "cnc-input.json"):
            src = os.path.join(run_dir, fname)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(pub, fname))
        log(f"Published latest documents to {pub}/")

    # --- report ----------------------------------------------------------
    log("")
    log(render_mod.console_summary(records, topo, cnc_doc))
    log("")
    log(f"Run directory : {run_dir}")
    log(f"Topology      : {os.path.join(run_dir, 'topology.md')}")
    log(f"Capabilities  : {os.path.join(run_dir, 'capabilities.md')}")
    log(f"CNC input     : {os.path.join(run_dir, 'cnc-input.json')}")
    log(f"Raw captures  : {raw_root}  "
        f"(.xml = NETCONF, .txt = CLI)")

    unreachable = [n for n, r in records.items() if not r.get("reachable")]
    if unreachable:
        log("")
        log(f"WARNING: {len(unreachable)} switch(es) unreachable: "
            f"{', '.join(unreachable)}")
        for name in unreachable:
            for err in records[name].get("errors", []):
                log(f"  {name}: {err['kind']} — {err['message']}")
        return 1
    if not topo["crosscheck"]["agreement"]:
        log("")
        log("WARNING: discovered topology disagrees with SYSTEM.md — "
            "see topology.md §5")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
