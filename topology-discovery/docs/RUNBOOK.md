# Runbook — NETCONF topology discovery and TSN capability retrieval

Operating manual for the discovery subproject. Covers prerequisites, switch
preparation, installation, preflight, running, reading the output,
troubleshooting, and how to feed the result into a CNC.

For *why* it is built this way — the design decisions, the PTP finding, and
an honest statement of what has and has not been verified — see
`REPORT.md`. Read **REPORT.md §9** before trusting any output: this tool has
never been run against a real KSwitch D10.

> **Discovery is read-only.** It issues only `<get>` and `<get-config>`.
> There is no `edit-config` code path. Running it cannot change a switch's
> configuration.

---

## 1. Prerequisites

**Hardware**

- 5 × Kontron KSwitch D10 MMT (S1921), reachable on 192.168.1.10–.14.
- UP-1 (192.168.1.61) on the same L2 network, or any Linux host with a route
  to the switch management addresses.

**Software on UP-1**

- Python 3.8 or newer, with `python3-venv`
  (`sudo apt-get install python3-venv` on Ubuntu).
- Network access to PyPI for `ncclient` and `lxml` — or the offline
  procedure in §3.3.
- `netopeer2-cli` is **optional**. It is already installed in
  `/home/ivank/netopeer2` and preflight will use it as a cross-check, but
  discovery does not need it.

**Switch access**

- The NETCONF server **enabled** on every switch (§2 — it does not start on
  its own). On firmware GA-3.06 it does not start even when enabled; §6
  covers what the tool does about that.
- NETCONF credentials. `SYSTEM.md` records `netconf` / `geheim`; AN001 v1.3
  documents the factory default as `netconf` / `netconf`.
- **Switch CLI credentials** — the `admin` account you would use for
  `ssh admin@192.168.1.10`. Needed by the CLI fallback (§6.1), and not the
  same as the NETCONF credentials.

**Reference addresses** (from `SYSTEM.md`)

| Device | IPv4 | MAC |
|---|---|---|
| SW1 | 192.168.1.10 | `00:80:82:b9:65:33` |
| SW2 | 192.168.1.11 | `00:80:82:bd:25:7c` |
| SW3 | 192.168.1.12 | `00:80:82:bd:25:a2` |
| SW4 | 192.168.1.13 | `00:80:82:bd:25:80` |
| SW5 | 192.168.1.14 | `00:80:82:bd:25:72` |
| UP-1 | 192.168.1.61 / .62 | `00:07:32:c1:43:30` / `:31` |
| UP-2 | 192.168.1.71 / .72 | `00:07:32:c1:29:69` / `:6a` |

---

## 2. Switch preparation

### 2.1 Enable the NETCONF server

Per Kontron AN001 §5, **the NETCONF server is not started automatically.**
On each switch's ISTAX CLI:

```
# configure terminal
(config)# netconf server
```

Confirm the management address:

```
# show ip interface
Interface Address            Method Status
--------- ------------------ ------ ------
VLAN 1    192.168.1.10/24    Static UP
```

To make this survive a reboot, save the configuration from the CLI or web
UI. This matters: see §2.3.

### 2.2 Enable LLDP

Topology discovery depends on it. On each switch, LLDP must be enabled and
transmitting on the ports that carry inter-switch links. In ISTAX:

```
# configure terminal
(config)# lldp
(config)# interface GigabitEthernet 1/1-6
(config-if)# lldp transmit
(config-if)# lldp receive
```

After enabling, **wait at least one LLDP transmit interval** — the default
is 30 s, and a neighbour entry does not exist until an advertisement has
been received. Running discovery immediately after enabling LLDP will show
fewer links than exist.

Verify from the CLI before blaming the tool:

```
# show lldp neighbors
```

If that shows neighbours but discovery does not, the plugin is not
populating `remote-systems-data` over NETCONF — see §9.6.

### 2.3 Know the datastore caveat

From AN001 §Data Stores, and it will bite you eventually:

- The sysrepo plugin reads the running configuration from the switch
  management software **only when the plugin starts**. Changes made in the
  CLI or web UI afterwards are **not visible over NETCONF** until the plugin
  restarts.
- Changes written over NETCONF *are* visible in the CLI and web UI, but are
  **not persistent** until saved from the CLI or web UI.

