# Implementation Report — TSN-FlexTest adapted to Intel I226 / Kontron KSwitch D10

**Subject:** porting the TSN-FlexTest measurement testbed
(`github.com/ivankl92/tsn-testbed`) to two UP Squared Pro 7000 Edge PCs with
Intel I226-IT NICs and two Kontron KSwitch D10 switches.

**Status:** implementation complete, and the **measurement path has now run end
to end on the real testbed** (2026-09-09, run `20260909-160453`): a `--quick`
smoke campaign of 4 points × 100 000 frames, every frame timestamped in
hardware on both ends, gPTP held within ±8 ns throughout. That validates the
mechanism. It is **not** a result: no full campaign has been run, and no latency
figures have been analysed or interpreted yet. Section 8 states precisely what
has and has not been exercised. Read it before trusting anything here.

---

## 1. Summary

The upstream testbed cannot run on this hardware. Two blockers are structural,
not incidental:

1. Its one-way latency measurement depends on a **patched Intel `igb` kernel
   driver** that makes the NIC write its own hardware TX timestamp into the
   frame payload. The I226 uses the `igc` driver; the patch does not apply.
2. It assumes **five nodes plus a controller**. We have two PCs with two ports
   each.

The adaptation replaces the measurement core with the standard Linux
`SO_TIMESTAMPING` API — no kernel patch, no custom kernel, no reboot — and
remaps the experiment onto the available hardware so that the shared
inter-switch link becomes the congestion point under study. **No file, script or
patch from the upstream repository is used, imported, or applied.** Upstream was
read and reimplemented; §7 documents the differences and §3.2 the single place
where code was genuinely derived.

---

## 2. Starting point — what upstream actually does

### 2.1 The measurement mechanism, dissected

This is the part that matters, and it is not documented in the upstream README;
it has to be reconstructed from the patch and the parser together.

`patches/linux-5.11.y-rt.patch` modifies four files in `drivers/net/ethernet/intel/igb/`:

```c
/* e1000_82575.h — a bit that upstream igb never sets */
#define E1000_ADVTXD_MAC_TSTAMP_ONESTEP 0x00040000

/* igb_main.c — flag EVERY frame whose EtherType is IPv4 for one-step */
uint16_t ethertype = ((uint16_t)skb->data[13+4] << 8) + skb->data[12+4];
if ((ethertype == 0x0008)) tx_flags |= IGB_TX_FLAGS_TSTAMP_ONESTEP;
ethertype = ((uint16_t)skb->data[13] << 8) + skb->data[12];
if ((ethertype == 0x0008)) tx_flags |= IGB_TX_FLAGS_TSTAMP_ONESTEP;

/* igb_ptp.c — tell the MAC where in the frame to write the timestamp */
tsync_tx_ctl |= 48 << 8;   // byte offset 48
```

The one-step PTP engine exists so the MAC can stamp the origin time into a
**Sync** message as it leaves the wire. The patch repurposes it: any IPv4 frame
(checked at both the untagged and the VLAN-tagged EtherType position) gets the
NIC's egress timestamp written **into its own payload at byte offset 48**.

`src/evaluation/parsePcap.py` reads it back out:

```python
src     = self.mapping[int(hexstream[23], 16)]      # last nibble of src MAC
frame_id= int(hexstream[44:48], 16)                 # bytes 22–23
ts_nsec = Decimal(int(hexstream[108:116], 16))      # bytes 54–57
ts_sec  = Decimal(int(hexstream[100:108], 16))      # bytes 50–53
ts_recv = ts                                        # pcap hardware RX timestamp
latency = ts_recv * 1e9 - ts_send * 1e9
```

So the full upstream chain is:

```
tcpreplay --sockprio → patched igb writes TX timestamp into payload@48
        → switch → tcpdump -j adapter_unsynced --time-stamp-precision=nano
        → pcap → parsePcap.py subtracts embedded TX from pcap RX
```

Two consequences worth stating plainly, because they were easy to get wrong:

- The **`tcpreplay` patch does not timestamp anything.** It adds one option,
  `--sockprio`, which calls `setsockopt(SO_PRIORITY)`. Nothing more.
- The **`linuxptp` patch does not affect measurement.** It sets `prefix=/usr`
  and forces socket priority 1 on PTP frames so they land in the high-priority
  hardware queue.

### 2.2 Why it does not port

| | Upstream | Here |
|---|---|---|
| NIC | Intel I210/I211 | Intel I226-IT |
| Driver | `igb` | `igc` |
| Kernel | 5.11-rt, patched | stock Ubuntu |

The patch touches `igb`-private headers, flags and register definitions. `igc`
is a separate driver for a different MAC generation; I225/I226 one-step
behaviour and its `TSYNCTXCTL` semantics differ from the 82575-family layout the
patch assumes. Applying it is not a matter of adjusting paths.

### 2.3 The node-count mismatch

`src/measurements/scripts/begin.sh` and `run.sh` address **node0** (sink,
`tcpdump`), **node1–node3** (talkers, `tcpreplay`), **node4** (background load,
MoonGen/DPDK), all driven over ssh from a sixth machine. Three streams are
replayed simultaneously and separated at the sink by the last byte of the source
MAC. We have two PCs, two ports each.

---

## 3. Design decisions

Each decision below lists what was considered and why the chosen option won.

### 3.1 Timestamping mechanism — the central choice

Three options were on the table:

| Option | Accuracy | Risk | Verdict |
|---|---|---|---|
| **A. Port the igb hack to igc** | Highest — identical to the paper | Exploratory kernel work; I226 one-step semantics differ; module rebuild on every kernel update | Rejected |
| **B. Software TX timestamp in payload** | Poor — adds host TX-stack jitter (µs, worse under load) to every sample | Low | Rejected: the jitter it adds is the same order as the effect being measured |
| **C. `SO_TIMESTAMPING` hardware TX + RX** ✅ | NIC-to-NIC, nanosecond | I226 has few TX-timestamp registers → possible yield limit at rate | **Chosen** |

Option C works like this. The sender sets

```c
SOF_TIMESTAMPING_TX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE
```

on the `AF_PACKET` socket. After each frame is on the wire the kernel queues a
message on the socket's **error queue** containing `SCM_TIMESTAMPING` — whose
`ts[2]` is the raw hardware (PHC) timestamp — **plus a copy of the original
frame**. The sender drains that queue after every send and recovers its own
sequence number from the returned frame copy. The receiver sets
`SOF_TIMESTAMPING_RX_HARDWARE` and reads `ts[2]` from the control message on
arrival.

**Why the result is a true one-way latency:** both timestamps are raw PHC time,
and both PHCs are disciplined by gPTP to the same grandmaster (the switches).
Subtracting them is therefore valid **without** `phc2sys` and without any
system-clock involvement. Residual grandmaster offset appears as a constant bias
in the absolute value, not as jitter — so distribution shape, jitter and the
No-QoS-vs-802.1p comparison are unaffected by it.

Rejecting option B deserves emphasis: measuring TSN with software TX timestamps
means the instrument's noise floor sits in the same range as the phenomenon.
That is not a measurement, it is a plausible-looking number.

### 3.2 Host QoS — the one derived piece of code

Upstream's `begin.sh` mqprio recipe was kept, with one deliberate change:

```bash
# upstream
tc qdisc add dev enp1s0 parent root handle 6666 mqprio num_tc 2 \
   map 1 0 1 1 1 1 1 1 1 1 1 1 1 1 1 1  queues 1@0 1@1 hw 0
# ours (qos_config.sh)
tc qdisc add dev "$IF" parent root handle 6666 mqprio num_tc 2 \
   map 1 1 1 0 1 1 1 1 1 1 1 1 1 1 1 1  queues 1@0 1@1 hw 0
```

`num_tc`, `queues`, `hw 0` and even the handle `6666` are theirs. The map
differs because the semantics of socket priority differ between the two
testbeds: upstream's **patched** linuxptp forces PTP frames to socket priority
1, so they map priority 1 → tc 0. Our linuxptp is unpatched and sets no socket
priority, so we map priority **3** → tc 0, matching `STREAM_SOCKPRIO=3`.
`ethtool -L <if> combined 4` is added for the I226's four queues.

**Scope note, stated honestly:** at 1000 pps on a 1 Gbit/s port the host's own
egress queues are never the bottleneck, so this qdisc changes essentially
nothing in our results. It is applied for fidelity to the original method. The
differentiation that actually matters happens in the switch, driven by the PCP
bits in the VLAN tag.

### 3.3 Traffic generation — no MoonGen

Upstream generates background load with **MoonGen** (DPDK: hugepages, NIC bound
away from the kernel, a dedicated node). Replaced with `iperf3` UDP:

- No DPDK, no hugepages, no driver binding — the background NIC stays a normal
  kernel interface, which matters because it is on the same PC as the measured
  stream.
- Rate is expressed as a **percentage of the detected link rate**
  (`/sys/class/net/<if>/speed`), so `105 %` deliberately oversubscribes.
- `BG_STREAMS=4` parallel streams because a single `iperf3` UDP stream is often
  CPU-bound below line rate. `-b` is per stream in iperf3, so the script divides.
- Achieved (as opposed to requested) rate is recorded in `iperf3.json` per point,
  because requesting 950 Mbit/s and getting 700 would silently invalidate a
  comparison.

Accepted loss: MoonGen produces harder, more precisely paced load. `iperf3` load
is burstier. Since load is a controlled variable compared across QoS modes and
not itself a reported result, this is acceptable — but it must be reported.

### 3.4 Topology mapping

```
PC1 enp1s0 ──► KSw1 p1 ┐                    ┌ KSw2 p1 ──► PC2 enp1s0
                       ├── inter-switch ────┤
PC1 enp2s0 ──► KSw1 p2 ┘   link (shared)    └ KSw2 p2 ──► PC2 enp2s0
```

PC1 `enp1s0` = the single talker; PC2 `enp1s0` = the sink; the `enp2s0` pair
carries background load. **The design hinges on the inter-switch link**: both
port pairs funnel through it, so background traffic between the `enp2s0` ports
contends for the same egress queue the measured stream must cross. Without that
shared link there would be no congestion and nothing for 802.1p to arbitrate.

### 3.5 Frame format

```
 0      6      12    14      18                    22        size
 +------+------+-----+-------+---------+-----------+---------+
 | dst  | src  |8100 | 88B5  | "TSNF"  | seq (u32) | sw_tx   | filler
 |  6B  |  6B  |TCI 2|  2B   |  4B     |    4B     |  8B     |
 +------+------+-----+-------+---------+-----------+---------+
        802.1Q tag: PCP<<13 | VID
```

Decisions embedded here:

- **EtherType `0x88B5`** (IEEE 802 local experimental 1) rather than IPv4. The
  frames are measurement traffic, not IP; using `0x0800` would invite the host
  stack and any middlebox to treat them as routable packets.
