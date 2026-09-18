# Switch capabilities — FlexTest TSN testbed

Generated 2026-09-18T15:27:05.866250+00:00 by `tsn_discovery` 1.0.0 (run `20260918-152705`).

**This file is generated.** Every value below came from a NETCONF reply captured in `raw/`; nothing is inferred from documentation.

## 1. Reachability and transport

| Switch | Host | Transport | Reachable | Session | YANG modules | :candidate | :writable-running | :xpath |
|---|---|---|---|---|---|---|---|---|
| SW1 | 192.168.1.10 | **CLI** | yes | - | n/a | n/a | n/a | n/a |
| SW2 | 192.168.1.11 | **CLI** | yes | - | n/a | n/a | n/a | n/a |
| SW3 | 192.168.1.12 | **CLI** | yes | - | n/a | n/a | n/a | n/a |
| SW4 | 192.168.1.13 | **CLI** | yes | - | n/a | n/a | n/a | n/a |
| SW5 | 192.168.1.14 | **CLI** | yes | - | n/a | n/a | n/a | n/a |

> **Read over the switch CLI, not NETCONF.** SW1, SW2, SW3, SW4, SW5 did not answer on the NETCONF port, so the values below were parsed from `show` command output. They are field-for-field the same shape as a NETCONF read, but they carry no schema validation, the output format is not versioned, and two Qbv limits are unavailable (see §3). 
>
> - **SW1**: NETCONF did not respond  
>   `netconf server` **is** present in the running-config, so this is a firmware fault rather than a missing setting.
>
> - **SW2**: NETCONF did not respond  
>   `netconf server` **is** present in the running-config, so this is a firmware fault rather than a missing setting.
>
> - **SW3**: NETCONF did not respond  
>   `netconf server` **is** present in the running-config, so this is a firmware fault rather than a missing setting.
>
> - **SW4**: NETCONF did not respond  
>   `netconf server` **is** present in the running-config, so this is a firmware fault rather than a missing setting.
>
> - **SW5**: NETCONF did not respond  
>   `netconf server` **is** present in the running-config, so this is a firmware fault rather than a missing setting.

## 2. TSN feature support

Derived at runtime from each switch's `<hello>` capability list and `/ietf-netconf-monitoring:netconf-state/schemas`.

| Switch | BRIDGE | QBV | QBU | QCI | QAV | QCC | LLDP | PTP |
|---|---|---|---|---|---|---|---|---|
| SW1 | yes | yes | yes | yes | yes | **no** | yes | yes |
| SW2 | yes | yes | yes | yes | yes | **no** | yes | yes |
| SW3 | yes | yes | yes | yes | yes | **no** | yes | yes |
| SW4 | yes | yes | yes | yes | yes | **no** | yes | yes |
| SW5 | yes | yes | yes | yes | yes | **no** | yes | yes |

## 3. PTP

Via CLI on SW1, SW2, SW3, SW4, SW5.

No switch implements a PTP or gPTP YANG module — the module list is unchanged between AN001 v1.2 and v1.3 — so PTP is never readable over NETCONF on this hardware. Where it appears below it came from the CLI collector and is projected into three YANG models side by side. See REPORT.md §4.

| Switch | Profile | Offset from master | Steps removed | Servo | as-capable ports | Lock verdict |
|---|---|---|---|---|---|---|
| SW1 | gPTP (802.1AS) | +0.000 ns | 0 | FREERUN | 2/8 | **grandmaster** |
| SW2 | gPTP (802.1AS) | -1.028 ns | 1 | PHASE_LOCKED | 3/8 | **locked** |
| SW3 | gPTP (802.1AS) | +0.909 ns | 2 | PHASE_LOCKED | 3/8 | **locked** |
| SW4 | gPTP (802.1AS) | -1.884 ns | 3 | PHASE_LOCKED | 4/8 | **locked** |
| SW5 | gPTP (802.1AS) | -0.173 ns | 4 | PHASE_LOCKED | 3/8 | **locked** |

All 5 switches name the same grandmaster (`00:80:82:ff:fe:b9:65:33`), so they are one time domain, and steps removed gives each switch's depth below it.