Practical consequence for discovery: if you change something on a switch
from the CLI and then run discovery, you may read the pre-change state. If
in doubt, restart the NETCONF server (or the switch) before a run that has
to be authoritative. This is recorded permanently in the CNC gap list as
`datastore-coherence`.

---

## 3. Installation

The subproject lives at `tsn-testbed/topology-discovery/`. It is
self-contained and does not touch `i226-adaptation/`.

### 3.1 Get the current code

```bash
cd /home/ivank/tsn-testbed
git pull
```

The tool and this runbook are versioned together with the rest of the
testbed, so pull before a setup and before reporting a problem — a mismatch
between the procedure you are reading and the code you are running is a
common source of confusion.

A pull will not disturb anything a previous run produced: `results/`,
`.venv/` and `requirements.lock` are all ignored by the repository. If the
pull is refused because the working tree has local edits, deal with them
first:

```bash
git status --short
git stash                  # or: git commit -am "local changes"
git pull
git stash pop              # if stashed
```

### 3.2 Normal install

```bash
cd /home/ivank/tsn-testbed/topology-discovery
./scripts/setup_env.sh
```

This creates `.venv/` inside the subproject and installs `ncclient` and
`lxml` into it. Nothing is installed system-wide. It prints the installed
versions and `environment OK` on success.

### 3.3 Offline install

If the testbed network has no route to PyPI, fetch the wheels on a machine
that does:

```bash
# on a machine with internet, same Python major.minor as UP-1
mkdir wheels && pip download -d wheels ncclient lxml
# copy the wheels/ directory to UP-1, then:
WHEELS=/path/to/wheels ./scripts/setup_env.sh
```

`pip download` pulls the transitive dependencies too (paramiko,
cryptography, bcrypt, pynacl, cffi, pycparser). Check the Python version
matches — wheels are version- and ABI-specific.

### 3.4 Record exact versions

For a run that must be reproducible later:

```bash
.venv/bin/pip freeze > requirements.lock
```

---

## 4. Preflight

**Run this before the first discovery, after any rewiring, and whenever
discovery reports a switch unreachable.**

```bash
./scripts/preflight.sh
```

Five stages, each independently diagnostic:

1. **Toolchain** — python3, the virtualenv, ncclient, lxml, netopeer2-cli.
2. **Inventory** — `SYSTEM.md` is found and parses.
3. **ICMP** — every address in `SYSTEM.md`. A switch that does not answer is
   a failure; an endpoint that does not answer is a warning, because it may
   simply be powered off.
4. **TCP/830** — the NETCONF port on every switch. Closed here almost always
   means the NETCONF server was never enabled (§2.1).
5. **NETCONF session** — a real `<hello>` exchange per switch, reporting the
   session id, the module count and which TSN modules are advertised. This
   is the authoritative check.

If `netopeer2-cli` is present a sixth stage cross-checks the first switch by
hand. It is best-effort: `netopeer2-cli` prompts for a password
interactively, so a `WARN` there is normal and stage 5 is what counts.

Override targets and credentials with environment variables:

```bash
INVENTORY=/path/to/SYSTEM.md \
NETCONF_USER=netconf NETCONF_PASSWORD=... \
NETCONF_PORT=830 ./scripts/preflight.sh
```

Exit 0 = all passed, 1 = failures, 2 = cannot run.

---

## 5. Running discovery

```bash
./scripts/run_discovery.sh
```

That is the whole thing. It discovers every switch listed in `SYSTEM.md`,
writes a timestamped run directory under `results/`, and copies the six
published documents into `topology/`.

Useful variations:

```bash
# one switch, named
./scripts/run_discovery.sh --switch SW1=192.168.1.10

# a subset
./scripts/run_discovery.sh --switch SW1=192.168.1.10 --switch SW2=192.168.1.11

# capture an unfiltered <get> as well — bigger, but catches models the tool
# did not think to ask for. Worth doing on the first real run.
./scripts/run_discovery.sh --full-dump

# sequential, longer timeout — for a slow or flaky switch
./scripts/run_discovery.sh --workers 1 --timeout 60

# do not overwrite topology/ (e.g. a one-off probe)
./scripts/run_discovery.sh --no-publish

# everything
./scripts/run_discovery.sh --help
```

**Credentials.** The lab default is compiled in, matching `SYSTEM.md`.
Override with the environment, not the command line — a password on the
command line ends up in shell history and in `/proc`:

