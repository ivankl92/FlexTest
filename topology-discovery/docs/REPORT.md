# Implementation Report — NETCONF topology discovery and TSN capability retrieval

**Subject:** extending the FlexTest TSN testbed with automated network
topology discovery and switch capability retrieval over NETCONF/YANG, as the
input stage of a future Centralized Network Configuration (CNC) entity.

**Hardware in scope:** 5 × Kontron KSwitch D10 MMT (S1921), each running a
netopeer2 NETCONF server backed by sysrepo, addressed 192.168.1.10–.14.
Client side: UP-1 (192.168.1.61).

**Status:** implementation complete and fully exercised offline — 38 tests,
including the live session path against a stand-in NETCONF transport.
**It has never been run against a real KSwitch D10.** Section 9 states
precisely what that means. Read it before trusting any output.

---

## 1. Summary

The testbed's topology and switch capabilities were, until now, knowledge
held in `SYSTEM.md` and in whoever last recabled the bench. That is fine for
a two-switch latency measurement and untenable for a CNC, which must know —
at the moment it computes a schedule — which ports exist, what they are
connected to, and what the hardware will actually accept.

This subproject adds a discovery tool that answers those questions from the
network itself:

1. **Topology** from IEEE 802.1AB LLDP neighbour tables read over NETCONF,
   with endpoint attachment from the 802.1Q filtering database, and
   `SYSTEM.md` used only to put readable names on the result and to
   cross-check it.
2. **Capabilities** from the YANG modules each switch actually implements —
   established at runtime from its NETCONF `<hello>` and from
   `ietf-netconf-monitoring`, not from the datasheet — plus the per-port
   limits those models expose.
3. **A CNC input document** that normalises both into one interchange file,
   together with an explicit list of what a CNC built on this interface
   **cannot** do.

The single most consequential finding is negative and is stated up front in
§4: **the KSwitch D10's NETCONF server implements no PTP or gPTP YANG
module.** Qbv schedules can be written; the time base they are anchored to
cannot be read or verified through NETCONF at all.

Nothing in this subproject writes to a switch. Discovery is read-only by
construction — the tool issues only `<get>` and `<get-config>`, and there is
no code path that emits `<edit-config>`.

---

## 2. Scope and what was built

**In scope, as briefed:** automated topology discovery via NETCONF;
retrieval of switch capabilities via YANG models covering PTP, VLAN, QoS,
Qbv and Qbu; the topology written to its own file; the whole thing shaped as
a starting point for a CNC.

**Deliberately out of scope:** any configuration write; stream/talker/
listener modelling (that is the CUC's half of 802.1Qcc, and it needs
requirements this tool has no access to); schedule computation; and anything
touching `i226-adaptation/`, which is the timestamping measurement and is
left untouched.

```
topology-discovery/
  tsn_discovery/     the package
    inventory.py     parse SYSTEM.md into an inventory
    netconf.py       ncclient session wrapper, raw capture, failure isolation
    xmlutil.py       namespace-agnostic XML helpers
    probe.py         runtime YANG-module and feature probe (incl. the PTP question)
    capabilities.py  system, interfaces, VLAN, Qbv, Qbu, QoS extraction
    topology.py      LLDP + FDB + inventory -> graph
    cnc.py           normalised CNC input document + gap list
    render.py        Markdown, Mermaid and Graphviz renderings
    cli.py           orchestration, exit codes, --reanalyse
  scripts/
    setup_env.sh     virtualenv + ncclient/lxml (offline mode supported)
    preflight.sh     toolchain, ICMP, TCP/830, real NETCONF session
    run_discovery.sh one command
  tests/
    make_fixture.py  synthetic raw captures in the AN001 XML shape
    test_pipeline.py 38 offline tests
  topology/          latest published documents (committed)
  results/<run-id>/  every run, with raw NETCONF XML
  docs/              REPORT.md (this file), RUNBOOK.md
```

---

## 3. The NETCONF surface of the KSwitch D10

### 3.1 What the vendor documents

Kontron AN001 v1.2 (*KSwitch D10 MMT Series (S1921) Netconf*, Network OS
release GA-2.03) states that the NETCONF interface is netopeer2 over sysrepo,
with a `sysrepo-plugind` plugin bridging to the switch management software
over a Unix domain socket. It lists eleven YANG modules:

| Module | Revision | What it gives us |
|---|---|---|
| `iana-if-type` | 2017-01-19 | interface type identities |
| `ietf-system` | 2014-08-06 | hostname, contact, location |
| `ietf-interfaces` | 2018-02-20 | the interface list, and the augmentation point everything else hangs off |
| `ietf-ip` | 2018-02-22 | addressing |
| `ietf-yang-types` | 2013-07-15 | types only |
| `ieee802-dot1ab-lldp` | 2018-11-13 | **LLDP — added in AN001 v1.2** |
| `ieee802-dot1ab-types` | 2018-10-03 | types only |
| `ieee802-dot1q-bridge` | 2020-02-15 | bridge, components, VLANs, filtering database |
| `ieee802-dot1q-preemption` | 2018-09-10 | **Qbu frame preemption** |
| `ieee802-dot1q-sched` | 2020-02-20 | **Qbv time-aware shaper** |
| `ieee802-ethernet-interface` | 2019-06-21 | speed, duplex, autoneg |

The application note is explicit that "the plugin implements only a part of
the entries defined in the yang modules", and enumerates the implemented
paths for `ietf-system`, `ietf-interfaces` and `ieee802-dot1q-bridge`. LLDP
is in the module table but has no implemented-path list and no worked
example — its arrival is recorded only in the v1.2 changelog line "Add LLDP
yang module".

### 3.2 Why the tool does not trust that table

Three reasons, each of which has bitten this class of tool before:

1. **The list is release-specific.** It documents GA-2.03. Five switches
   need not be on one firmware, and a testbed that gets updated piecemeal is
   the normal case rather than the exception.
2. **A module being advertised is not the same as the paths being
   populated.** The plugin implements a subset, and the subset is not
   documented for LLDP at all.
3. **The absence of a module in a document is weak evidence.** The absence
   of a module in a switch's own `<hello>` is strong evidence.

So the tool establishes the module set at runtime from two independent
sources — the NETCONF `<hello>` capability list, and
`/ietf-netconf-monitoring:netconf-state/schemas` — and reports the union with
the provenance of each entry. Where they disagree, the disagreement is
visible in `capabilities.json` rather than silently resolved.

The `<hello>` capability list is the primary evidence and it arrives on the
transport rather than in an RPC reply, so it would be lost from a raw
capture. It is written to `raw/<switch>/hello-capabilities.json` explicitly;
that is what makes `--reanalyse` reproduce a live run exactly (§5.7).

### 3.3 What is present for TSN, and what is not

| Feature | Standard | Model | Status |
|---|---|---|---|
| VLAN / bridging | 802.1Q | `ieee802-dot1q-bridge` | present |
| Qbv — time-aware shaper | 802.1Qbv | `ieee802-dot1q-sched` | present |
| Qbu — frame preemption | 802.1Qbu | `ieee802-dot1q-preemption` | present |
| LLDP — topology | 802.1AB | `ieee802-dot1ab-lldp` | present (v1.2+) |
| Qci — PSFP | 802.1Qci | `ieee802-dot1q-psfp` | **absent** |
| Qav — credit-based shaper | 802.1Qav | — | **absent** |
| Qcc — stream/UNI | 802.1Qcc | — | **absent** |
| PTP / gPTP | 1588 / 802.1AS | — | **absent** (§4) |

The Qcc absence is expected and benign: in the fully-centralized model the
CNC holds the stream abstraction itself and renders it into per-port Qbv
state, which is exactly what this interface supports. The Qci and Qav
absences bound what kinds of isolation a CNC can enforce. The PTP absence is
the one that changes how the testbed must be operated.

---

## 4. The PTP finding

**No PTP or gPTP YANG module is implemented by the KSwitch D10's NETCONF
server.** Not `ieee1588-ptp`, not `ietf-ptp` (RFC 8575), not
`ieee802-dot1as`. The module table in §3.1 has no entry for any of them, and
the tool re-checks this at runtime against every switch rather than assuming
the document is current.

This matters more here than it would elsewhere, because the entire testbed
rests on gPTP. `i226-adaptation` measures one-way latency by subtracting two
PHC timestamps taken on two different machines, and the only reason that
subtraction means anything is that both PHCs are gPTP-disciplined to the
switches. And 802.1Qbv is itself a time-anchored mechanism: `admin-base-time`
is an absolute PTP instant, and a gate schedule whose base time refers to a
clock that has stepped is not a schedule, it is a random permutation.

