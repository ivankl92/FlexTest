# Implementation Report — NETCONF topology discovery and TSN capability retrieval

**Subject:** extending the FlexTest TSN testbed with automated network
topology discovery and switch capability retrieval over NETCONF/YANG, as the
input stage of a future Centralized Network Configuration (CNC) entity.

**Hardware in scope:** 5 × Kontron KSwitch D10 MMT (S1921), each running a
netopeer2 NETCONF server backed by sysrepo, addressed 192.168.1.10–.14.
Client side: UP-1 (192.168.1.61).

**Status:** implementation complete and fully exercised offline — 100 tests.
Since the GA-3.06 firmware update the switches' NETCONF server no longer
starts, so the tool now has a **second transport**: it probes NETCONF, and
where NETCONF does not answer it reads the same information over the ISTAX
CLI and maps it onto the same YANG shapes (§5.10). That fallback also closes
the PTP gap (§4). **The NETCONF path has still never run against a real
KSwitch D10**; the CLI parsers are tested against real captured output.
Section 9 states precisely what is and is not verified. Read it before
trusting any output.

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
cannot be read or verified through NETCONF at all. Since GA-3.06 the
NETCONF server does not start on any of the five switches either, so the
tool reads them over the ISTAX CLI and maps the result onto the same YANG
shapes (§5.9).

The first live run, on 2026-09-18, reached all five switches and confirmed
the topology and the gPTP hierarchy. It also corrected six defects in this
tool that no offline fixture could have caught (§9.1), and turned up two
findings about the bench itself (§9.2): **no bridge port trusts the incoming
VLAN tag**, so a Qbv schedule gating traffic classes 1–7 would gate empty
queues, and **the PCP-to-traffic-class map is not the identity** on any
port. Both need attention before the first scheduling experiment.

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

**This is unchanged in AN001 v1.3.** The v1.3 revision (which adds only the
default username and password to the document) carries exactly the same
eleven-module table. So the gap is not a documentation lag that a newer
application note closes.

**The CLI collector closes it, at a cost.** §5.9 describes the fallback
transport added after the GA-3.06 regression. Among the things the ISTAX CLI
prints and NETCONF does not is the whole PTP surface: `show ptp 0 default`,
`current`, `parent`, `time-property`, `port-state` and `slave`, plus the
`ptp 0 ...` lines of the running configuration. `tsn_discovery/ptp.py` maps
those onto standard YANG node names rather than inventing field names, for
three reasons: the record stays uniform whatever the transport; a CNC has one
standard place to look for the time base; and if Kontron ever ships a PTP
YANG module the data is already in its shape, so only the collector changes.

### 4.1 Why all three PTP models, and not one

The first version of the collector projected onto RFC 8575 (`ietf-ptp`)
alone. That was the wrong single choice, and rather than swap it for a
different single choice the collector now emits **all three** projections
side by side, from one neutral observation of the CLI output.

| Module | Revision | Models | Shape |
|---|---|---|---|
| `ietf-ptp` | 2019-05-06 | IEEE 1588-2008 | `/ptp/instance-list`, flat `port-ds-list` |
| `ieee1588-ptp-tt` | 2023-08-14 | IEEE 1588-2019 | `/ptp/instances/instance`, nested `ports/port/port-ds` |
| `ieee802-dot1as-gptp` | 2025-02-04 | IEEE 802.1AS-2020 | augments only -- no top-level containers of its own |

Three things forced this.

**802.1AS is a layer, not a peer.** `ieee802-dot1as-gptp` imports
`ieee1588-ptp-tt` and augments its tree; it defines no top-level container.
So "map to 802.1AS instead of 1588" is not a choice that can be made --
emitting the gPTP augmentations at all *requires* emitting the 1588-2019 tree
underneath them. A projection that named only 802.1AS would be a fiction.