- **Priority-tagged, VID 0.** The standard way to carry a PCP without asserting
  VLAN membership. It avoids requiring VLAN configuration on the KSwitch ports
  while still giving the switch a PCP to schedule on. If the ports reject tagged
  frames, the preflight (§5.6) detects it and stops rather than silently
  degrading.
- **Baseline is PCP 0, not untagged.** Both configurations therefore send
  byte-identical frames; only three bits differ. Comparing tagged against
  untagged would confound prioritisation with a 4-byte length change.
- **A magic word plus sequence number in the payload**, and a *scan* for the
  magic over bytes 12–24 rather than a fixed offset, because the NIC may strip
  the VLAN tag on receive into skb metadata — so the payload's position is not
  known a priori.
- `sw_tx_ns` (CLOCK_TAI at `send()`) is carried as a **diagnostic only**. It is
  never used to compute latency; it exists so a hardware-timestamp failure can
  be distinguished from a network failure after the fact.

### 3.6 gPTP profile

`/etc/linuxptp/gPTP.cfg` is written from scratch as an 802.1AS profile:
`network_transport L2`, `delay_mechanism P2P`, `transportSpecific 0x1`,
`ptp_dst_mac 01:80:C2:00:00:0E`, `logSyncInterval -3`, `gmCapable 0`,
`priority1/2 = 255`.

`gmCapable 0` guarantees the PCs lose the BMCA and never become grandmaster: it
forces `priority1` and `clockClass` to 255 internally. If a PC nonetheless
reports `portState MASTER`, that is unambiguous evidence it cannot hear the
network — a useful diagnostic rather than a silent misconfiguration.

**`gmCapable 0` must appear alone.** The first version of this profile also set
`clientOnly 1`, on the reasoning that saying it twice could not hurt. It can:
`clientOnly`/`slaveOnly` are the IEEE 1588 default-profile mechanism, and ptp4l
refuses the combination at startup —

```
Cannot mix 1588 clientOnly with 802.1AS !gmCapable
failed to create a clock
```

— exiting 255 before it ever opens the interface. With `Restart=always` on the
systemd unit, that becomes a crash loop, and the symptom the operator sees is
not a config error but "no gPTP lock within 90 s", because `pmc` has no running
`ptp4l` to answer it. The setup script now validates the file with
`ptp4l -f … -h` and exits 5 with ptp4l's own message rather than installing a
unit that cannot start.

The grandmaster need not be one of the KSwitches. They may relay time from a GM
elsewhere in the network; this profile only requires that the PC is a client of
whatever GM the domain has.

### 3.7 System-level decisions

- **`chrony` and `systemd-timesyncd` are disabled.** They contend with
  `phc2sys` for `CLOCK_REALTIME`. Left running, they produce step corrections
  that look like measurement artefacts.
- **`phc2sys` is run but is not load-bearing.** Latency comes from PHC−PHC
  (§3.1). `phc2sys` exists so logs and `CLOCK_TAI` pacing are sane.
- **Strict ARP settings** (`arp_filter=1`, `arp_announce=2`, `arp_ignore=1`) are
  applied per interface. Both port pairs live in `192.168.1.0/24`, so without
  these Linux would happily answer ARP for `.72` out of `enp1s0` and route
  background load onto the measurement port — quietly invalidating every result.
  This is the most easily missed failure in the whole setup.

### 3.8 Data format — CSV, not pcap

Upstream captures a pcap at the sink and derives everything from it. We write
two CSVs per measurement point and join them offline on the sequence number:

```
tx.csv:  seq,tx_hw_ns,sw_tx_ns,ts_src
rx.csv:  seq,rx_hw_ns,sw_tx_ns,ts_src
```

`ts_src` is `hw` or `sw` per row — a permanent, per-sample record of whether the
value came from the NIC or from a software fallback. Loss is then simply the
sequence numbers present in `tx.csv` and absent from `rx.csv`, which is exact
and needs no heuristics.

---

## 4. Patching — precise account

**Patches applied: none. Zero.** No kernel is rebuilt, no module is replaced, no
userspace package is patched, no reboot is required.

| Upstream patch | Lines | What it does | Disposition |
|---|---|---|---|
| `linux-5.11.y-rt.patch` | 65 | igb one-step timestamp into payload@48 | **Not applied** — `igb`-only, superseded by `SO_TIMESTAMPING` |
| `tcpreplay_4.3.4.patch` | 149 | adds `--sockprio` → `setsockopt(SO_PRIORITY)` | **Not applied** — `tsn_tx` calls `setsockopt(SO_PRIORITY)` natively |
| `linuxptp_OpenIL-v1.8.1-202009.patch` | 26 | `prefix=/usr`; socket priority 1 on PTP frames | **Not applied** — distro `linuxptp` used unmodified |

Consequences of not applying the linuxptp patch, since it is the least obvious:
our PTP frames carry no socket priority, so they are not steered into hardware
queue 0 by mqprio. On the measurement port this is harmless — the port carries
only the measurement stream and PTP, and is far from saturated. It would matter
if the measurement port itself were congested, which this design deliberately
avoids by putting the congestion in the switch.

**If option A (§3.1) is ever revisited**, the igc equivalent would need: the
one-step enable bit and TX-offset field in `IGC_TSYNCTXCTL` located in the
I225/I226 datasheet, the corresponding `igc_ptp.c` / `igc_main.c` changes, and a
DKMS module so it survives kernel updates. Treat it as research, not a port.

---

## 5. Implementation walkthrough

