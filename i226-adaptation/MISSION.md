# MISSION: bring up the TSN-FlexTest testbed on two UP Squared Pro 7000 Edge PCs

You are running on **PC 1**. Your job is to set up and run a TSN measurement
testbed across two Ubuntu PCs and two Kontron KSwitch D10 switches, then produce
plots comparing a No-QoS baseline against IEEE 802.1p prioritisation under
increasing background load.

Work autonomously. Only stop and ask if you hit a decision that is genuinely the
operator's to make (for example: switch configuration must be changed, or
hardware is miswired). Report at the end.

---

## 1. The testbed

### Hosts

| | PC 1 (talker, **this machine**) | PC 2 (listener) |
|---|---|---|
| user | `ivank` | `ivank` |
| management IP | `172.16.0.91` | `172.16.28.17` |
| password | *(not stored here — supply interactively, see Step 2)* | *(same)* |
| `enp1s0` (measured stream) | `192.168.1.61/24` | `192.168.1.71/24` |
| `enp2s0` (background load) | `192.168.1.62/24` | `192.168.1.72/24` |

Both PCs are UP Squared Pro 7000 Edge with two **Intel I226-IT** NICs
(`igc` driver).

### Wiring

```
PC1 enp1s0 ──► KSwitch1 port 1 ┐                    ┌ KSwitch2 port 1 ──► PC2 enp1s0
                               ├── inter-switch ────┤
PC1 enp2s0 ──► KSwitch1 port 2 ┘   link (shared)    └ KSwitch2 port 2 ──► PC2 enp2s0

KSwitch 1 = 192.168.1.13     KSwitch 2 = 192.168.1.14     gPTP enabled on both
```

**The inter-switch link is the whole point of the experiment.** Both port pairs
funnel through it, so background traffic pushed from `enp2s0` to `enp2s0`
congests the same egress queue the measured stream on `enp1s0` has to cross.
That is where 802.1p prioritisation either helps or does not.

---

## 2. Design decisions already made — do not re-litigate these

The upstream repository (`https://github.com/ivankl92/tsn-testbed`,
"TSN-FlexTest") **cannot be used as-is on this hardware**. Two hard blockers,
both already investigated:

1. **The latency mechanism does not port.** Upstream measures one-way latency by
   patching the Intel **igb** driver (`patches/linux-5.11.y-rt.patch`) to abuse
   the NIC's one-step PTP engine: `TSYNCTXCTL` is programmed with a byte offset
   of 48, so the NIC writes its hardware TX timestamp *into the frame payload*
   of every IPv4 packet. `parsePcap.py` then reads that back at bytes 50–57 and
   subtracts it from the receiver's pcap timestamp. The I226 uses the **igc**
   driver — that patch does not apply, and I225/I226 one-step behaviour differs.
   The `tcpreplay` patch is unrelated; it only adds `--sockprio`.

   **Replacement (already implemented, in `tools/`):** standard Linux
   `SO_TIMESTAMPING`. `tsn_tx` requests `SOF_TIMESTAMPING_TX_HARDWARE` and reads
   the real NIC TX timestamp back off the socket error queue; `tsn_rx` takes
   `SOF_TIMESTAMPING_RX_HARDWARE` on arrival. Frames carry a sequence number, so
   the two sides join offline. Both timestamps are raw PHC time and both PHCs are
   gPTP-disciplined to the switches, so the difference is a true NIC-to-NIC
   one-way latency. No kernel patch, no custom kernel, no reboot.

2. **The node count does not match.** Upstream expects 5 nodes (node0 sink,
   node1–3 talkers, node4 running MoonGen/DPDK) driven from a 6th controller.
   We have 2 PCs × 2 ports. **Mapping:** PC1 `enp1s0` is the single talker, PC2
   `enp1s0` is the sink, and the `enp2s0` pair carries `iperf3` UDP background
   load in place of MoonGen — no DPDK, no hugepages, no NIC binding.

Other decisions:

- **Baseline is PCP 0, not untagged.** Both configurations send byte-identical
  priority-tagged frames (VID 0); only the PCP differs (0 vs 6). That isolates
  the effect of prioritisation instead of also changing frame length.
- **Pack location:** `/home/tsn-testbed/i226-adaptation/`. The upstream repo is
  *not* required to run anything here (see Step 1); if cloned, keep it unmodified
  as a reference for the paper's methodology and the open items.
- **`chrony`/`systemd-timesyncd` get disabled** by the setup script. They fight
  `phc2sys` for `CLOCK_REALTIME`.

---

## 3. What this pack contains

```
i226-adaptation/
├── MISSION.md                 this file
├── tools/
│   ├── tsn_common.h           shared helpers, SIOCSHWTSTAMP, frame layout
│   ├── tsn_tx.c               paced talker, hardware TX timestamps
│   └── tsn_rx.c               listener, hardware RX timestamps
├── scripts/
│   ├── setup_node.sh          per-node install + gPTP + build (idempotent)
│   ├── bootstrap_ssh.sh       passwordless ssh + sudo to PC2
│   ├── qos_config.sh          apply/clear host mqprio
│   ├── config.conf            topology and run parameters
│   └── run_measurement.sh     the campaign driver (runs on PC1)
├── analysis/
│   ├── analyze_plot.py        join, statistics, figures
│   └── make_fixture.py        FABRICATED data, for testing the analysis only
└── docs/
    ├── REPORT.md              design decisions, patching, deltas vs upstream,
    │                          verification status, open features
    └── RUNBOOK.md             operating manual: setup, switch requirements,
                               configuration, running, evaluation, troubleshooting
```

`docs/RUNBOOK.md` is the human operating manual and duplicates the setup
sequence below in more detail, including the switch-side requirements and a
troubleshooting matrix. `docs/REPORT.md` explains why each design choice was
made and lists what is deliberately not implemented.

### The synthetic-data interlock - read this

`make_fixture.py` generates measurement-shaped data from a random number
generator with hand-picked parameters. It exists solely so `analyze_plot.py` can
be exercised without hardware. **Nothing it produces is a measurement.** Its
invented queueing model was tuned to show the outcome one *hopes* to see
(No-QoS degrading under load, 802.1p staying flat) - which is exactly the
question the real campaign is supposed to answer, so mistaking one for the other
would be self-confirming nonsense.

To make that mistake impossible:

- `make_fixture.py` writes a `SYNTHETIC` marker file into its output directory.
- `analyze_plot.py` detects the marker and then watermarks every figure
  ("SYNTHETIC FIXTURE / NOT MEASURED DATA"), retitles `summary.md`, and sets
  `data_source=SYNTHETIC_FIXTURE` in `summary.csv`.
- `run_measurement.sh` never writes the marker, so real campaigns are never
  watermarked and fixtures always are.

Never write that marker by hand, never remove it from a fixture directory, and
never present a watermarked figure as a result.

### What has and has not been tested

Be precise about this, because it determines where to expect trouble.

**Exercised:** both C tools compile clean with `-Wall -Wextra`; a veth loopback
run passed 600/600 frames end to end, confirming frame construction with the
802.1Q tag, payload layout, sequence recovery from the error-queue copy, pacing
and CSV format; `analyze_plot.py` ran against the fixture and rendered every
figure; the shell scripts passed `bash -n` and `shellcheck`.

**Never executed anywhere:** the hardware timestamping path itself. veth has no
PHC - `SIOCSHWTSTAMP` returns `EOPNOTSUPP` there - so that run used the `-S`
software fallback and never touched `HWTSTAMP_FILTER_ALL` or `ts[2]`. Also
untested: every script at runtime (apt, the systemd units, `pmc` output parsing,
ssh/scp, iperf3, `mqprio` on igc), gPTP lock, and whether the KSwitches forward
priority-tagged VID-0 frames.

