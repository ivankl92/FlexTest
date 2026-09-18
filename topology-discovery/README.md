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

## Two transports, one output shape

NETCONF is preferred and is tried first on every switch. Where it does not
answer, the run falls back to reading the same information over the ISTAX
CLI and maps it onto the same YANG shapes, so nothing downstream can tell
the two apart: the same keys, the same units, the same decoded gate
bitmasks. Each record says which transport produced it and why.

The fallback exists because the **GA-3.06 firmware stopped starting the
NETCONF server** even with `netconf server` present in the running-config —
a regression for the manufacturer to fix, not a configuration problem. It is
a stopgap, not a replacement: CLI output carries no schema, is not versioned
between releases, loses two of the Qbv capability limits, and has none of
NETCONF's transactional machinery. `--transport netconf` disables the
fallback entirely.

One thing the fallback gains: **PTP**. See below.

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
                 istax, istax_parse, cli_record, ptp   <- the CLI fallback
scripts/         setup_env.sh, preflight.sh, run_discovery.sh
tests/           make_fixture.py / make_cli_fixture.py (captures),
                 test_pipeline.py + test_cli_transport.py (100 tests)
topology/        latest published documents — generated, do not hand-edit
results/<run-id> every run, including the raw NETCONF XML
docs/            REPORT.md, RUNBOOK.md
```

## Two things to know before reading any output

**PTP is not on the NETCONF interface.** The KSwitch D10's NETCONF server
implements no PTP or gPTP YANG module — not `ieee1588-ptp`, not
`ieee802-dot1as`, and the module list is unchanged between AN001 v1.2 and
v1.3. Qbv schedules can be written over NETCONF, but the time base they are
anchored to cannot be read or verified there at all.

The CLI collector closes that gap: it reads the PTP datasets and maps them
onto **RFC 8575 (`ietf-ptp`)** node names, so a CNC gets `default-ds`,
`current-ds`, `parent-ds`, `time-properties-ds` and `port-ds-list` in a
standard shape, plus an advisory lock verdict derived from the offset from
master. That is enough to refuse a schedule on an unsynchronised network,
which was the one genuinely blocking gap. See `docs/REPORT.md` §4.

**The NETCONF path has never run against a real switch.** It passes 100
offline tests, including the live session path against a stand-in transport,
and its fixtures reproduce the XML shape Kontron AN001 documents exactly.
But no NETCONF session has been established against the hardware — and on
GA-3.06 none can be, because the server does not start. The CLI parsers are
in better shape: they are tested against real captured output from
KSwitchTSN-1. `docs/REPORT.md` §9 lists precisely what is therefore
unverified — the largest item being whether the switches populate LLDP
`remote-systems-data` over NETCONF at all. Every reply is captured raw and
`--reanalyse` re-runs the whole parse offline, so the first real run can be
fixed up without going back to the bench.

## Status

Implementation complete, offline-verified, awaiting a first run against the
testbed. It does not touch `i226-adaptation/`.