### 5.1 `tools/tsn_common.h`

Shared helpers: `hwtstamp_enable()` (the `SIOCSHWTSTAMP` ioctl, requesting
`HWTSTAMP_TX_ON` + `HWTSTAMP_FILTER_ALL`, and reporting what the driver actually
applied), `find_payload()` (the magic scan of §3.5), portable `hton64`/`ntoh64`,
MAC parsing, interface lookup.

`HWTSTAMP_FILTER_ALL` is required because the PTP-only filters would not
timestamp our `0x88B5` frames. It is a **superset** of the PTP filter, so ptp4l
keeps working — **but only if ptp4l starts first**, since ptp4l narrows the
filter when it starts and our tools widen it again afterwards. This ordering
constraint is load-bearing and is documented in the source, the runbook and the
mission file.

### 5.2 `tools/tsn_tx.c`

Paced talker. `clock_nanosleep(CLOCK_TAI, TIMER_ABSTIME)` against an absolute
schedule (so pacing error does not accumulate), starting on a whole second for
run-to-run comparability. After each `send()` it drains the error queue
non-blockingly, then polls for up to `-w` seconds at the end to collect trailing
timestamps. Reports TX-timestamp yield and **warns below 90 %**, which is the
early-warning signal for the I226 register-saturation risk.

Options: `-i -d -n -r -s -p -v -q -o -w -S`.

### 5.3 `tools/tsn_rx.c`

Listener on `AF_PACKET`/`ETH_P_ALL`, 16 MB receive buffer, userspace filter on
the magic word. Counts frames seen, frames matched, and frames matched **without**
a hardware timestamp — the third counter is what catches a filter that silently
reverted to PTP-only. Exits non-zero if nothing matched.

### 5.4 `scripts/setup_node.sh` — seven phases, idempotent

1. **Packages** — `build-essential linuxptp ethtool iperf3 tcpdump iproute2
   python3{,-numpy,-pandas,-matplotlib} git`; disables `chrony` and
   `systemd-timesyncd`.
2. **Interface verification** — existence, driver is `igc`, link state, speed;
   prints `ethtool -T`; **hard-fails** if `hardware-transmit` is absent and warns
   if `HWTSTAMP_FILTER_ALL` is not advertised.
3. **IP configuration** — adds the addresses if missing; applies the strict ARP
   settings of §3.7.
4. **gPTP** — writes `/etc/linuxptp/gPTP.cfg`, `ptp4l@.service` and
   `phc2sys@.service` (systemd **template** units, so the interface is the
   instance name), enables and starts them.
5. **Build** — compiles `tsn_tx`/`tsn_rx` into `/opt/tsn-flextest/`, installs
   `qos_config.sh` beside them.
6. **Background sink** — `iperf3-bg.service` on the listener.
7. **Lock gate** — polls `pmc` for up to 90 s and requires `portState`
   SLAVE/CLIENT **and** `|offsetFromMaster| < 1000 ns`; exits 4 with diagnostics
   otherwise. Setup does not "succeed" merely because packages installed.

Files this writes outside the pack: `/etc/linuxptp/gPTP.cfg`,
`/etc/systemd/system/{ptp4l@,phc2sys@,iperf3-bg}.service`, `/opt/tsn-flextest/*`.

### 5.5 `scripts/bootstrap_ssh.sh`

Generates a key for **root** on PC1 (the campaign runs under `sudo`, so root's
key is the one that matters), installs it on PC2, and writes
`/etc/sudoers.d/99-tsn-flextest` there for non-interactive `sudo`. The password
is read interactively or from `$SSHPASS` and is never written to disk or logs.

### 5.6 `scripts/run_measurement.sh`

Per measurement point:

1. `check_ptp()` on both nodes — state and offset; one 60 s retry, then the
   point is **skipped and marked**, never silently measured out of sync.
2. `qos_config.sh` applied on both nodes.
3. `tsn_rx` started on PC2 over ssh (`+12 s` margin), 3 s settle.
4. `iperf3` background load started, 3 s settle.
5. `tsn_tx` sends `STREAM_RATE × STREAM_DURATION` frames.
6. `rx.csv` fetched by `scp`; `check_ptp()` again; `meta.json` written with PTP
   state **before and after**, requested and achieved load, and counts.