**Formerly the highest-risk unknown, now measured:** I226 TX-timestamp yield.
`scripts/tx_rate_selftest.sh` on the target hardware (kernel
`6.8.1-1058-realtime`, `igc`) returned 100 % yield from 200 to 20 000 fps with
zero skipped frames. `STREAM_RATE` up to 10 000 fps is safe with a hardware
timestamp on every frame. Re-run the self-test if the kernel or NIC changes.
The remaining rate-related question is software pacing jitter, which that same
script now reports as GAP SD / GAP MAX.

---

## 4. Execution plan

Work through these in order. Each step has a gate — do not proceed past a failed
gate without either fixing it or reporting it.

### Step 1 — Get the code onto both PCs

```bash
sudo mkdir -p /home/tsn-testbed && sudo chown ivank:ivank /home/tsn-testbed
# place this pack at /home/tsn-testbed/i226-adaptation/
```

**The upstream clone is optional and is NOT a dependency.** No script, tool or
analysis in this pack reads, imports or executes anything from it — the only
mention of `tsn-testbed` in the code is the `RESULT_ROOT` path, which is just
where results are written. Clone it only if you intend to work on the open items
that use it (see `docs/REPORT.md` §9): the upstream-schema adapter (#1),
confidence intervals (#2), TAS (#12) or realistic traffic profiles (#15).

```bash
# optional, for the open items above:
git clone https://github.com/ivankl92/tsn-testbed.git /home/tsn-testbed/upstream
```

**Gate:** `/home/tsn-testbed/i226-adaptation/scripts/setup_node.sh` exists.

### Step 2 — Passwordless ssh and sudo to PC 2

```bash
cd /home/tsn-testbed/i226-adaptation/scripts
sudo ./bootstrap_ssh.sh ivank@172.16.28.17     # prompts for PC 2's password
```

This creates a key for **root** on PC1 (the campaign runs under `sudo`) and
installs `/etc/sudoers.d/99-tsn-flextest` on PC2.

**Gate:** `sudo ssh -o BatchMode=yes ivank@172.16.28.17 'sudo -n true && echo OK'`
prints `OK`.

### Step 3 — Copy the pack to PC 2 and set up both nodes

```bash
rsync -a /home/tsn-testbed/i226-adaptation/ ivank@172.16.28.17:/home/tsn-testbed/i226-adaptation/
```
(Create `/home/tsn-testbed` on PC2 first, owned by `ivank`.)

On **PC 2** (listener):
```bash
sudo /home/tsn-testbed/i226-adaptation/scripts/setup_node.sh \
     --stream-if enp1s0 --bg-if enp2s0 \
     --stream-ip 192.168.1.71/24 --bg-ip 192.168.1.72/24 --role listener
```

On **PC 1** (talker):
```bash
sudo /home/tsn-testbed/i226-adaptation/scripts/setup_node.sh \
     --stream-if enp1s0 --bg-if enp2s0 \
     --stream-ip 192.168.1.61/24 --bg-ip 192.168.1.62/24 --role talker
```

The script installs packages, verifies the NICs and their timestamping
capabilities, writes a gPTP (802.1AS) profile and systemd units, builds the
tools into `/opt/tsn-flextest/`, and waits for gPTP lock.

**Gate:** both invocations exit 0 and print `gPTP LOCKED`. Exit code 4 means no
lock — see §5.

### Step 4 — Verify the network before measuring

```bash
ping -c3 -I enp2s0 192.168.1.72          # background path
sudo pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET CURRENT_DATA_SET'   # both PCs
ethtool -T enp1s0                        # both PCs
```

**Gate:** `portState` is `SLAVE`/`CLIENT` and `offsetFromMaster` is inside
±1000 ns on both PCs, sustained over ~30 s.

### Step 5 — Quick functional run

```bash
cd /home/tsn-testbed/i226-adaptation/scripts
sudo ./run_measurement.sh --quick
```

Two QoS modes × two loads × 10 s. Its preflight sends 200 tagged frames and
tells you immediately if the switches drop priority-tagged traffic.

**Gate:** every point reports `tx=` and `rx=` counts within a few per-cent of
each other at 0 % load, and no `sw` appears in the `ts_src` column of any CSV
(that would mean a fallback to software timestamps).

### Step 6 — Full campaign

```bash
sudo ./run_measurement.sh
```

Two QoS modes × five loads (0/50/80/95/105 %) × 30 s ≈ 8–10 minutes. It prints
the results directory path on the last line.

### Step 7 — Analyse and plot

```bash
python3 /home/tsn-testbed/i226-adaptation/analysis/analyze_plot.py \
        /home/tsn-testbed/i226-adaptation/results/<run-id>
```

Produces `summary.csv`, `summary.md`, and `figures/` containing:

1. `01_latency_vs_load` — median and p99 latency vs background load, both modes
2. `02_latency_cdf` — latency CDF at the heaviest load
3. `03_latency_box` — spread per load, both modes
4. `04_frame_loss` — loss of the measured stream
5. `05_latency_timeseries` — per-frame latency at the heaviest load

**Gate:** figures exist and the numbers are physically plausible — a 512-byte
frame store-and-forwarded through two 1 Gbit/s switches should land in the tens
of microseconds. See §5 if they do not.

---

## 5. Known failure modes and what to do

**gPTP never locks.** Check the switch is grandmaster and gPTP is enabled on the
specific ports in use (KSwitch 1 = `192.168.1.13`, KSwitch 2 = `192.168.1.14`).
Confirm the profile matches: 802.1AS uses L2 transport, P2P delay mechanism,
`transportSpecific 0x1`, destination MAC `01:80:C2:00:00:0E`. Read
`journalctl -u ptp4l@enp1s0 -n 50`. If the switch runs a different sync interval,
adjust `logSyncInterval` in `/etc/linuxptp/gPTP.cfg`.

**`portState` is `MASTER` on a PC.** The PC won the BMCA, so it is not hearing
the switch. Either gPTP is off on that port or the cable is in the wrong port.
The profile already sets `clientOnly`/`slaveOnly` with worst-case priorities.

**Preflight says tagged frames do not pass but untagged do.** The KSwitch ports
are dropping VLAN-tagged frames. Either permit priority-tagged frames (VID 0) on
those ports, or pick a real VLAN id, configure it on both switches, and set
`STREAM_VID` in `config.conf`. **Report this rather than silently switching to
untagged** — without a PCP there is no 802.1p experiment.

**`ts_src` column shows `sw`.** Hardware timestamping fell back to software.
Check `ethtool -T enp1s0` for `hardware-transmit`, `hardware-receive` and
`HWTSTAMP_FILTER_ALL`. Note the ordering constraint: `ptp4l` must start
**before** `tsn_rx`, because `ptp4l` narrows the RX filter to PTP and `tsn_rx`
widens it back to `ALL`. If `ptp4l` restarts mid-run it will narrow it again.

**Few TX timestamps (`tsn_tx` warns).** The I226 has a small number of TX
timestamp registers. Lower `STREAM_RATE` in `config.conf` (try 500 pps).

**Latency looks negative or absurd.** The two PHCs are not actually on the same
time base — check gPTP on both. A small constant bias is normal (residual
grandmaster offset) and appears as an offset, not as jitter.

**802.1p shows no improvement.** Most likely the KSwitch is not doing strict
priority on PCP by default. Check the switch's PCP→traffic-class mapping and
scheduler. This is a finding worth reporting, not a bug to hide — say so
explicitly in the final report.

**Background load does not reach the requested rate.** Check `iperf3.json` in
each case directory for the achieved rate, and raise `BG_STREAMS` if a single
core is the limit. Report actual achieved load alongside requested load.

---

## 6. What to report at the end

- Whether each gate passed, and what you changed if one did not.
- The gPTP offset range observed on both PCs during the campaign.
- The summary table (`summary.md`).
- The headline finding: does 802.1p keep the stream's latency and loss flat as
  the shared inter-switch link saturates, and by how much?
- Honest caveats: software-timestamp fallbacks, loads not achieved, points
  skipped for lost sync, anything the switch configuration made impossible.

Collect the whole results directory (CSVs, logs, `meta.json`, figures) — it is
the evidence for every number in the report.