So a CNC built on this NETCONF surface can compute a schedule and install it,
and can read back what it installed — but it cannot, through this interface:

- read the switch's PTP clock identity, port states, or grandmaster;
- confirm that a switch is synchronised at all;
- read the offset from the grandmaster, or detect a step;
- configure the gPTP profile, domain, or priorities.

**Consequence, stated plainly:** on this hardware, "the schedule was
installed" and "the schedule is running against a valid time base" are two
separate claims, and NETCONF can only support the first. The second must come
from somewhere else.

**How the tool handles it.** It does not pretend. `probe.ptp_finding()`
emits a structured record with `status: "not-exposed-via-netconf"`, the
evidence (which module names were searched for and in which sources), and the
consequence text. That record propagates into `capabilities.json`, into
`capabilities.md` as a call-out, and into the CNC document's gap list with
`impact: "blocking-for-verification"`. If a future firmware does implement a
PTP model, the same code path detects it and the status flips to `available`
with the module and revision recorded — no code change.

**Recommended out-of-band verification,** which the testbed already has: the
i226-adaptation campaign driver reads gPTP offset with `pmc` on both PCs
before and after every measurement point and has held ±8 ns in practice.
That is the gate to use. A CNC-driven campaign should call the same check
before it trusts a schedule, and §8 lists this as the first integration
point.

---

## 5. Design decisions

### 5.1 ncclient, not a netopeer2-cli wrapper

`netopeer2-cli` is installed and working on UP-1 and it is what the
application note uses, which is an argument for it. It was rejected anyway.

It is an interactive REPL. Driving it non-interactively means piping a script
into stdin and scraping human-formatted output back out — and it prompts for
the SSH password on a separate channel, interleaves `libyang` warnings into
the same stream (the AN001 connection example shows six of them before the
first useful line), and paginates. Every one of those is a parser hazard, and
none of them is a hazard that stays fixed: the output format is a UI, not an
interface.

`ncclient` speaks the protocol. Replies come back as XML that can be parsed
rather than scraped, `<rpc-error>` arrives as a structured exception instead
of a line of text, and session setup exposes the `<hello>` capability list,
which is the single most valuable thing on the wire for this task and which
`netopeer2-cli` does not surface in a machine-readable form at all.

`netopeer2-cli` is kept for what it is good at: `scripts/preflight.sh` uses
it, if present, as an independent cross-check that a human can reproduce by
hand, and the runbook gives the equivalent manual command for every automated
read. Discovery does not depend on it.

### 5.2 Namespace-agnostic parsing

Every lookup in `xmlutil.py` matches on **local name only**. This is a
deliberate inversion of the usual advice.

The reasoning: YANG node names (`gate-parameters`, `admin-control-list`,
`remote-systems-data`) are fixed by the IEEE and IETF standards. The
namespace URIs attached to them in a given reply depend on which module
revision the vendor compiled in and on how the plugin emits augmentations.
AN001 already shows the same data under different shapes — `admin-base-time`
with `<nanoseconds>` in one capture and `<fractional-seconds>` in another.
Binding to namespaces produces a parser that works against one firmware.
Binding to node names produces one that works against the standard.

The cost is that a node with the same local name in a different model would
be matched wrongly. In practice the searches are scoped — `extract_qbv` looks
inside one `bridge-port` element, not the whole document — and the raw XML is
kept, so any misparse is diagnosable after the fact.

The *filters* sent to the switch are the exception: those are built from the
namespaces the switch itself advertised in its `<hello>`, falling back to the
standard URIs only if it advertised none. Sending a filter and receiving a
reply are asymmetric problems.

### 5.3 LLDP is the only source of links

A filtering-database entry says "a frame with this source MAC arrived on this
port". On an edge port that usually means the device is attached there. On a
trunk it means nothing of the sort — every MAC in the network beyond that
trunk appears on it. Treating FDB entries as adjacency is how topology tools
produce confident, wrong, fully-connected graphs.

So: LLDP produces links. The FDB produces *endpoint attachments*, and only
under three conditions, all checked in `topology.add_fdb_attachments()`:

- the MAC resolves to a device that `SYSTEM.md` says is not a switch,
- the port is not one LLDP has already claimed as an inter-switch link, and
- LLDP has not already established the same adjacency.