```bash
NETCONF_PASSWORD='...' ./scripts/run_discovery.sh
```

**Exit codes.** `0` every switch answered and the topology agrees with
`SYSTEM.md`; `1` discovery ran but something needs you; `2` it could not
run. Usable as a gate:

```bash
./scripts/run_discovery.sh --quiet || { echo "network not as expected"; exit 1; }
```

**Runtime** is dominated by SSH session setup, roughly 2–5 s per switch in
parallel. `--full-dump` adds a few seconds per switch.

---

## 6. When NETCONF does not answer

Since firmware **GA-3.06** the switches' NETCONF server no longer starts,
even though `netconf server` is still in the running configuration. The tool
handles that itself: it probes NETCONF on every switch and falls back to
reading the same information over the ISTAX CLI where the probe fails. You
do not have to choose a transport — but you do have to supply CLI
credentials, or the fallback cannot log in.

### 6.1 Set the CLI credentials

```bash
export ISTAX_USER=admin              # default; the switch admin account
read -rs ISTAX_PASSWORD; export ISTAX_PASSWORD
./scripts/run_discovery.sh
```

`read -rs` keeps the password out of your shell history. These are the
*switch CLI* credentials (the account you use for `ssh admin@192.168.1.10`),
not the NETCONF ones — `netconf`/`geheim` is a NETCONF account and will not
log in to the CLI.

### 6.2 Choosing a transport explicitly

```bash
./scripts/run_discovery.sh                        # auto (default)
./scripts/run_discovery.sh --transport netconf    # never fall back
./scripts/run_discovery.sh --transport cli        # skip the probe
```

Use `--transport netconf` when you want a broken switch to *report* as
broken — for example when checking whether a firmware fix has landed. Use
`--transport cli` to exercise the fallback while NETCONF is working.

A run can be mixed. Four switches over NETCONF and one over the CLI produces
one coherent set of documents, and every record says which transport
produced it.

### 6.3 What you lose on the CLI

The output shape is identical — same keys, same units, same decoded gate
masks — so `topology.md`, `capabilities.md` and `cnc-input.json` read the
same way. What differs:

| | NETCONF | CLI |
|---|---|---|
| Schema validation | yes | none — output is parsed text |
| Format stability | versioned YANG modules | can change between firmware releases |
| `supported-list-max` (Qbv) | yes | yes |
| `supported-cycle-max`, `supported-interval-max` | yes | **not printed — reported as null** |
| YANG module list | yes | n/a; features evidenced by which command was accepted |
| **PTP** | **no module exists** | **yes** — see §6.4 |
| Transactional writes (candidate, validate, rollback) | yes | none |

The two missing Qbv limits matter for a CNC: they are what say whether a
computed schedule will fit. `network_capability_envelope` reports them as
`null` on a CLI-read network rather than carrying a stale number.

### 6.4 What you gain: PTP

PTP is the one thing the CLI has and NETCONF does not, on any firmware.
`capabilities.md` §3 gains a table with the profile, offset from master,
mean path delay, steps removed, servo state and a **lock verdict**:

| Verdict | Meaning |
|---|---|
| `locked` | offset within tolerance (default 1 µs) — `safe_to_schedule: true` |
| `out-of-tolerance` | PTP running but the offset is too large |
| `not-synchronised` | every PTP port disabled, initializing or faulty |
| `unknown` | PTP could not be read at all |

It is advisory and based on one sample. Before trusting a Qbv schedule, keep
checking gPTP around each measurement point the way `i226-adaptation`
already does with `pmc`.

The same reading is written into `capabilities.json` under
`ptp.models`, projected into **three** standard YANG shapes at once:

| Key | Module | Models | Use it if |
|---|---|---|---|
| `ietf-ptp` | RFC 8575, rev 2019-05-06 | IEEE 1588-2008 | your consumer expects `offset-from-master`, `mean-path-delay`, flat `port-ds-list` |
| `ieee1588-ptp-tt` | rev 2023-08-14 | IEEE 1588-2019 | your consumer expects `offset-from-time-transmitter`, `mean-delay`, nested `ports/port/port-ds` |
| `ieee802-dot1as-gptp` | rev 2025-02-04 | IEEE 802.1AS-2020 | you want the gPTP-specific leaves (`current-log-gptp-cap-interval`, `is-measuring-delay`) |