7. 5 s cool-down (mirrors upstream's inter-run pause).

**Preflight**, before any of that: 200 priority-tagged frames PC1→PC2. If fewer
than half arrive it retries untagged; if untagged then works, it **aborts with a
specific diagnosis** — the KSwitch ports are dropping tagged traffic — rather
than quietly falling back, because untagged frames carry no PCP and there would
be no 802.1p experiment left. It also inspects `ts_src` and warns on any
software-timestamp fallback.

### 5.7 `analysis/analyze_plot.py`

Joins on `seq`, computes `latency_ns = rx_hw_ns − tx_hw_ns`, trims the first and
last 5 % of each run (link training, iperf3 ramp-up/tear-down), and emits
`summary.csv`, `summary.md` and five figures: latency-vs-load (median + p99
band), CDF at the heaviest load, per-load box plot, frame loss, and a per-frame
time series. Colours are two slots from a CVD-validated categorical palette;
every figure carries both a legend and direct labels so series identity is never
colour-alone.

### 5.8 `analysis/make_fixture.py` and the synthetic-data interlock

`make_fixture.py` fabricates measurement-shaped data so the analysis code can be
exercised without hardware. **Nothing it produces is a measurement.** Its
queueing model is invented and was tuned to show the outcome one *hopes* to see,
which is precisely the question the real campaign answers — so a fixture
mistaken for a result would be self-confirming.

The interlock: `make_fixture.py` writes a `SYNTHETIC` marker into its output
directory; `analyze_plot.py` detects it and then watermarks every figure
("SYNTHETIC FIXTURE / NOT MEASURED DATA"), retitles `summary.md`, and sets
`data_source=SYNTHETIC_FIXTURE` in `summary.csv`. `run_measurement.sh` never
writes the marker. Verified in both directions: marked runs are always stamped,
unmarked runs never are.

---

## 6. Differences from the upstream project

| Aspect | Upstream | This adaptation |
|---|---|---|
| TX timestamp | patched igb writes into payload@48 | `SO_TIMESTAMPING` TX hardware via error queue |
| RX timestamp | `tcpdump -j adapter_unsynced` | `SO_TIMESTAMPING` RX hardware in `tsn_rx` |
| Kernel | 5.11-rt, patched | stock Ubuntu, unpatched |
| Traffic source | patched `tcpreplay` replaying real pcaps | purpose-built paced generator, synthetic frames |
| Background load | MoonGen / DPDK on a dedicated node | `iperf3` UDP, 4 streams, % of link rate |
| Nodes | 5 + controller | 2 (talker+load / sink+load) |
| Streams | 3 concurrent, separated by src MAC | 1 |
| Data format | pcap | two CSVs joined on sequence number |
| Loss detection | inferred at parse time | exact, by missing sequence numbers |
| Compared variable | frame size × background load × stream set | **QoS mode** × background load |
| Baseline | `No-QoS` (untagged) | PCP 0, byte-identical to the 802.1p case |
| EtherType | IPv4 (required by the patch) | `0x88B5` experimental |
| PTP config | patched linuxptp, `ptp3.conf` | stock linuxptp, `gPTP.cfg` written fresh |
| PTP units | fixed-path `.service` files | systemd template units + `Restart=always` |
| Sync validation | post-hoc journald grep for announce timeouts | live `pmc` gate before *and* after each point, run skipped if lost |
| Statistics | `min/max/median/mean/std/p0.01…p0.99` | adds p99.9 and IPDV; **different schema** |
| Provenance marking | none | `SYNTHETIC` interlock + `data_source` column |

---

## 7. Code provenance

No upstream file is copied. A whitespace-normalised line-level comparison of the
whole pack against upstream's shell, Python, systemd and config files yields 16
shared lines out of ~1300, of which 14 are boilerplate (`[Unit]`, `fi`, `done`,
`continue`, `return`, `else:`, `from pathlib import Path`). The two meaningful
ones are `num_tc 2 \` and `queues 1@0 1@1 \` from the mqprio recipe of §3.2.

Ideas taken (not code): the config-file/campaign-driver/analysis split; the
sweep structure; sink-then-load-then-talker ordering; wrapping remote commands
in `timeout`; using `pmc` with `portState SLAVE` as the sync gate; and the
**±1000 ns offset threshold**, which is upstream's number from `end.sh`.

The method originates in the TSN-FlexTest papers (NetSoft 2022; IEEE TNSM 21(2)
2024, doi:10.1109/TNSM.2023.3327108). Those citations belong in anything
published from this work even though no upstream line ships.

---

## 8. Verification status

**Exercised.** Both C tools compile clean with `-Wall -Wextra`. A `veth`
loopback run passed 600/600 frames end to end, confirming frame construction
with the 802.1Q tag, payload layout, sequence recovery from the error-queue
frame copy, pacing and CSV format. `analyze_plot.py` ran against the fixture and
rendered all five figures, and the watermark interlock was verified in both
directions. Shell scripts pass `bash -n` and `shellcheck -S error`.

**Exercised on the real testbed** (run `20260909-160453`, `--quick`: 2 QoS modes
× 2 loads × 10 s at 10 000 fps). This supersedes the `veth` caveat above — the
hardware timestamping path itself now runs. Specifically confirmed:

- `SIOCSHWTSTAMP` with `HWTSTAMP_FILTER_ALL` succeeds on `igc`, **while ptp4l is
  running**, and the filter stays wide: `ts_src` is `hw` in every row of every
  `tx.csv` and `rx.csv` across all four points. The ptp4l-narrows /
  `tsn_rx`-widens ordering therefore holds in practice.
- Hardware TX-timestamp yield under real load: 100 000/100 000 at 0 % load in
  both QoS modes; 99 998 and 99 993 out of 100 000 at 95 % load.
- gPTP lock sustained on both PCs before and after every point, offsets between
  −7 and +8 ns, including under 950 Mbit/s of background traffic.
- Every script at runtime: apt, the systemd units, `pmc` parsing, ssh/scp,
  iperf3, and `mqprio` on `igc`.
- The KSwitches forward priority-tagged VID-0 frames (preflight 200/200).

**Still never executed.** A full campaign (`BG_LOADS="0 50 80 95 105"`,
30 s per point). No latency figure from this testbed has been analysed,
plotted or interpreted — `analyze_plot.py` has still only ever run against the
synthetic fixture. Nothing here says whether 802.1p makes a measurable
difference on this hardware; that is what the full campaign is for.

**Silent-failure mode found and closed: a dead background-load generator.** A
campaign run with 802.1Q disabled on the switch returned a median latency of
13.14 µs at 0 %, 50 % *and* 105 % background load — identical to within 0.01 µs,
i.e. the unloaded floor at every point. The obvious reading ("load has no
effect") was wrong. The listener's journal showed, on every accepted
connection:

```
iperf3-bg.service: Main process exited, code=dumped, status=11/SEGV
iperf3-bg.service: Failed with result 'core-dump'.
Scheduled restart job, restart counter is at 14.
```

Ubuntu 24.04 ships **iperf 3.16**, the release that made iperf3 multi-threaded,
and its thread-lifetime bugs kill the server as soon as a UDP test attaches.
Upstream fixed these in 3.18 (#1801, #1760, #1750, PR#1755), 3.19 (#1807) and
3.21 (a socket-close race, and erroneous zero-loss reporting on lossy UDP tests
— which this harness reads back out of iperf3's JSON).

The methodological point generalises beyond iperf3. Every *other* component
here fails loudly: ptp4l goes FAULTY, `tsn_tx` reports a timestamp shortfall,
the preflight counts frames. The load generator was the one part whose failure
left the pipeline producing well-formed, self-consistent, plausible numbers —
and a plausible number is far more dangerous than an error. Three defences were
added rather than one: `fix_iperf3.sh` (builds a fixed release into
`/usr/local`), a version gate in `setup_node.sh` and `run_measurement.sh` that
refuses anything below 3.18 on either node, and a 2-second live UDP probe of the
background path before a campaign starts. `run_measurement.sh` additionally
records `background_mbps_achieved` per point and writes a `LOAD_SHORTFALL`
marker below 80 % of the requested rate, and `analyze_plot.py` flags a latency
series that is flat across loads. **The rule this enforces: an experiment must
verify its stimulus, not only its instrument.**

**Second silent-failure mode, from the same investigation: source-address
selection.** With iperf3 upgraded to 3.21 the server stopped crashing — and the
background load still did not flow. The client failed after 30 seconds with
`unable to read from stream socket: Resource temporarily unavailable`, while the
server logged a healthy `Server listening on 5201 (test #1)`. A capture on the
talker resolved it:

```
enp2s0 Out IP 192.168.1.62.44668 > 192.168.1.72.5201: UDP, length 4
enp2s0 In  IP 192.168.1.71.5201 > 192.168.1.62.44668: UDP, length 4
```

The reply carries `.71`, PC 2's **stream** address. All four measurement
addresses sit in one `192.168.1.0/24` across two interfaces per node, so the
kernel holds two equal-cost routes for that prefix and resolves the tie by
interface index — `ip route get 192.168.1.62` on PC 2 returned `dev enp1s0 src
192.168.1.71`. iperf3's UDP handshake (`iperf_udp_connect()`) waits on a socket
*connected* to `.72` with a 30 s `SO_RCVTIMEO`; a datagram from `.71` is
discarded by the kernel before iperf3 sees it.

Two things make this worth recording rather than just fixing. First, the
existing mitigation was **incomplete in a way that looked complete**: §5.4's
`arp_filter`/`arp_ignore`/`arp_announce` settings were applied, verified, and
correct — but they govern *who answers ARP*, not which source address a
locally-originated packet carries. That is a separate route lookup, and nothing
in the strict-ARP family touches it. Second, TCP is immune, because an accepted
socket's addresses are fixed by the handshake — so the control connection
worked, the server looked healthy, and every symptom pointed at iperf3.

Fixed by a `/32` host route with an explicit `src`, which wins on longest-prefix
match; `run_measurement.sh` installs and verifies it on both nodes at the start
of every campaign, because like the sysctls it is runtime state lost on reboot.
The structural fix, recommended in RUNBOOK §5.6, is to give the background pair
its own subnet so the ambiguity cannot arise.

**Ranked unknowns.** #1, #2 and #3 are now answered on the real hardware. #4 and
#5 remain open — and #4 is the one that determines whether the experiment has
anything to show.

1. ~~**I226 TX-timestamp yield.**~~ **MEASURED — not a limitation on this
   hardware.** `tx_rate_selftest.sh` on the target PC (kernel
   `6.8.1-1058-realtime`, `igc`, 1 Gbit/s, 512 B frames) returned **100 % yield
   at every rate from 200 to 20 000 fps**, with `tx_hwtstamp_skipped` = 0
   throughout. The four TX-timestamp registers in `igc` 6.8 keep up. `STREAM_RATE`
   can be raised to 10 000 fps (100 µs period) with a hardware timestamp on every
   frame. The concern that motivated open items #5 and #9 does not apply below
   20 kfps here; re-measure if the kernel or NIC changes.
2. ~~Whether `igc` accepts `HWTSTAMP_FILTER_ALL`, and whether the
   ptp4l-narrows/`tsn_rx`-widens ordering holds in practice.~~ **ANSWERED —
   both hold.** `ethtool -T` advertises exactly `none` and `all`, and run
   `20260909-160453` confirms the filter is actually applied and stays applied:
   `ts_src` is `hw` in every row of all eight CSVs, with ptp4l running
   throughout. The ordering constraint is real but satisfied by the documented
   start order; nothing silently reverted to PTP-only.
3. ~~Whether the KSwitch ports forward priority-tagged VID-0 frames.~~
   **ANSWERED — they do.** The preflight in the first end-to-end run received
   **200 / 200** priority-tagged frames PC 1 → PC 2, so no VLAN configuration
   is needed on the access ports and the PCP-0-vs-PCP-6 comparison is viable
   as designed.
4. Whether the KSwitch applies **strict priority** on PCP by default. If it does
   not, 802.1p will show no improvement — a finding about the switch, not a bug.
5. `pmc` output-format assumptions across linuxptp versions.

---

## 9. Open features — not implemented

### Tier 1 — needed for parity with the published method

1. **Upstream-schema adapter.** ~40 lines converting `tx.csv`/`rx.csv` into the
   DataFrame schema `statsAnalysis.py` and `createPlot.py` expect
   (`'Latency (ns)'`, `'Inter Frame Gap Sender (ns)'`,
   `'Inter Frame Gap Receiver (ns)'`, `'Frame No.'`, `'Source'`). Every quantity
   is already available — sender IFG is successive differences of `tx_hw_ns`.
   Would make upstream's statistics CSV, its plotting code and the `paper_*.ipynb`
   notebooks work on this data, and make results comparable with the paper and
   the ieee-dataport datasets.
2. ~~**Repetitions and confidence intervals.**~~ **IMPLEMENTED**, and the
   implementation deliberately differs from upstream's, because upstream's
   method would not have caught the problem that forced this.

   Upstream's `confidence-interval.ipynb` computes

   ```python
   dist = NormalDist.from_samples(data)          # data = per-frame latencies
   e = dist.stdev * z / ((len(data) - 1) ** .5)  # z for 99 %
   ```

   — the precision of the mean *within one run*, over individual frames. The
   paper follows the same philosophy: long single runs (30 min for the generic
   stream) with CCDFs over pooled packets, rather than repeated independent
   runs. Its "at most ten times" is a retry-on-error mechanism, not a
   statistical repetition.

   That interval is ~0.02 µs on our data. It is also **blind to the dominant
   source of variation here**: two campaigns with identical settings produced
   medians 11 µs apart, and at 50 % load a spurious 10 µs "802.1p effect"
   appeared in a run where 802.1Q was *disabled on the switch* — where the
   mechanism could not act. Within-run intervals cannot see run-to-run drift,
   and per-frame samples are strongly autocorrelated anyway (queueing arrives
   in bursts), which makes the interval optimistic even on its own terms.

   So `summary.csv` now reports **both**:

   | Column | Meaning |
   |---|---|
   | `mean_ci99_us_upstream` | upstream's formula, exactly, for comparability with the published work |
   | `median_ci95_us`, `p99_ci95_us` | 95 % Student-t interval **across repetitions** — the one that answers "is this difference real" |

   `REPETITIONS` in `config.conf` (default 3) drives it. Rounds are
   **interleaved** — a full sweep of every point, then the next sweep — so slow
   drift in the background load cannot masquerade as a difference between
   configurations. Frame-level statistics are computed on the pooled samples,
   matching the published method; only the interval differs. Figure 01 draws
   the across-repetition interval as error bars, and says so in its caption;
   with `REPETITIONS=1` it says instead that the run is single and differences
   may be drift.
3. **Frame-size sweep.** Upstream sweeps 64 B and 1518 B background frames; we
   fix `BG_DGRAM=1400`. Background frame size strongly affects switch queueing
   and is arguably the most interesting missing axis.
4. **Explicit IFG metrics.** Upstream tracks sender and receiver inter-frame gap
   as first-class measurements; we compute IPDV only.

### Tier 2 — measurement rigour

5. ~~**Single-PC hardware-timestamp self-test.**~~ **Implemented** as
   `scripts/tx_rate_selftest.sh`. Sweeps `tsn_tx` across rates on one machine —
   no switch, no second PC — reports hardware TX-timestamp yield and the
   `tx_hwtstamp_skipped` driver counter per rate, and names the highest rate
   holding ≥90 %. Run it before the first campaign and before raising
   `STREAM_RATE`.
6. **Persist IP and sysctl settings.** `ip addr add` and the `sysctl` ARP
   settings are applied at **runtime only** and are lost on reboot. Should be
   written to netplan/systemd-networkd and `/etc/sysctl.d/`. *This is a real
   defect, not merely a missing feature: a rebooted PC will silently lose the
   strict-ARP protection of §3.7.*
7. **Continuous PHC offset logging.** `meta.json` records offset only before and
   after each point. A per-second time series would let sync quality be
   correlated with latency outliers.
8. **Switch-side telemetry.** Read KSwitch queue depths, drops and per-priority
   counters around each run, so switch behaviour is observed rather than inferred.
9. ~~**Sampled timestamping, for rates above the register limit.**~~
   **IMPLEMENTED as `tsn_tx -N` / `STREAM_TS_EVERY`, and it turned out to be
   necessary for a reason nobody anticipated.** The original framing was wrong
   twice over. It assumed the constraint was *our own* yield, and it assumed
   100 % yield meant the rate was safe. Neither holds.

   What actually happens at 10 000 fps on `igc`: `tsn_tx` requests a TX
   timestamp on every frame and occupies the NIC's small set of TX-timestamp
   registers continuously. **ptp4l, on the same interface, needs one for every
   Pdelay_Req/Resp.** When it loses the race it logs

   ```
   timed out while polling for tx timestamp
   increasing tx_timestamp_timeout may correct this issue, but it is
   likely caused by a driver bug
   port 1 (enp1s0): send peer delay request failed
   port 1 (enp1s0): LISTENING to FAULTY on FAULT_DETECTED
   ```

   and the port cycles FAULTY → LISTENING → FAULTY indefinitely. The campaign
   then skips every remaining point, because without gPTP there is no common
   time base. Measured on the reference testbed:
   `tx_hwtstamp_skipped = 78719` against 78 713 frames missing a timestamp in
   the affected point — the driver's skip counter accounts for the shortfall
   almost exactly.

   The victim is ptp4l, not the measurement tool, which is why the yield column
   showed nothing wrong until the damage was already done — `tx_rate_selftest.sh`
   now polls `portState` per rate for exactly this reason.

   `-N EVERY` requests a timestamp on every EVERY-th frame using a per-packet
   `SO_TIMESTAMPING` control message on `sendmsg()` (the kernel masks it with
   `SOF_TIMESTAMPING_TX_RECORD_MASK`, so only the TX record bits are overridden;
   the socket keeps the generation bits). **The stream on the wire is
   unchanged** — same rate, same offered load — only the sampling of it is
   thinned. This is strictly better than lowering `STREAM_RATE`, which would
   change the experiment's stimulus.

10. **Hardware pacing via `SO_TXTIME` + `etf`.** *Now the binding constraint —
   and it is measured, not predicted.* `tx_rate_selftest.sh` on both PCs
   (kernel `6.8.1-1058-realtime`, idle machines, no background load):

   | Rate | Period | GAP SD | GAP MAX | Worst gap |
   |---|---|---|---|---|
   | 1 000 fps | 1000 µs | 28.4 µs (3 %) | 485 µs | 0.5 × period |
   | 5 000 fps | 200 µs | 25.4 µs (13 %) | 1365 µs | **7 × period** |
   | 10 000 fps | 100 µs | 24.3 µs (24 %) | 2419 µs | **24 × period** |
   | 20 000 fps | 50 µs | 11.8 µs (24 %) | 1607 µs | **32 × period** |
   | 50 000 fps | 20 µs | 10.8 µs (54 %) | 1113 µs | **56 × period** |

   Timestamp yield is 100 % at every one of these rates, so this is invisible
   in the yield column — the frames are timestamped, they just do not leave on
   schedule. `GAP MAX` stays around 0.5–2.4 ms regardless of rate, which is the
   signature of an occasional scheduler preemption rather than a rate-dependent
   limit: `clock_nanosleep` cannot hold a schedule finer than the wakeup jitter
   of the machine it runs on, RT kernel or not.

   Consequence for the experiment: per-frame latency stays valid (both
   timestamps are hardware), but at `STREAM_RATE=10000` **the stimulus is
   bursty, not a 100 µs CBR stream** — and queueing delay depends on the
   arrival pattern, so this belongs in the methodology section of any writeup.
   `igc`'s LaunchTime support (`SO_TXTIME` plus the `etf` qdisc) would move
   pacing into the NIC. Required before describing the stimulus as periodic.
11. **Optional pcap capture alongside CSV**, for post-hoc inspection of anomalies.

### Tier 3 — scientific extensions

12. **TAS / IEEE 802.1Qbv.** `taprio` on the host plus gate schedules on the
    KSwitches. Upstream's `scripts/txinject-TAS1.sh` and `txinject-TAS1-full.sh`
    are the reference — deliberately not read yet. This is the natural next
    experiment after 802.1p.
13. **CBS / IEEE 802.1Qav** credit-based shaper as a middle point between
    strict priority and TAS.
14. **Frame preemption (802.1Qbu / 802.3br).** I225/I226 supports it and recent
    `igc` exposes it; would need switch support too. Exploratory.
15. **Realistic traffic profiles.** Upstream's `pcap_writer` generates robotic,
    audio, video and podcast streams from real media. Replaying those (via
    `tcpreplay`, which needs no patch for this purpose) would connect results to
    application-level QoS instead of a synthetic CBR stream.
16. **Multiple concurrent talkers**, separated at the sink by source MAC as
    upstream does — the current design measures one stream.
17. **Per-hop decomposition.** With two switches, capturing at an intermediate
    point would separate per-switch contributions from end-to-end latency.
18. **Bidirectional measurement**, to check path symmetry.
19. **Host tuning study.** Upstream uses a 5.11-rt kernel; the effect of RT
    kernel, CPU isolation and IRQ affinity on the residual jitter floor is
    unmeasured here.

### Tier 4 — engineering

20. **Cleanup/teardown script** to restore both machines (re-enable `chrony`,
    remove units, clear qdiscs, remove sudoers drop-in).
21. **CI** — compile both tools and run the fixture pipeline on every change.
22. **Switch configuration as code**, so the KSwitch state that produced a
    result is captured with it.

---

## 10. Relationship to the upstream repository at runtime

Worth stating explicitly, because the setup instructions originally implied
otherwise: **the upstream clone is not a runtime dependency.** No tool, script
or analysis in this pack opens, imports or executes any upstream file. A grep of
the entire pack for `tsn-testbed`, `parsePcap`, `statsAnalysis`, `createPlot`,
`pcap_writer` and `measurements/` returns exactly one hit — the `RESULT_ROOT`
path in `config.conf`, which is only where output is written. Deleting the clone
changes nothing.

It becomes a dependency the moment open item #1 (the upstream-schema adapter) is
implemented, since that imports `statsAnalysis.py` and `createPlot.py` directly.
Until then it is a reference: for the TAS scripts (#11), the traffic generators
(#14), the confidence-interval notebook (#2), and for pinning the exact commit
this work deviates from.

---

## 11. Bottom line

The pack is a complete, self-consistent reimplementation that avoids every
patch, kernel rebuild and DPDK dependency of the original while keeping its
scientific structure. It has never touched the target hardware. The first hour
on the real testbed should be spent on unknowns #1–#4 of §8 — in that order —
before any campaign is run or any number is believed.