When an FDB entry names a known endpoint on a port that *is* an inter-switch
link, that is recorded as a note explaining why it was not treated as an
attachment — the observation is kept, the wrong conclusion is not drawn.

LLDP is bidirectional, so a link should be reported from both ends. The
builder merges the two reports and sets `confirmed_bidirectional`. A
one-sided link is still reported, flagged: it means the far end does not run
LLDP, has not aged in yet, or is a device this tool did not query.

### 5.4 `SYSTEM.md` is an oracle, not an input

The inventory is parsed and used for three things: resolving a chassis-id or
management address to the name an operator recognises; deciding which
addresses are switches worth contacting; and comparing the discovered graph
against what the document claims.

It never creates a link, and it never overrides a discovered one. When the
two disagree, both are reported and the run exits non-zero. The whole point
of automating discovery is to find out when the documentation has drifted;
a tool that quietly reconciles them destroys the signal it was built to
produce.

The cross-check distinguishes two severities, because they mean different
things. A **switch** in `SYSTEM.md` that did not answer is a fault. An
**endpoint** that was not observed usually is not: an end station that
neither runs LLDP nor has transmitted recently has no FDB entry and is
invisible to both methods. The first breaks agreement; the second is reported
and does not. Getting this wrong in either direction makes the exit code
useless — always-red is as unhelpful as always-green.

### 5.5 `get` versus `get-config` — capability versus configuration

Both are issued, for different reasons.

The Qbv *limits* — `supported-list-max`, `supported-cycle-max`,
`supported-interval-max` — are `config false`. They appear in a `<get>` and
are absent from a `<get-config>`. They are also the three numbers a CNC most
needs, because they decide whether a computed schedule is installable at all.
On this platform they are 128 entries, 33538048/1000000000 s ≈ 33.5 ms, and
33538048 ns respectively.

So `<get>` is the primary read. `<get-config>` on `running` is captured
separately, because on this platform the two can legitimately differ: AN001
§Data Stores states the plugin reads running configuration from the switch
management software only when the plugin starts, so CLI and web-UI changes
made afterwards are not visible over NETCONF, and NETCONF writes are not
persistent until saved from the CLI or web UI. That is a real operational
hazard and it is recorded as a permanent entry in the CNC gap list
(`datastore-coherence`), not just as a footnote here.

Throughout the output the two are kept in separate objects — `capability` and
`configuration` — per port. Conflating them is how a scheduler ends up
believing that a cycle time currently configured is a cycle time the hardware
supports.

### 5.6 Capability limits are checked, not just recorded

`extract_qbv` cross-checks what it read: do the gate-control-list intervals
sum to the configured cycle time; does the list exceed `supported-list-max`;
does the cycle time exceed `supported-cycle-max`. A CNC would have to do this
anyway, and finding it at discovery time is cheaper than finding it after an
`edit-config` is rejected.

Severity depends on whether the gate is actually running. This is not
pedantry: the KSwitch factory default is `admin-cycle-time` 100/1000 s =
100 ms against a `supported-cycle-max` of ≈33.5 ms — every idle port on
every switch is, on paper, out of spec. Reporting forty warnings that all
mean "this port is not doing anything" trains the operator to ignore the
warning list. So an inconsistency on a port with `gate-enabled false` becomes
a **note**, collapsed across ports; on a port with the gate on it is a
**warning**.

### 5.7 Raw capture and offline re-analysis

Every reply is written to `results/<run-id>/raw/<switch>/<subtree>.xml`
before anything parses it, and `--reanalyse <run-dir>` re-runs the entire
parse-and-render pipeline against a saved run without touching the network.

This is not a convenience feature. Against firmware that cannot be tested
here, some parser will eventually be wrong in a way the fixtures do not
model. When that happens the choice is between "go back to the lab and run it
again" and "fix the parser and re-run the analysis on the bytes we already
have". The second is the only one that works when the bench is in use, and it
is also what lets a discovery run be re-examined months later against a
changed understanding of the data.

It also makes the test suite honest: `--reanalyse` is the same code path the
live run uses from the moment the XML exists.

### 5.8 Failure isolation and concurrency