**The two 1588 lineages disagree on names, not on values.** IEEE 1588-2019
renamed the leaves that matter most: `offset-from-master` became
`offset-from-time-transmitter`, `mean-path-delay` became `mean-delay`,
`slave-only` became `time-receiver-only`, and the port states `master` and
`slave` became `time-transmitter` and `time-receiver`. A consumer written
against RFC 8575 and a consumer written against the 2019 module are both
correct and neither can read the other's document. The collector carries the
same measured value under both names -- the projections are two spellings of
one observation, not two readings.

**This testbed runs gPTP, and gPTP data has a standard home.** The
`ptp 0 gptp-interval 0` line of the running config, and the
`is-measuring-delay` / neighbour-rate-ratio state the CLI prints, are
802.1AS concepts with no node in either 1588 module. Under the previous
single-model mapping they sat in invented field names. They now sit in
`ieee802-dot1as-gptp`'s own augmentation leaves --
`current-log-gptp-cap-interval`, `is-measuring-delay` -- where a gPTP-aware
CNC will look for them.

Each projection declares its `module`, `revision`, `reference` and
`namespace`, and carries two honesty lists: `unavailable`, for nodes the
model defines that the CLI does not print, and `derived`, for values filled
by inference, each with the inference stated.

### 4.2 `as-capable`: a correction

The first version of this section held up `as-capable` as the model case for
declaring a gap. The argument was that 802.1AS defines it, the CLI does not
print it, it would be easy to fake from the port state, and a CNC that
trusted a fabricated `as-capable` would schedule against a time base nobody
had verified. The argument was sound. The premise was wrong.

`show ptp 0 port-state` prints **three** tables, not two. After the PTP port
state and the virtual-port table comes one headed `802.1AS port status:`,
and it has an `as-cap` column:

```
802.1AS port status:
Port  port-role  is-mes-del  as-cap  rate-ratio   cur-anv  cur-syv  ...
----  ---------  ----------  ------  -----------  -------  -------  ...
   4  Slave      True        True            241        0       -3  ...
   6  Disabled   False       False             0        0       -3  ...
```

The collector was parsing the first table and stopping. Worse, it filled
`is-measuring-delay` from the `Peer-delay` OK/FAIL column of the first
table — which reports whether the peer delay mechanism is healthy on that
link, a different assertion — and got it wrong on five of eight ports on
every switch. So the one leaf that was loudly declared unobtainable was
printed all along, and the leaf beside it was quietly wrong.

Both are now read from their own columns, along with `port-role`,
`rate-ratio`, `cur-MPR`, `sync-time-intrv`, the asymmetry and computation
flags and the 802.1AS version. `mean-link-delay` stays unconverted: the
`cur-MPR` column carries a bare integer with no unit and reads 0 on every
port in this testbed, so there is nothing to infer a scale from, and the raw
value is kept as `mean-link-delay-raw` rather than rescaled on a guess. That
is what a real gap looks like.

The lesson generalises past this one leaf. "The CLI does not print it" is a
claim about the CLI, and the only way to hold it is to have read all of the
output — not the part that answered the first question asked of it. §9
records this as the reason the offline test fixtures are now verbatim
captures from all five switches rather than abridged examples.

On top of the datasets, `ptp.lock_assessment()` produces the verification
gate REPORT §8 previously listed as the highest-value missing piece: a
verdict of `locked`, `out-of-tolerance`, `not-synchronised` or `unknown`,
derived from the offset from master and the per-port PTP state, with
`safe_to_schedule` as its bottom line. It is deliberately advisory and says
so -- one sample is not a synchronisation guarantee, and the honest answer
when PTP cannot be read at all is `unknown`, never "fine".

What this does **not** fix: PTP is still unreachable from NETCONF, so a CNC
cannot configure the time base through the same channel it configures Qbv,
and the read it does get is screen-scraped rather than schema-validated. The
CNC gap list records this as `impact: degraded-read-only` rather than
quietly dropping the gap once the CLI can answer.

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

This is not contradicted by the CLI collector added in §5.9. That collector exists because the NETCONF server stopped running at all, not because scraping was reconsidered as a way to talk to a working one; it drives the switch's own ISTAX CLI rather than wrapping `netopeer2-cli`; and NETCONF still wins whenever it answers.

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

