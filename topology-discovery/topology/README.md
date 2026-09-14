# Discovered topology — not yet populated

This directory holds the **latest discovered state of the testbed**. It is
generated. Nothing here is hand-written, and nothing here should be
hand-edited.

It is empty of results because **discovery has not yet been run against the
testbed.** Run it:

```bash
cd ..
./scripts/preflight.sh
./scripts/run_discovery.sh
```

after which this directory will contain:

| File | Contents |
|---|---|
| `topology.md` | the discovered topology: node and link tables, a Mermaid diagram, and the cross-check against `SYSTEM.md` |
| `topology.json` | the same graph, machine-readable, with the method and evidence behind every link |
| `topology.dot` | Graphviz source (`dot -Tpng topology.dot -o topology.png`) |
| `capabilities.md` | per-switch capability summary: NETCONF, YANG modules, TSN feature matrix, Qbv envelope, per-port state |
| `capabilities.json` | the full per-switch record |
| `cnc-input.json` | the normalised CNC input document (`schema: tsn-testbed/cnc-input/v1`) |

Each run also writes a complete, timestamped copy — including every raw
NETCONF reply — to `../results/<run-id>/`. The files here are a copy of the
most recent run, kept at a fixed path so the repository and downstream tools
always have one place to look.

**Do not fill this in by hand from `SYSTEM.md`.** The entire point of the
subproject is that the topology here is what the network reported, so that a
disagreement with `SYSTEM.md` is visible rather than assumed away. If
discovery cannot produce part of it — see `../docs/RUNBOOK.md` §11, the case
where the firmware does not expose LLDP neighbour data — record that
limitation explicitly rather than substituting documentation for
measurement.