They are three spellings of one observation, not three readings — the same
measured offset appears under both the 2008 and 2019 leaf names. The
802.1AS module defines no top-level containers; it augments the 1588-2019
tree, so its projection carries only the augmentation leaves and expects
`ieee1588-ptp-tt` underneath it.

Each projection lists `unavailable` (nodes the model defines that the CLI
does not print) and `derived` (values filled by inference, with the
inference stated). Notably `as-capable` is **absent, not guessed** — the
CLI never prints it, and a fabricated value would be a time base nobody
verified. REPORT §4.1 explains the choice.

### 6.5 Confirming the NETCONF regression on a switch

Worth doing once per switch, and worth quoting to Kontron:

```
KSwitchTSN-1# show running-config | include netconf
netconf server                     <- configured

KSwitchTSN-1# debug system shell
~ # ps | grep -E 'netopeer|sysrepo'  <- no process
~ # netstat -ltn | grep 830          <- no listener
```

Configured, not running, nothing listening. From UP-1 the same fault shows
as `Connection refused` — a TCP reset, not a timeout, so it is not a
firewall. The tool records all of this: `capabilities.md` §1 states, per
switch, that `netconf server` is present in the running-config while the
server did not answer.

---

## 7. Output

```
results/<run-id>/
  topology.json          the graph, machine-readable
  topology.md            the graph, human-readable, with a Mermaid diagram
  topology.dot           Graphviz source
  capabilities.json      full per-switch record
  capabilities.md        per-switch summary and feature matrix
  cnc-input.json         normalised CNC input document
  run.json               run metadata, per-switch errors and durations
  switches/<name>.json   one switch each, for diffing between runs
  raw/<name>/*.xml       every NETCONF reply, verbatim
  raw/<name>/hello-capabilities.json
topology/                a copy of the six published documents, always latest
```

Render the Graphviz version:

```bash
dot -Tpng topology/topology.dot -o topology.png
dot -Tsvg topology/topology.dot -o topology.svg
```

`topology.md` embeds a Mermaid diagram that renders directly in GitHub and
most Markdown viewers, so a picture is usually available without Graphviz.

**`topology/` is generated.** Do not hand-edit it; re-run discovery instead.
`results/` is excluded from git except for the directory itself — commit a
specific run only if it is worth keeping as a reference.

---

## 8. Reading the output

### 7.1 Start with the console summary

```
Switches contacted : 5/5
  SW1    192.168.1.10     12 modules, 8 bridge ports, Qbv on 8, Qbu on 8
  ...
Links              : 12 (4 LLDP / 8 FDB)
Inventory agreement: yes
PTP over NETCONF   : NOT EXPOSED
CNC gaps recorded  : 5
```

`PTP over NETCONF : NOT EXPOSED` is expected on this hardware — see
REPORT.md §4. It is a property of the switch firmware, not a fault in the
run.

### 7.2 `topology.md`

§3 is the link table. The columns that matter:

- **Method** — `lldp` is a real discovered link. `fdb` is an endpoint
  attachment inferred from a filtering-database entry; it tells you a device
  was seen on that port, not that nothing else is behind it.
- **Both ends** — `yes` means both switches reported the link. `no` means
  only one did: the far end may not run LLDP, may not have aged in yet, or
  may not have been queried. Treat one-sided links as provisional.
- **Resolution** — how the remote was identified. `bridge-address match` is
  the strongest. Anything mentioning *weak* deserves a manual look.

§5 is the cross-check against `SYSTEM.md`. A switch listed there that
discovery did not see is a fault. An endpoint not observed usually is not —
an end station that neither runs LLDP nor has transmitted recently has no
FDB entry and is invisible to both methods (§9.7).

### 7.3 `capabilities.md`

- §2 is the feature matrix. `**no**` for QCI, QAV, QCC and PTP is expected;
  `**no**` for QBV or QBU on a switch where the others have it means that
  switch is on different firmware — check §1 of the same file for its module
  count.
- §3 is the Qbv envelope: the largest gate-control list, cycle time and
  interval that will work **everywhere** in this network. On this hardware
  expect 128 entries, ≈33.5 ms, ≈33.5 ms.
- §4 is per-port state.
- **Warnings** affect a port whose gate is enabled — act on these.
  **Notes** are inert, typically the factory default `admin-cycle-time` of
  100 ms exceeding the supported maximum of ≈33.5 ms on ports whose gate is
  off. That is cosmetic until you enable a gate on such a port without
  setting a cycle time.

