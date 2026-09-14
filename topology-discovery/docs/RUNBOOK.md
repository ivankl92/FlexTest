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
  procedure in §3.2.
- `netopeer2-cli` is **optional**. It is already installed in
  `/home/ivank/netopeer2` and preflight will use it as a cross-check, but
  discovery does not need it.

**Switch access**

- The NETCONF server **enabled** on every switch (§2 — it does not start on
  its own).
- Credentials. `SYSTEM.md` records `netconf` / `geheim`.

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
populating `remote-systems-data` over NETCONF — see §8.6.

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

### 3.1 Normal install

```bash
cd /home/ivank/tsn-testbed/topology-discovery
./scripts/setup_env.sh
```

This creates `.venv/` inside the subproject and installs `ncclient` and
`lxml` into it. Nothing is installed system-wide. It prints the installed
versions and `environment OK` on success.

### 3.2 Offline install

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

### 3.3 Record exact versions

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

## 6. Output

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

## 7. Reading the output

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
FDB entry and is invisible to both methods (§8.7).

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

## 8. Troubleshooting

Error kinds come from `run.json` and the console. Each maps to one cause.

### 8.1 `connection-refused` / TCP 830 closed

The NETCONF server is not running. It does not start automatically — §2.1.
Verify from the switch CLI, then re-run preflight.

### 8.2 `timeout` / `unreachable`

No route or the switch is down. Check with `ping 192.168.1.10`. If ICMP
works but NETCONF times out, a firewall or ACL is filtering 830, or the
switch is too loaded to accept an SSH session — retry with
`--workers 1 --timeout 60`.

### 8.3 `auth-failed`

Wrong username or password. The defaults come from `SYSTEM.md` (`netconf` /
`geheim`). Override with `NETCONF_USER` / `NETCONF_PASSWORD`. If the
credentials are right, check the account still exists on the switch and has
NETCONF access.

### 8.4 `ssh-error` during key exchange

Embedded SSH stacks sometimes offer only algorithms that recent paramiko
disables by default. Test the raw transport first:

```bash
ssh -v -p 830 -s netconf@192.168.1.10 netconf
```

If that fails on `no matching key exchange method` or similar, note the
algorithm it names. A per-host SSH config entry re-enabling it usually fixes
the interactive case; for ncclient, `netopeer2-cli` is the fallback path for
that switch and `--reanalyse` can still process whatever was captured.

### 8.5 `rpc-error` on one subtree, others fine

Normal and informative: the switch does not implement that model. It is
recorded in `run.json` and reflected in the feature matrix. Nothing to fix.

### 8.6 LLDP returns nothing / no switch-to-switch links

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

### 8.7 An endpoint does not appear

It has no filtering-database entry, because it has not transmitted recently
or the entry has aged out. Make it talk and re-run:

```bash
ping -c 3 192.168.1.51          # from UP-1
./scripts/run_discovery.sh
```

Devices that never transmit unprompted — the NXP MIMXRT1170 boards in an
idle state — may need traffic generated from them or a shorter FDB aging
time.

### 8.8 Discovery disagrees with `SYSTEM.md`

That is the tool working. Discovery reports the network; `SYSTEM.md` reports
what someone wrote down. Check the cabling, then update `SYSTEM.md` — do not
adjust the tool to match the document.

### 8.9 A parser looks wrong

Do not go back to the bench. The raw XML is already saved:

```bash
less results/<run-id>/raw/SW1/interfaces.xml
# fix the parser, then:
./scripts/run_discovery.sh --reanalyse results/<run-id>
```

`--reanalyse` re-runs the entire parse and render pipeline against the saved
bytes and contacts nothing.

### 8.10 `ncclient not importable`

The virtualenv is missing or the wrong python is being used. Re-run
`./scripts/setup_env.sh`. `run_discovery.sh` prefers `.venv/bin/python3`
automatically.

---

## 9. Changing the setup

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

## 10. Feeding a CNC

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

## 11. If LLDP is unavailable

If §8.6 concludes the firmware does not expose LLDP remote systems data,
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

## 12. Security notes

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

## 13. Testing without hardware

The full pipeline can be exercised offline, which is how it was developed:

```bash
# 38 tests: parsers, live session path, topology, CNC document
python3 -m unittest discover -s tests -v

# generate a synthetic five-switch capture and analyse it end to end
python3 tests/make_fixture.py --out results/synthetic-001
python3 -m tsn_discovery.cli --reanalyse results/synthetic-001 \
        --inventory ../SYSTEM.md --no-publish
```

Everything the fixture generator writes is fabricated. Each synthetic run
directory carries a `SYNTHETIC` marker file. Never cite a synthetic run as a
property of the testbed — the same interlock as
`i226-adaptation/analysis/make_fixture.py`.

---

## 14. Reproducibility checklist

Before recording a discovery run as a reference:

- [ ] `scripts/preflight.sh` exits 0.
- [ ] LLDP has been enabled for longer than one transmit interval on every
      relevant port.
- [ ] Every endpoint you expect to see has transmitted recently (§8.7).
- [ ] The NETCONF server has been restarted since the last CLI or web-UI
      configuration change (§2.3).
- [ ] `run_discovery.sh` exits 0, or the non-zero reason is understood and
      recorded.
- [ ] `topology.md` §5 shows no unexplained discrepancy.
- [ ] `capabilities.md` §2 shows the same feature set on all five switches —
      a difference means a firmware mismatch.
- [ ] `.venv/bin/pip freeze > requirements.lock` if the run must be
      reproducible later.
- [ ] The run directory under `results/` is kept, raw XML included.
