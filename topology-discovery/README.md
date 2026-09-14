# Topology discovery and TSN capability retrieval over NETCONF/YANG

Automated discovery of the FlexTest TSN testbed: what is connected to what,
and what each Kontron KSwitch D10 can actually be asked to do — read from
the switches themselves over NETCONF, and shaped as the input stage of a
future Centralized Network Configuration (CNC) entity.

Read-only by construction: the tool issues `<get>` and `<get-config>` and
has no `edit-config` code path.

## What it produces

- **Topology** from IEEE 802.1AB LLDP neighbour tables, with endpoint
  attachment from the 802.1Q filtering database. `SYSTEM.md` supplies names
  and acts as a cross-check oracle — it never creates a link.
- **Capabilities** from the YANG modules each switch actually implements,
  established at runtime from its NETCONF `<hello>` and from
  `ietf-netconf-monitoring` rather than from the datasheet: VLAN, QoS,
  802.1Qbv (TAS), 802.1Qbu (frame preemption), and the per-port limits that
  bound any schedule.
- **A CNC input document** normalising both, with an explicit list of what
  this NETCONF interface *cannot* do.

## Quick start

```bash
cd /home/ivank/tsn-testbed/topology-discovery

./scripts/setup_env.sh        # virtualenv + ncclient/lxml
./scripts/preflight.sh        # do the switches answer?
./scripts/run_discovery.sh    # discover

less topology/topology.md
less topology/capabilities.md
```

Full procedure, switch preparation and troubleshooting:
**`docs/RUNBOOK.md`**. Design, decisions and verification status:
**`docs/REPORT.md`**.

## Layout

```
tsn_discovery/   inventory, netconf, probe, capabilities, topology, cnc, render, cli
scripts/         setup_env.sh, preflight.sh, run_discovery.sh
tests/           make_fixture.py (synthetic captures), test_pipeline.py (38 tests)
topology/        latest published documents — generated, do not hand-edit
results/<run-id> every run, including the raw NETCONF XML
docs/            REPORT.md, RUNBOOK.md
```

## Two things to know before reading any output

**PTP is not on this interface.** The KSwitch D10's NETCONF server
implements no PTP or gPTP YANG module — not `ieee1588-ptp`, not
`ieee802-dot1as`. Qbv schedules can be written, but the time base they are
anchored to cannot be read or verified through NETCONF at all. The tool
re-checks this per switch at runtime and reports it with evidence rather
than assuming. Verify gPTP out of band — `i226-adaptation` already does,
with `pmc` on both PCs. See `docs/REPORT.md` §4.

**This has never run against a real switch.** The implementation is complete
and passes 38 offline tests, including the live session path against a
stand-in transport, and the fixtures reproduce the XML shape Kontron AN001
v1.2 documents exactly. But no NETCONF session has been established against
the hardware. `docs/REPORT.md` §9 lists precisely what is therefore
unverified — the largest item being whether the switches populate LLDP
`remote-systems-data` over NETCONF at all. Every reply is captured raw and
`--reanalyse` re-runs the whole parse offline, so the first real run can be
fixed up without going back to the bench.

## Status

Implementation complete, offline-verified, awaiting a first run against the
testbed. It does not touch `i226-adaptation/`.