### 7.4 `cnc-input.json`

The interchange document, `schema: tsn-testbed/cnc-input/v1`. The three
fields to look at first:

```bash
python3 -m json.tool topology/cnc-input.json | less

# the envelope any schedule must fit
jq '.network_capability_envelope' topology/cnc-input.json

# what a CNC cannot do here
jq '.gaps[] | {capability, impact}' topology/cnc-input.json

# every port that carries an inter-switch link
jq '.network.bridges[].ports[] | select(.role=="inter-switch")
    | {name, speed_mbps, neighbour: .neighbour.node}' topology/cnc-input.json
```

---

## 9. Troubleshooting

Error kinds come from `run.json` and the console. Each maps to one cause.

### 9.1 `connection-refused` / TCP 830 closed

Nothing is listening on the NETCONF port. Two different causes, and the
running-config tells them apart:

```
KSwitchTSN-1# show running-config | include netconf
```

- **No output** — the server was never enabled. §2.1 enables it.
- **`netconf server`** — configured but not running. This is the **GA-3.06
  firmware regression**: the daemon does not start. Nothing you can
  configure fixes it; it is the manufacturer's to repair, and §6.5 gathers
  the evidence for a ticket.

Either way discovery keeps working: with CLI credentials set (§6.1) the run
falls back automatically and says so rather than failing.

### 9.2 `timeout` / `unreachable`

No route or the switch is down. Check with `ping 192.168.1.10`. If ICMP
works but NETCONF times out, a firewall or ACL is filtering 830, or the
switch is too loaded to accept an SSH session — retry with
`--workers 1 --timeout 60`.

### 9.3 `auth-failed`

Wrong username or password. The defaults come from `SYSTEM.md` (`netconf` /
`geheim`). Override with `NETCONF_USER` / `NETCONF_PASSWORD`. If the
credentials are right, check the account still exists on the switch and has
NETCONF access.

### 9.4 `ssh-error` during key exchange

Embedded SSH stacks sometimes offer only algorithms that recent paramiko
disables by default. Test the raw transport first:

```bash
ssh -v -p 830 -s netconf@192.168.1.10 netconf
```

If that fails on `no matching key exchange method` or similar, note the
algorithm it names. A per-host SSH config entry re-enabling it usually fixes
the interactive case; for ncclient, `netopeer2-cli` is the fallback path for
that switch and `--reanalyse` can still process whatever was captured.

### 9.5 `rpc-error` on one subtree, others fine

Normal and informative: the switch does not implement that model. It is
recorded in `run.json` and reflected in the feature matrix. Nothing to fix.

### 9.6 LLDP returns nothing / no switch-to-switch links

The most likely real problem, and REPORT.md §9 flags it as the largest
unverified risk. Work through it in this order:

1. Is LLDP enabled and transmitting on the relevant ports? (§2.2)
2. Has at least one transmit interval elapsed since enabling it? Default
   30 s.
3. Does `show lldp neighbors` on the switch CLI show neighbours?
   - **No** → the problem is LLDP on the switch, not this tool.
   - **Yes** → the plugin is not exposing `remote-systems-data` over
     NETCONF. Confirm directly:
     ```bash
     ./scripts/run_discovery.sh --switch SW1=192.168.1.10 --full-dump
     grep -ic 'remote-systems-data' results/*/raw/SW1/full-dump.xml
     ```
     If the full dump has no remote systems data either, the firmware does
     not populate it and switch-to-switch discovery over NETCONF is not
     available on this release. In that case the topology file will contain
     endpoint attachments only; record the inter-switch wiring in
     `SYSTEM.md` and treat the discovered part as a partial result. Note it
     as a firmware limitation, not a tool defect.

### 9.7 An endpoint does not appear

It has no filtering-database entry, because it has not transmitted recently
or the entry has aged out. Make it talk and re-run:

```bash
ping -c 3 192.168.1.51          # from UP-1
./scripts/run_discovery.sh
```

Devices that never transmit unprompted — the NXP MIMXRT1170 boards in an
idle state — may need traffic generated from them or a shorter FDB aging
time.

### 9.8 Discovery disagrees with `SYSTEM.md`

That is the tool working. Discovery reports the network; `SYSTEM.md` reports
what someone wrote down. Check the cabling, then update `SYSTEM.md` — do not
adjust the tool to match the document.