### 5.9 The CLI fallback, and why it is a fallback

After the switches were updated to firmware **GA-3.06**, the NETCONF server
stopped starting. The evidence is unambiguous and worth recording precisely,
because it determines whose bug it is:

* `netconf server` **is** present in `show running-config`.
* Nothing is listening on port 830: a client gets `Connection refused`, a
  TCP RST, not a timeout -- so this is not a filter.
* From the switch's own Linux shell, `ps` shows no `netopeer2-server` and no
  `sysrepo` process, and `netstat -ltn` shows no socket on 830.

Configured but not running is a firmware regression, not a configuration
error. It is Kontron's to fix. In the meantime the testbed still needs its
topology and capabilities, so `tsn_discovery/istax.py`,
`istax_parse.py`, `ptp.py` and `cli_record.py` read the same information
over the ISTAX CLI.

**The fallback never pre-empts NETCONF.** `cli.py` probes per switch: a TCP
connect to 830, and if that opens, a real NETCONF session. Only when the
probe fails does that switch drop to the CLI, and the probe's reason is
recorded in the record and rendered in `capabilities.md` §1. A run can be
mixed -- four switches over NETCONF and one over the CLI -- and the output
is one coherent document either way. `--transport netconf` disables the
fallback so a broken switch reports as broken instead of being papered over;
`--transport cli` forces it, for testing.

**Uniformity is the design constraint.** `cli_record.build()` emits exactly
the keys `capabilities.build()` emits: same nesting, same units
(nanoseconds throughout), same decoded gate bitmasks. The CLI prints
`GateStates 0x1f` where NETCONF sends `31`, and prints `GateOperation
set-hold` where the YANG model says `set-and-hold-mac`; both are normalised
at parse time, so `topology.py`, `cnc.py` and `render.py` need no knowledge
of the transport. A test asserts the two record shapes have not drifted,
because a drift would be silent -- a consumer would read `None` where it
expected a value and nothing would raise.

**What the CLI genuinely cannot supply** is recorded as `None` with the
omission named, never guessed:

| Field | NETCONF | CLI |
|---|---|---|
| `supported-list-max` | yes | yes (`SupportedListMax`) |
| `supported-cycle-max` | yes | **not printed** |
| `supported-interval-max` | yes | **not printed** |
| YANG module list and revisions | yes | n/a -- features evidenced by which command the firmware accepted |
| PTP | **no module exists** | yes (§4) |

Losing two of the three Qbv limits is the real cost: they are what let a CNC
know whether a computed schedule is installable before it tries. The
`network_capability_envelope` therefore reports them as `null` for a
CLI-read network rather than leaving a stale or invented number in place.

Other properties carried over from the NETCONF collector deliberately:
every command's output is written to `raw/<switch>/*.txt` before anything
parses it, so `--reanalyse` works identically for both transports; failures
are isolated per switch; and the module is **read-only by construction** --
`istax.run()` refuses any command that is not a `show`, enforced at the
point of execution rather than left to convention.

Two parsing details were forced by the real output rather than by taste.
`show mac address-table` prints no dashed separator under its header, unlike
every other table, so the column geometry is inferred from the header itself
with a run of two or more spaces as the break -- splitting on whitespace
would destroy `MAC Address` and the port-range column. And `show vlan`
writes nineteen characters into a ten-dash `Interfaces` column, so the last
column of every table is treated as open-ended.

### 5.10 Exit codes

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

**`istax.py`** — the CLI transport. An SSH interactive shell (ISTAX has no
usable exec subsystem), answering the `-- more --` pager with `g` so a long
`show running-config` arrives whole, stripping ANSI, and refusing any
command that is not a `show`. It also retries SSH with the SHA-1 era
algorithms these switches still offer, which is why it connects where a
current OpenSSH client fails on key exchange.