Five switches are queried in parallel (`--workers`, default 5). A failure
anywhere — switch powered off, NETCONF server not enabled, auth rejected,
session dropped mid-transfer, a model that answers `<rpc-error>` — is caught,
classified into an actionable kind (`timeout`, `connection-refused`,
`auth-failed`, `rpc-error`, `unreachable`, `hostkey-rejected`, …), recorded,
and does not stop anything else. Partial results are written and are useful:
knowing that four switches answered and SW3 did not is a better outcome than
a traceback.

An `<rpc-error>` in particular is treated as data, not as a failure. "Unknown
element" in response to a filter for a model is the switch telling us it does
not implement that model — which is exactly what discovery is for.

### 5.9 Exit codes

`0` every target switch answered and the topology agrees with the inventory;
`1` discovery ran but something needs a human; `2` discovery could not run.
This makes the tool usable as a gate in a campaign script — a measurement run
that begins by asserting the network is what it was last time is a measurement
run with one fewer way to be quietly wrong.

---

## 6. Implementation walkthrough

**`inventory.py`** — tolerant parse of the `SYSTEM.md` address table. Any
line carrying an IPv4 address is a row; a row with no leading name continues
the previous device (this is how UP-1's and UP-2's second ports are
attached). MACs are normalised from all three spellings in use in that file
(`00:07:32:C1:43:30`, `88:a2:9e:4b:97:1b`, `00-BB-CC-DD-EE-12`) to lowercase
colon-separated. `SW<n>` names classify as switches. Lookups by name, MAC,
IP, and — as an explicitly-flagged weak fallback — by the first three MAC
octets, since a bridge base address is not always exactly the management MAC.

**`netconf.py`** — `SwitchSession` wraps one ncclient session. `connect()`
captures the `<hello>`, parses module names and revisions out of the
capability URIs (`…?module=X&revision=Y`), and persists them. `collect()`
issues the standard read set: `ietf-system`, `ietf-system` state,
`ietf-interfaces` (operational and config), `ieee802-dot1q-bridge`
(operational and config), `ieee802-dot1ab-lldp`, and the
`ietf-netconf-monitoring` schema list; `--full-dump` adds an unfiltered
`<get>`. Every RPC returns a `Capture` record — never an exception.

**`probe.py`** — merges hello-advertised modules with the monitoring schema
list, keeping the source of each, then matches the union against a table of
TSN features (`bridge`, `qbv`, `qbu`, `qci`, `qav`, `qcc`, `lldp`, `ptp`,
plus the IETF base models). Matching is case-insensitive substring, so a
vendor-prefixed or newer-revision module name still matches. `ptp_finding()`
turns the PTP question into a structured answer with evidence and
consequence.

**`capabilities.py`** — the parsers. Rational times
(`numerator`/`denominator` seconds) and PTP times (`seconds` +
`nanoseconds`/`fractional-seconds`) are converted to nanoseconds; gate-state
bitmasks are decoded into per-traffic-class open/closed (255 → all eight
open, 32 → TC5 only, 131 → TC0, TC1, TC7); the gate-control list is parsed
with its operation, gate states and interval per entry. Qbu yields the
per-priority express/preemptable table plus hold and release advance. QoS
collects whichever of twelve standard 802.1Q bridge-port nodes the firmware
actually exposes, and reports the list — for a CNC, knowing a knob is *not*
reachable matters as much as its value. Bridge extraction yields VLANs,
VLAN registration entries and filtering entries with their port maps.

**`topology.py`** — LLDP parsing walks up from each `remote-systems-data`
element to find its owning port, handling both the nested 802.1AB-2016 shape
and the flattened variants. Remote resolution tries, in order: discovered
bridge base address, `SYSTEM.md` MAC, `ietf-system` hostname, `SYSTEM.md`
name, LLDP management address, and finally the flagged OUI-prefix fallback.
Links are merged across directions, FDB attachments are added under the
conditions in §5.3, and the cross-check runs.

**`cnc.py`** — the normalised interchange document (§7).

**`render.py`** — `topology.md` with a Mermaid graph that renders in the
repository viewer, `topology.dot` for Graphviz, `capabilities.md`, and the
console summary.

**`cli.py`** — argument handling, parallel collection, document assembly,
publication to `topology/`, and the exit code.

---

## 7. Output

One directory per run under `results/<run-id>/`, plus a copy of the six
published documents in `topology/` so the latest state is always at a fixed
path for the repository and for downstream tools.