### 9.9 A parser looks wrong

Do not go back to the bench. The raw XML is already saved:

```bash
less results/<run-id>/raw/SW1/interfaces.xml
# fix the parser, then:
./scripts/run_discovery.sh --reanalyse results/<run-id>
```

`--reanalyse` re-runs the entire parse and render pipeline against the saved
bytes and contacts nothing.

### 9.10 The CLI fallback cannot log in

`auth-failed` on the CLI transport means the *switch* credentials are wrong.
`ISTAX_USER` defaults to `admin`; the NETCONF account (`netconf`) is not a
CLI user and will not work there. Test by hand:

```bash
ssh admin@192.168.1.10
```

If that itself fails during key exchange, see §9.11.

### 9.11 `ssh-algorithm-mismatch` on the CLI transport

The switch offers only SHA-1 era algorithms and a current client refuses
them. The tool already retries with those re-enabled, so this error means
the retry failed too. Check what plain SSH does:

```bash
ssh -o KexAlgorithms=+diffie-hellman-group1-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o PubkeyAcceptedAlgorithms=+ssh-rsa admin@192.168.1.10
```

If that works and the tool does not, report the exact error rather than
working around it. `--no-legacy-ssh` disables the retry to get the raw
failure.

### 9.12 A CLI read returns nothing for Qbv, Qbu or PTP

Two harmless cases and one worth chasing:

- **No schedule configured.** `show tsn tas status` has nothing to say about
  a port with no gate control list. The record shows the port as Qbv-capable
  with an empty list, which is correct.
- **Command not in this release.** The record marks the feature
  `supported: false` with the evidence *the firmware rejected `<command>`*.
  That is a discovery result, not a failure.
- **Command timed out.** `show running-config` on a busy switch can exceed
  the per-command timeout. Raise it with `--timeout 60`.

Whichever it is, the raw text is in `results/<run-id>/raw/<switch>/*.txt` and
`--reanalyse` re-parses it without touching the switch.

### 9.13 `ncclient not importable`

The virtualenv is missing or the wrong python is being used. Re-run
`./scripts/setup_env.sh`. `run_discovery.sh` prefers `.venv/bin/python3`
automatically.

---

## 10. Changing the setup

### Different addresses or new switches

Edit the address table in `SYSTEM.md`. Names matching `SW<n>` are treated as
switches and contacted; everything else is an endpoint. No code change.

### A switch on a non-standard port

```bash
NETCONF_PORT=2830 ./scripts/run_discovery.sh
```

### Discovering a switch not in `SYSTEM.md`

```bash
./scripts/run_discovery.sh --switch NEWSW=192.168.1.20 --no-publish
```

It will show up in the cross-check as *discovered but not in SYSTEM.md*,
which is the correct complaint.

### Running from a different host

Anything with a route to the switch management addresses works. Copy the
subproject, run `setup_env.sh`, and point `--inventory` at `SYSTEM.md`.

### Scheduling periodic discovery

```bash
# crontab -e — hourly, keeping runs, alerting on change
0 * * * * cd /home/ivank/tsn-testbed/topology-discovery && \
  ./scripts/run_discovery.sh --quiet >> results/cron.log 2>&1 || \
  echo "topology discovery flagged a problem" | mail -s "TSN topology" you@example
```

Runs accumulate under `results/`; prune them by age.

---

## 11. Feeding a CNC

`cnc-input.json` is the network half of the IEEE 802.1Qcc fully-centralized
model. To build on it:

1. **Read the envelope first.** `network_capability_envelope` bounds every
   schedule. On this hardware: 128 gate-control entries, ≈33.5 ms maximum
   cycle, ≈33.5 ms maximum interval, 8 traffic classes.
2. **Read the gaps.** `gaps[]` says what this interface cannot do. The
   `ptp` gap is `blocking-for-verification`: you can install a schedule but
   not verify its time base over NETCONF.
3. **Verify gPTP out of band before trusting any schedule.**
   `i226-adaptation` already does this with `pmc` on both PCs, before and
   after every measurement point. Reuse that check; do not assume
   synchronisation.
4. **Use `ports[].role`** to separate inter-switch links (where contention
   happens, and where a schedule usually matters) from edge ports.