**SW1 is the grandmaster**, so its zero offset and free-running servo are definitional rather than measured, and it gets its own verdict rather than `locked`. Nothing inside the network can verify a grandmaster's time base. This one advertises clock class 248 and time source 0xA0, which is not traceable to an external reference — the whole network is disciplined to this switch's local oscillator. That is fine for relative measurements between ports of this network and meaningless as an absolute time base.

Every verdict is advisory and rests on a single sample. A measurement campaign should re-check before and after each point, as `i226-adaptation` does with `pmc` on the end stations. `as-capable` is read per port from the `802.1AS port status` table of `show ptp 0 port-state`; a switch whose synchronising port reports it false is reported `inconsistent`, not `locked`.

**Projected into three YANG models.** The same readings appear under each model's own node names in `capabilities.json` at `ptp.models`, so a consumer can use whichever matches its data model without this tool having chosen for it:

| Model | Reference | Path | Nodes the CLI cannot fill |
|---|---|---|---|
| `ietf-ptp` | RFC 8575 (models IEEE 1588-2008) | `/ptp/instance-list` | 3 |
| `ieee1588-ptp-tt` | IEEE Std 1588-2019 | `/ptp/instances/instance` | 5 |
| `ieee802-dot1as-gptp` | IEEE Std 802.1AS-2020 | `augments /ptp-tt:ptp/ptp-tt:instances/ptp-tt:instance` | 6 |

`ieee802-dot1as-gptp` defines no top-level containers — it augments the `ieee1588-ptp-tt` tree — so its projection holds only the gPTP-specific additions and the base datasets live in the 1588 projection. Note also that IEEE 1588-2019 renamed the roles: `offset-from-master` is `offset-from-time-transmitter` there, and the port states `master`/`slave` are `time-transmitter`/`time-receiver`. Each projection lists what it could not fill and which values were inferred rather than read.

## 4. Qbv (802.1Qbv time-aware shaper) capability envelope

- Maximum gate-control-list entries per port: **256**
- Maximum cycle time: **-**
- Maximum single interval: **-**
- Traffic classes: **8**

These are the smallest limits found across every reachable bridge port. A schedule that fits inside them is installable network-wide.

## 5. Ingress classification — can a talker reach a gated class?

| Switch | Ports trusting the VLAN tag | Ports with an identity PCP map |
|---|---|---|
| SW1 | 0/8 | 0/8 |
| SW2 | 0/8 | 0/8 |
| SW3 | 0/8 | 0/8 |
| SW4 | 0/8 | 0/8 |
| SW5 | 0/8 | 0/8 |

> **No port trusts the incoming VLAN tag.** Every frame is classified to the port's default traffic class whatever PCP the talker sets, so a gate-control list that opens classes 1–7 opens them onto empty queues. This is configuration, not a missing capability — the hardware supports it — but it has to be changed before any schedule means anything. See the `ingress-classification` entry in §7.

> 40 of 40 ports regenerate priority by something other than the identity map, so a stream's PCP is not its gate index. The per-port map is in `capabilities.json` under `interfaces[].qos.values.priority_regeneration`, and the Qbu per-priority record already applies it.

## 6. Per-port state

### SW1 (192.168.1.10)

- Hostname: `KSwitchTSN-1`, location: `-`
- Bridge `bridge0` address `00:80:82:b9:65:33`, type `-`, 8 ports, up ? s
- VLANs: 1, 2

| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | Cycle | GCL | Qbu | Preemptable |
|---|---|---|---|---|---|---|---|---|---|---|
| `2.5G 1/1` | 7 | - | - | down | yes | no | - | 0 | no | - |
| `2.5G 1/2` | 8 | - | - | down | yes | no | - | 0 | no | - |
| `Gi 1/1` | 1 | 1000 | 2 | up | yes | no | - | 0 | yes | - |
| `Gi 1/2` | 2 | - | 2 | down | yes | no | - | 0 | yes | - |
| `Gi 1/3` | 3 | - | 2 | down | yes | no | - | 0 | yes | - |
| `Gi 1/4` | 4 | - | 2 | down | yes | no | - | 0 | yes | - |
| `Gi 1/5` | 5 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/6` | 6 | - | - | down | yes | no | - | 0 | yes | - |

**Notes** (inert — gate disabled on those ports):

- Read over the ISTAX CLI, not NETCONF. Values are parsed from human-readable command output; field-for-field they match the NETCONF shape, but they carry no schema validation and the output format is not versioned.
- `netconf server` IS present in this switch's running-config, yet the NETCONF server did not answer. That is a firmware fault rather than a missing configuration — worth quoting to the manufacturer.

### SW2 (192.168.1.11)

- Hostname: `KSwitchTSN-2`, location: `-`
- Bridge `bridge0` address `00:80:82:bd:25:7c`, type `-`, 8 ports, up ? s
- VLANs: 1, 2

| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | Cycle | GCL | Qbu | Preemptable |
|---|---|---|---|---|---|---|---|---|---|---|
| `2.5G 1/1` | 7 | - | - | down | yes | no | - | 0 | no | - |
| `2.5G 1/2` | 8 | - | - | down | yes | no | - | 0 | no | - |
| `Gi 1/1` | 1 | - | 2 | down | yes | no | - | 0 | yes | - |
| `Gi 1/2` | 2 | 1000 | 2 | up | yes | no | - | 0 | yes | - |
| `Gi 1/3` | 3 | - | 2 | down | yes | no | - | 0 | yes | - |
| `Gi 1/4` | 4 | 1000 | 2 | up | yes | no | - | 0 | yes | - |
| `Gi 1/5` | 5 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/6` | 6 | 1000 | - | up | yes | no | - | 0 | yes | - |

**Notes** (inert — gate disabled on those ports):

- Read over the ISTAX CLI, not NETCONF. Values are parsed from human-readable command output; field-for-field they match the NETCONF shape, but they carry no schema validation and the output format is not versioned.
- `netconf server` IS present in this switch's running-config, yet the NETCONF server did not answer. That is a firmware fault rather than a missing configuration — worth quoting to the manufacturer.

### SW3 (192.168.1.12)

- Hostname: `KSwitchTSN-3`, location: `-`
- Bridge `bridge0` address `00:80:82:bd:25:a2`, type `-`, 8 ports, up ? s
- VLANs: 1, 2

| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | Cycle | GCL | Qbu | Preemptable |
|---|---|---|---|---|---|---|---|---|---|---|
| `2.5G 1/1` | 7 | - | - | down | yes | no | - | 0 | no | - |
| `2.5G 1/2` | 8 | - | - | down | yes | no | - | 0 | no | - |
| `Gi 1/1` | 1 | - | - | down | yes | no | - | 0 | yes | - |
| `Gi 1/2` | 2 | - | - | down | yes | no | - | 0 | yes | - |
| `Gi 1/3` | 3 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/4` | 4 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/5` | 5 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/6` | 6 | 1000 | - | up | yes | no | - | 0 | yes | - |

**Notes** (inert — gate disabled on those ports):

- Read over the ISTAX CLI, not NETCONF. Values are parsed from human-readable command output; field-for-field they match the NETCONF shape, but they carry no schema validation and the output format is not versioned.
- `netconf server` IS present in this switch's running-config, yet the NETCONF server did not answer. That is a firmware fault rather than a missing configuration — worth quoting to the manufacturer.

### SW4 (192.168.1.13)

- Hostname: `KSwitchTSN-4`, location: `-`
- Bridge `bridge0` address `00:80:82:bd:25:80`, type `-`, 8 ports, up ? s
- VLANs: 1, 2

| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | Cycle | GCL | Qbu | Preemptable |
|---|---|---|---|---|---|---|---|---|---|---|
| `2.5G 1/1` | 7 | - | - | down | yes | no | - | 0 | no | - |
| `2.5G 1/2` | 8 | - | - | down | yes | no | - | 0 | no | - |
| `Gi 1/1` | 1 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/2` | 2 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/3` | 3 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/4` | 4 | 1000 | - | up | yes | no | - | 0 | yes | priority0, priority1, priority2, priority3, priority4, priority5 |
| `Gi 1/5` | 5 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/6` | 6 | 1000 | - | up | yes | no | - | 0 | yes | - |

**Notes** (inert — gate disabled on those ports):

- Read over the ISTAX CLI, not NETCONF. Values are parsed from human-readable command output; field-for-field they match the NETCONF shape, but they carry no schema validation and the output format is not versioned.
- `netconf server` IS present in this switch's running-config, yet the NETCONF server did not answer. That is a firmware fault rather than a missing configuration — worth quoting to the manufacturer.

### SW5 (192.168.1.14)

- Hostname: `KSwitchTSN-5`, location: `-`
- Bridge `bridge0` address `00:80:82:bd:25:72`, type `-`, 8 ports, up ? s
- VLANs: 1, 2

| Port | if-index | Speed | PVID | Oper | Qbv | Gate on | Cycle | GCL | Qbu | Preemptable |
|---|---|---|---|---|---|---|---|---|---|---|
| `2.5G 1/1` | 7 | - | - | down | yes | no | - | 0 | no | - |
| `2.5G 1/2` | 8 | - | - | down | yes | no | - | 0 | no | - |
| `Gi 1/1` | 1 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/2` | 2 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/3` | 3 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/4` | 4 | 1000 | - | up | yes | no | - | 0 | yes | - |
| `Gi 1/5` | 5 | - | - | down | yes | no | - | 0 | yes | - |
| `Gi 1/6` | 6 | 1000 | - | up | yes | no | - | 0 | yes | - |