| File | Contents |
|---|---|
| `topology.json` | the graph: nodes, links, per-link method and evidence, cross-check, plus the parsed inventory |
| `topology.md` | the same, human-readable, with a Mermaid diagram |
| `topology.dot` | Graphviz source |
| `capabilities.json` | full per-switch record: system, NETCONF, YANG modules, features, bridges, every interface with Qbv/Qbu/QoS |
| `capabilities.md` | per-switch summary, feature matrix, Qbv envelope, gap list |
| `cnc-input.json` | the normalised CNC document |
| `switches/<name>.json` | one switch's record, for diffing between runs |
| `run.json` | run metadata, per-switch errors and durations |
| `raw/<name>/*.xml` | every NETCONF reply verbatim |
| `raw/<name>/hello-capabilities.json` | the `<hello>` capability list |

`cnc-input.json` carries `schema: "tsn-testbed/cnc-input/v1"` and is
intentionally flat and self-describing — a future CNC should consume it
without importing this package. Its shape:

- `network.bridges[]` — per switch: management address, bridge identity,
  NETCONF base capabilities, YANG module list, per-feature support, the PTP
  finding, VLANs, and `ports[]`.
- `network.bridges[].ports[]` — per port: name, port number, if-index,
  speed, PVID, traffic-class count, **role** (`inter-switch` / `edge` /
  `unused`) and neighbour, the Qbv capability *and* current state, the Qbu
  capability and state, and which QoS nodes the firmware exposes.
- `network.links[]` — the graph, with method and evidence per link.
- `network.end_stations[]` — endpoints with MACs and how they were seen.
- `network_capability_envelope` — the **smallest** per-port limit across
  every reachable bridge port. A schedule that fits this fits everywhere.
- `network_wide_support` — per feature, `all_bridges` and `any_bridge`.
- `gaps[]` — what a CNC cannot do here, each with impact and workaround.
- `crosscheck` — agreement with `SYSTEM.md`.

---

## 8. What this gives a CNC, and what is still missing

**Available now.** The network half of the 802.1Qcc fully-centralized model:
the bridge inventory, the link graph with port roles, per-port link speeds,
VLAN membership, the Qbv capability envelope that bounds any schedule, the
current gate state on every port, the Qbu preemption configuration, and an
explicit statement of which standard knobs this firmware does not expose.

**Missing, and in rough order of effort:**

1. **A gPTP verification gate.** The highest-value next step, and it follows
   directly from §4. The information exists — `i226-adaptation` already reads
   gPTP offset with `pmc` on both PCs. Wiring that into the discovery output
   so the CNC can refuse to trust a schedule on an unsynchronised network
   closes the one gap that is genuinely blocking.
2. **The write path.** `edit-config` against `ieee802-dot1q-sched`, with
   pre-validation against the envelope this tool already produces, and an
   explicit re-read to confirm what landed. The AN001 examples in §16–21 are
   the reference for the exact XML. Note that `config-change` must be set for
   a new gate-control list to take effect, and that persistence across reboot
   requires a save from the CLI or web UI.
3. **The CUC half.** Talkers, listeners and their requirements. Nothing in
   discovery can supply this; it has to come from the experiment definition.
4. **Schedule computation.** Once 1–3 exist, the envelope in
   `network_capability_envelope` is the constraint set a scheduler works
   against.
5. **Change detection.** `switches/<name>.json` is written per run precisely
   so that two runs can be diffed. A watch mode that alerts on topology
   change would fall out of that cheaply.
6. **NETCONF notifications.** The netopeer2 server advertises
   `:notification:1.0`. Subscribing would turn periodic polling into
   event-driven discovery — worth doing once the polling version has been
   proven against real hardware, not before.

---

## 9. Verification status

**Exercised.** 38 offline tests pass (`python3 -m unittest discover -s tests`),
covering:

- The **live session code path** — `SwitchSession.connect()`, `collect()`,
  raw capture, session close — driven against a stand-in transport that
  replays fixture XML. Everything except the SSH layer itself is executed by
  the same code that will run against the switches.
- Every subtree filter the tool emits is asserted to be well-formed XML and
  to carry a namespace.
- Failure handling: `<rpc-error>` recorded and collection continues;
  connection-refused, timeout and auth failures classified correctly; an
  empty capture directory reported as unreachable rather than replayed as
  clean.