**`istax_parse.py`** — the CLI parsers, producing the YANG shapes. Table
geometry from the dashed separator where there is one and from the header
otherwise; `key : value` blocks for the TSN status commands; running-config
split into global and per-interface. Gate masks parse from hex or decimal,
`GateOperation` maps to the `ieee802-dot1q-sched` operation names, and
`110 ms` / `4300 seconds, 500 nanoseconds` both normalise to nanoseconds.

**`ptp.py`** — one neutral PTP observation projected into `ietf-ptp`,
`ieee1588-ptp-tt` and `ieee802-dot1as-gptp`, plus the lock assessment (§4).

**`cli_record.py`** — assembles a CLI-read switch into the record
`capabilities.build()` would have produced, and derives feature support from
which commands the firmware accepted rather than from a module list.

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

1. ~~**A gPTP verification gate.**~~ **Done**, via the CLI collector:
   `ptp.lock_assessment()` returns a verdict and a `safe_to_schedule` boolean
   derived from the offset from master and the per-port PTP state (§4). Two
   caveats keep this from being finished business — it works only where a
   switch is read over the CLI, so restoring NETCONF does not restore it, and
   a single sample is not a synchronisation guarantee. A campaign should
   still re-check around every measurement point, as `i226-adaptation` does
   with `pmc`.
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

**Exercised.** 134 offline tests pass
(`python3 -m unittest discover -s tests`), covering:

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

**Exercised against real switch output.** The CLI parsers are tested on
genuine captured output from KSwitchTSN-1 (192.168.1.10, GA-3.06): `show
lldp neighbors`, `show mac address-table`, `show interface * status`,
`show vlan` and `show running-config`. From that capture the tool correctly
derives the hostname, the bridge base address, the management address, all
eight ports with their speeds and PVIDs, both VLANs, the 28 filtering-database
entries, the LLDP adjacency to KSwitchTSN-2 on Gi 1/5, and — importantly —
attaches CTRL to Gi 1/1 while declining to attach the twelve other MACs
learned through the Gi 1/5 trunk. Since the
2026-09-18 run the PTP, Qbv, Qbu and QoS fixtures are verbatim captures from
KSwitchTSN-2 as well, pager residue and all, so the tests exercise the
firmware's real output rather than a tidied version of it. One
`SYNTHETIC` fixture remains, from Microchip AN1185: no port in this testbed
enables a credit-based shaper, and the `credit: enabled` branch would
otherwise be untested.

A separate test asserts that the CLI record and the NETCONF record have the
same keys at every level that downstream code reads. That is the one failure
mode uniformity has: a drift would not raise, it would silently hand a
consumer `None`.

**Executed against all five switches, 2026-09-18.** The CLI transport met a
live session for the first time and reached all five switches. Everything
the previous version of this section listed as unverified about the CLI path
is now answered:

- The `-- more --` pager does answer to `g`, as AN001 says.
- The prompt regex matched every state on all five switches.
- `show tsn tas status` and `show ptp 0 port-state` are both accepted
  without an interface argument, so the fast path works; the per-interface
  fallback was exercised too, since the collector issues both.
- Port numbering does follow the `show interface * status` ordering.
  Independently corroborated three ways per switch: LLDP Port ID, the PTP
  port numbering, and the filtering-database port references all agree.
- The SHA-1 retry was not needed; all five negotiated on the modern path.

The run also produced three cross-checks the tool had never been able to
make before, and all three hold. Every switch's bridge address is the EUI-64
pre-image of its PTP clock identity. All five name the same grandmaster and
report steps-removed 0,1,2,3,4, which is the daisy chain LLDP independently
derived. And each switch's parent-port identity is the clock identity of its
LLDP-discovered upstream neighbour.

### 9.1 What the first live run corrected

Six defects the offline fixtures could not have exposed, because every one
of them was a place where the fixtures encoded an assumption rather than the
hardware's actual output:

1. **The pager erases its prompt with a run of spaces** and resumes writing
   on that line, so one row per paged command arrives displaced ~50 columns
   right. Column-sliced parsing read it as empty and dropped it: one
   filtering-database entry was lost on SW2. The residue is now recognised
   by geometry — content beginning past the last column's start that
   re-aligns into two or more columns when the run is removed — rather than
   by counting spaces, because `show ptp 0 current` right-aligns a genuine
   value 31 columns in.
2. **Column widths are sized to the heading, not the data.**
   `ParentPortIdentity` gets 22 dashes and holds a 23-character clock
   identity, so every clock identity lost its last character. Two identities
   differing only in the last byte would have compared equal. Fields are now
   sliced from each column's start to the *next* column's start.
3. **`spanning-tree mst name` is not the bridge address.** It looks like one
   on a switch nobody has touched, because the firmware seeds it from the
   MAC. Four of the five here carry `00-22-33-44-55-66`, and that fabricated
   address reached `capabilities.md` and `topology.md`. The address now
   comes from the static CPU entry in the filtering database, which is a
   reading, and the MST name is recorded as what it is. Worth noting
   separately: SW1 is in a different MST region name from the other four.
4. **The 802.1AS port status table was never parsed** — see §4.2.
5. **`show qos interface` was ~95% discarded.** The parser matched only
   queue shapers. It now reads the whole output, which turned up the two
   testbed findings below.
6. **A grandmaster was reported as `locked`.** SW1 reports zero offset and a
   free-running servo because it *is* the reference; calling that `locked`
   hands a CNC a verification that never happened. It now gets its own
   verdict, with its clock class and time source, so the reader can see that
   this network is disciplined to SW1's local oscillator.

### 9.2 Two findings about the testbed, not the tool

Both came out of the QoS output once it was actually read, and both matter
before any scheduling experiment:

- **No port trusts the incoming VLAN tag.** All 40 bridge ports carry
  `qos trust tag disabled` and `qos cos 0`, so every frame is classified to
  traffic class 0 whatever PCP the talker sets. A gate-control list that
  opens classes 1–7 would open them onto empty queues. This is
  configuration, not a capability gap — the hardware supports it — and it is
  recorded in the CNC gap list as `blocking-for-scheduling`.
- **The PCP-to-traffic-class map is not the identity.** Uniformly on all 40
  ports, PCP 0 maps to class 1 and PCP 1 to class 0 — the 802.1Q-recommended
  default. This invalidated a caveat the Qbu record used to print about
  holding "under the default 1:1 map". The map is now read per port and
  applied when expressing per-queue preemption as per-priority preemption.

Also surfaced rather than left silent: four ports (`Gi 1/6` on SW2–SW5) have
a link with no LLDP neighbour and no filtering-database entry. That is the
same count as the four Raspberry Pis the inventory cross-check lists as
unobserved, so they are most likely cabled there and simply silent. The
tool now reports link-up-but-unidentified ports instead of omitting them.

**Still never executed.** **No NETCONF session has ever been established
against a KSwitch D10** — and since GA-3.06 none can be, because the server
does not start. No NETCONF reply has been parsed from real hardware. For the
NETCONF path, unchanged:

- That the NETCONF server can be enabled at all on GA-3.06. It is present in
  every switch's running-config and listening on nothing; §4 and §5.9 cover
  this. On all five, not just SW1 — which is what makes it a firmware
  regression rather than a per-device mistake.
- That the credentials in `SYSTEM.md` (`netconf` / `geheim`) are correct.
- **That LLDP `remote-systems-data` is populated over NETCONF.** The CLI
  path has now proved LLDP adjacency data exists and is correct; whether the
  sysrepo plugin exposes it is still unknown.
- The exact LLDP tree shape over NETCONF.
- Whether `port-ref` in filtering-database entries maps to
  `bridge-port/port-number` or to `if-index` on this firmware.
- Whether the frame-preemption model is populated, and in which shape.

**Conclusion: the CLI path is now verified end to end against all five
switches of this testbed, and its offline fixtures are verbatim captures
from that run rather than abridged examples. The NETCONF path remains
verified against the documented data model and unverifiable against the
hardware until Kontron fixes the server.**

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