**Notes** (inert — gate disabled on those ports):

- Read over the ISTAX CLI, not NETCONF. Values are parsed from human-readable command output; field-for-field they match the NETCONF shape, but they carry no schema validation and the output format is not versioned.
- `netconf server` IS present in this switch's running-config, yet the NETCONF server did not answer. That is a firmware fault rather than a missing configuration — worth quoting to the manufacturer.

## 7. Gaps for a Centralized Network Configuration entity

### `ptp` — degraded-read-only

PTP is readable, but only over the ISTAX CLI: this hardware implements no PTP or gPTP YANG module in either AN001 v1.2 or v1.3. A CNC therefore gets the time base through a screen-scraped, unversioned interface, and cannot configure PTP through the same channel it configures Qbv.

*Workaround:* Treat the CLI PTP read as a verification gate before installing or trusting a schedule, and re-check it around each measurement point rather than once per campaign.

### `transport` — degraded

These bridges were read over the ISTAX CLI because their NETCONF server did not answer. CLI reads carry no schema validation, the output format is not versioned between firmware releases, and NETCONF's transactional machinery (candidate datastore, validate, confirmed-commit, rollback-on-error) is unavailable — which matters most for the write path a CNC will need.

*Workaround:* Restore the NETCONF server. `netconf server` being present in the running-config while nothing listens on port 830 is a firmware regression to raise with the manufacturer, not a configuration error.

### `qcc` — expected

No 802.1Qcc UNI/stream model. The switches are configured directly by writing Qbv and VLAN state; there is no stream abstraction on the bridge side. This is the normal case for a fully-centralized CNC and is not an obstacle.

*Workaround:* The CNC keeps the stream model itself and renders it into per-port Qbv gate-control lists.

### `ingress-classification` — blocking-for-scheduling

40 bridge port(s) have `qos trust tag disabled`, so the PCP a talker sets is ignored and every frame is classified to the port's default traffic class. A Qbv gate-control list that opens classes 1-7 would gate empty queues. This is a configuration state, not a missing capability -- the hardware can do it.

*Workaround:* Enable tag trust on the ports carrying scheduled traffic before installing any schedule, and re-run discovery to confirm.

### `priority-regeneration` — operational

40 port(s) map PCP to traffic class by something other than the identity, so a stream's PCP is not its gate index. The map is read per port and recorded under qos/priority_regeneration; a CNC must apply it when turning a stream priority into a gate.

*Workaround:* Use the per-port map rather than assuming PCP N means traffic class N.

### `datastore-coherence` — operational

Per Kontron AN001 v1.2 (Data Stores): the sysrepo plugin reads running configuration from the switch management software only when the plugin starts. Changes made in the CLI or web UI after that are not visible over NETCONF, and changes written over NETCONF are not persistent until saved from the CLI or web UI.

*Workaround:* Treat NETCONF as the single writer during a campaign. Re-read after any out-of-band change, and save explicitly if the configuration must survive a reboot.