- Parsers: gate-state bitmask decoding, rational and PTP time conversion,
  gate-control list, Qbv capability limits, Qbu status table, VLANs, bridge
  identity, MAC normalisation across all three spellings, `SYSTEM.md`
  continuation lines.
- Topology: LLDP links match the fixture wiring exactly, bidirectional
  confirmation, resolution by bridge address, multi-homed endpoints getting
  both attachments, inventory names winning over LLDP hostnames, unobserved
  endpoints not breaking agreement.
- The CNC document: envelope minimisation, port role classification, the PTP
  gap present with `blocking-for-verification`, network-wide support flags.
- Shell scripts pass `bash -n` and `shellcheck -S warning` clean.

The fixtures reproduce the exact XML shape, namespaces, element names and
default values AN001 documents — `supported-list-max` 128,
`supported-cycle-max` 33538048/1000000000, `admin-gate-states` 255, port
names `Gi 1/1..1/6` and `2.5G 1/1..1/2`, bridge type
`two-port-mac-relay-bridge`, 8 ports. Everything the fixture generator writes
is marked `SYNTHETIC` and run directories it creates carry a `SYNTHETIC`
marker file, mirroring the interlock in
`i226-adaptation/analysis/make_fixture.py`.

**Never executed.** **Nothing in this subproject has ever contacted a
KSwitch D10.** No NETCONF session has been established against the real
hardware, no reply has been parsed from a real switch, and no topology has
been discovered. Specifically unverified:

- That the NETCONF server is enabled on any of the five switches. AN001 §5 is
  explicit that it does not start automatically and must be enabled from the
  ISTAX CLI (`configure terminal` / `netconf server`).
- That the credentials in `SYSTEM.md` (`netconf` / `geheim`) are correct, and
  that the SSH key exchange and cipher suites the switches offer are ones
  paramiko will negotiate. Older embedded SSH stacks sometimes are not.
- **That LLDP `remote-systems-data` is populated at all.** This is the single
  largest risk in the subproject. The module is advertised in AN001 v1.2, but
  the plugin implements only a subset of each module, no implemented-path
  list is given for LLDP, and no worked example exists. If remote systems
  data is not populated, no switch-to-switch link can be discovered by this
  method and §11 of the runbook applies.
- The exact LLDP tree shape. The parser handles the nested 802.1AB-2016 form
  and two flattened variants, but this is defensive coding against an
  unobserved format.
- Whether `port-ref` in filtering-database entries maps to
  `bridge-port/port-number` or to `if-index` on this firmware. Both are
  tried, `port-number` first; the fixture exercises the case where they
  coincide, which is the case that hides a mismatch.
- Whether the frame-preemption model is populated, and in which of the two
  shapes the parser handles.
- Whether the five switches are on the same firmware release.

**Conclusion: the tool is verified to be correct against the documented data
model. It is not verified to work against the hardware.** The first real run
is a test of the vendor's implementation as much as of this code, which is
why every reply is captured raw and why `--reanalyse` exists. Expect the
first run to need one round of parser adjustment, and expect the raw captures
to be what makes that adjustment possible.

---

## 10. Not implemented

- Any write path. Read-only by construction; there is no `edit-config` code.
- Qci, Qav and Qcc handling beyond detecting and reporting their absence.
- NETCONF notification subscriptions.
- SNMP, RESTCONF, or ISTAX-CLI collectors. The brief was NETCONF/YANG; the
  PTP gap is the obvious candidate for a second collector and is deliberately
  left as a documented decision rather than a half-built feature.
- Topology change detection between runs. The per-switch JSON is written to
  make it easy; the diffing is not written.
- Historical trending or a database. Runs are directories.

---

## 11. Bottom line

The discovery mechanism is complete, tested offline against the documented
data model, and read-only. It answers what the network is, what each switch
can do, and — with equal prominence — what this NETCONF interface cannot do.

Two things determine whether it works on the bench. The first is whether the
switches' LLDP remote-systems-data is populated; if it is not, topology
discovery falls back to what the FDB and `SYSTEM.md` can support, which is
endpoint attachment and nothing else. The second is that PTP is not on this
interface at all, which is not a limitation of this tool and cannot be fixed
in it — it changes how a CNC must be operated on this hardware, and §4 and
§8.1 say how.

Run `scripts/preflight.sh` first. It is designed to tell you which of those
two you are dealing with before you spend time on the output.