5. **Write with `edit-config` against `ieee802-dot1q-sched`.** AN001 §16–21
   has the exact XML. Two things to remember: `config-change` must be set
   for a new gate-control list to take effect, and a configuration written
   over NETCONF is not persistent until saved from the CLI or web UI (§2.3).
6. **Re-run discovery after writing** and compare
   `switches/<name>.json` before and after. That is what the per-switch files
   are for.

---

## 12. If LLDP is unavailable

If §9.6 concludes the firmware does not expose LLDP remote systems data,
discovery still produces value, and this is the degraded procedure:

- Endpoint attachment from the FDB still works and is the part that changes
  most often.
- Capability retrieval — the entire Qbv/Qbu/VLAN/QoS surface, which is what
  a CNC needs most — is unaffected.
- Inter-switch links must be recorded by hand. Put them in `SYSTEM.md` and
  note in the topology file that the switch-to-switch portion is documented
  rather than discovered.
- Consider raising it with Kontron: the module is advertised, so a populated
  `remote-systems-data` is a reasonable expectation of a future release.

Do not try to infer inter-switch links from the FDB. A MAC learned on a
trunk says nothing about what is directly attached to it, and a topology
built that way is confidently wrong — REPORT.md §5.3.

---

## 13. Security notes

- The NETCONF password defaults to the lab value recorded in `SYSTEM.md`.
  This is a closed test network; on any network that is not, set
  `NETCONF_PASSWORD` in the environment and do not commit a real credential.
- `hostkey_verify` is **disabled**. The switches' host keys are not managed
  and change on firmware update. Acceptable on an isolated bench, not
  acceptable on a routed network — REPORT.md §5.1 and `netconf.py` mark the
  line.
- Raw captures contain the full configuration of each switch, including
  VLAN and MAC tables. They are not secret in this testbed, but treat
  `results/` accordingly if that changes.

---

## 14. Testing without hardware

The full pipeline can be exercised offline, which is how it was developed:

```bash
# 100 tests: parsers for both transports, live session path, topology,
# CNC document, and the NETCONF-vs-CLI record uniformity check
python3 -m unittest discover -s tests -v

# NETCONF: a synthetic five-switch capture, analysed end to end
python3 tests/make_fixture.py --out results/synthetic-001
python3 -m tsn_discovery.cli --reanalyse results/synthetic-001 \
        --inventory ../SYSTEM.md --no-publish

# CLI: split a real pasted terminal session into per-command captures
python3 tests/make_cli_fixture.py --out results/cli-fixture \
        --from-transcript /path/to/your/session.txt
python3 -m tsn_discovery.cli --reanalyse results/cli-fixture \
        --inventory ../SYSTEM.md --no-publish
```

`make_cli_fixture.py` is the more useful of the two day to day: paste a
session of `show` commands from any switch into a file and it becomes a
replayable capture. Commands your transcript did not contain are filled from
the Microchip AN1185 and AN1295 worked examples, so the remaining parsers
are still exercised.

Everything not taken from your transcript is from the vendor's reference
hardware or fabricated. Each run directory carries a `SYNTHETIC` marker
naming the origin of every file. Never cite such a run as a property of the
testbed — the same interlock as
`i226-adaptation/analysis/make_fixture.py`.

`--reanalyse` also works on any real run directory, so a parser can be fixed
and re-run against output already captured, without going back to the
bench.

---

## 15. Reproducibility checklist

Before recording a discovery run as a reference:

- [ ] `scripts/preflight.sh` exits 0.
- [ ] LLDP has been enabled for longer than one transmit interval on every
      relevant port.
- [ ] Every endpoint you expect to see has transmitted recently (§9.7).
- [ ] The NETCONF server has been restarted since the last CLI or web-UI
      configuration change (§2.3).
- [ ] `run_discovery.sh` exits 0, or the non-zero reason is understood and
      recorded.
- [ ] The transport each switch used is recorded (`capabilities.md` §1). A run
      that fell back to the CLI is not directly comparable with one that did
      not — two of the Qbv capability limits are missing from a CLI read.
- [ ] `topology.md` §5 shows no unexplained discrepancy.
- [ ] `capabilities.md` §2 shows the same feature set on all five switches —
      a difference means a firmware mismatch.
- [ ] `.venv/bin/pip freeze > requirements.lock` if the run must be
      reproducible later.
- [ ] The run directory under `results/` is kept, raw XML included.
