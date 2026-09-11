# Runbook — operating the I226 / KSwitch D10 TSN testbench

Operating manual for the adapted TSN-FlexTest testbed. Covers prerequisites,
wiring, switch requirements, installation, configuration, execution, evaluation,
how to change the topology or targets, troubleshooting, and teardown.

For *why* it is built this way, see `REPORT.md`. For the autonomous-agent
version of the setup sequence, see `../MISSION.md`.

> **Before the first campaign, read §6 (verification gates).** The measurement
> path has now been validated end to end on the reference testbed, but the gates
> still matter on every setup and after every reboot — hardware timestamping,
> gPTP lock and tagged-frame forwarding can all fail silently, and skipping the
> gates produces plausible numbers that mean nothing.

---

## 1. Prerequisites

**Hardware**

- 2 × Linux PC, each with two Intel I226 NICs (`igc` driver). Reference: UP
  Squared Pro 7000 Edge.
- 2 × Kontron KSwitch D10 with gPTP enabled, one acting as grandmaster.
- 4 × Ethernet cable PC↔switch, 1 × inter-switch cable.

**Software on both PCs**

- Ubuntu (22.04 or 24.04). Stock kernel — no RT kernel or patched driver needed.
- `sudo` rights for the operating user.
- Package-manager network access on the management interface (`apt` must work).
- `sshd` running and reachable on PC 2 from PC 1.

**Access**

- Management addresses for both PCs, and PC 1 able to reach PC 2 over ssh.
- Administrative access to both switches (web UI or CLI).

**Reference configuration used throughout**

| | PC 1 (talker) | PC 2 (listener) |
|---|---|---|
| user | `ivank` | `ivank` |
| management IP | `172.16.0.91` | `172.16.28.17` |
| `enp1s0` — measured stream | `192.168.1.61/24` | `192.168.1.71/24` |
| `enp2s0` — background load | `192.168.1.62/24` | `192.168.1.72/24` |

Switches: KSwitch 1 `192.168.1.13`, KSwitch 2 `192.168.1.14`.

---

## 2. Topology

```
   PC 1 (talker)                                          PC 2 (listener)
  ┌──────────────┐                                       ┌──────────────┐
  │ enp1s0 .61   ├──► KSw1 p1 ┐                ┌ KSw2 p1 ─┤ enp1s0 .71   │  measured stream
  │              │            ├─ inter-switch ─┤          │              │
  │ enp2s0 .62   ├──► KSw1 p2 ┘   link (shared)└ KSw2 p2 ─┤ enp2s0 .72   │  background load
  └──────────────┘                                       └──────────────┘
        KSwitch 1 = 192.168.1.13        KSwitch 2 = 192.168.1.14
```

**Why the wiring matters.** Both port pairs traverse the *same* inter-switch
link. Background load pushed from `enp2s0` to `enp2s0` therefore contends for
the same egress queue the measured stream on `enp1s0` must cross. That
contention is the entire experiment. If the two pairs were on independent paths,
there would be no congestion and 802.1p would have nothing to arbitrate — the
measurement would return a flat line and mean nothing.

Cabling checks that are easy to get wrong:

- Port 1 of each PC must reach port 1 of *its* switch, port 2 to port 2. A
  crossed pair silently turns the experiment into a no-contention run.
- The inter-switch link must be a **single** link. A LAG or a second cable
  removes the bottleneck.
- gPTP must be enabled on **all four** access ports and on the inter-switch link.

---

## 3. Switch configuration

The switches are not configured by these scripts. Four properties must hold; how
you set them is per the Kontron KSwitch D10 documentation (web UI or CLI).

| # | Requirement | Why | How to verify |
|---|---|---|---|
| 1 | **gPTP (802.1AS) enabled** on all four access ports and the inter-switch link | The PCs' PHCs must share a time base or latency is meaningless | §6 gate B: `portState` SLAVE/CLIENT on both PCs |
| 2 | **A grandmaster exists in the domain**, and both PCs are clients | The PCs are configured `gmCapable 0` and will never win the BMCA. The GM may be a KSwitch or any other 802.1AS device the switches relay from — it does not matter which | If a PC shows `portState MASTER`, it is not hearing the network |
| 3 | **Priority-tagged frames (VID 0) accepted and forwarded** on the four access ports | The measured stream carries its PCP in an 802.1Q tag with VID 0 | §6 gate C: the preflight in `run_measurement.sh` |
| 4 | **PCP → traffic class mapping active, with strict priority** on the egress queue of the inter-switch link | This is what makes 802.1p do anything. PCP 6 must land in a higher-priority queue than PCP 0 | If absent, the 802.1p run will look identical to the baseline |

Requirement 4 is the one to check first if results show no difference between
QoS modes. **That outcome is a finding about the switch configuration, not a bug
in the testbench** — report it as such rather than adjusting the experiment
until it produces the expected picture.

If requirement 3 cannot be met (ports reject tagged frames), do **not** fall
back to untagged: untagged frames carry no PCP and there is no 802.1p experiment
left. Instead configure a real VLAN on all five links and set `STREAM_VID` in
`config.conf` accordingly.

---

## 4. Installation

On **PC 1**:

```bash
git clone https://github.com/ivankl92/FlexTest.git /home/ivank/tsn-testbed
# result: /home/ivank/tsn-testbed/i226-adaptation/
```

No `sudo` — the tree lives in the operating user's home directory and is owned
by that user throughout. Only the setup and campaign scripts need root.

Cloning **into** `/home/ivank/tsn-testbed` rather than under it is deliberate:
the repository root holds `i226-adaptation/`, so every path in this runbook —
`/home/ivank/tsn-testbed/i226-adaptation/...` — resolves as written. `git clone`
creates the target directory itself.

Substitute your own home directory if the operating user is not `ivank`; the
path must be **identical on both PCs**, because the campaign driver invokes the
scripts on PC 2 over ssh by absolute path.

To pick up later changes: `git -C /home/ivank/tsn-testbed pull`. If you have edited
`config.conf` in place, commit or stash it first — `pull` will refuse to
overwrite local changes.

**Verify:** `/home/ivank/tsn-testbed/i226-adaptation/scripts/setup_node.sh` exists
and is executable:

```bash
ls -l /home/ivank/tsn-testbed/i226-adaptation/scripts/*.sh
```

The scripts are committed mode `100755`, so a `git clone` on Linux already
produces them executable — **do not run `chmod +x` as a matter of course.** A
manual `chmod` turns the mode into an uncommitted local change, and the next
`git pull` then aborts with *"Your local changes to the following files would be
overwritten by merge"* (§12).

Only if the `x` bits really are missing — which happens when the tree passed
through a filesystem that cannot store them (a Windows checkout, a cloud-sync
folder, an unzipped archive) — restore them:

```bash
chmod +x /home/ivank/tsn-testbed/i226-adaptation/scripts/*.sh
```

Without the bit, `sudo ./script.sh` fails with **`command not found`**, which
reads like a missing file but is a missing `x` bit. `python3 analysis/*.py` is
unaffected — those run through the interpreter.

### 4.1 Do you need the upstream repository?

**No — not to run anything in this pack.** It is not a dependency. No tool,
script or analysis here reads, imports or executes any upstream file; the only
occurrence of `tsn-testbed` in the code is the `RESULT_ROOT` path, which is
merely where results are written. You can delete the clone and everything still
works.

Clone it when you take on one of these, and not before:

| You want to… | You need | Open item |
|---|---|---|
| Produce statistics/figures comparable with the paper and the ieee-dataport datasets | `src/evaluation/statsAnalysis.py`, `createPlot.py`, `paper_*.ipynb` — via the adapter | #1 |
| Add repetitions and confidence intervals | `src/evaluation/confidence-interval.ipynb` as a reference | #2 |
| Add TAS / 802.1Qbv | `src/measurements/scripts/txinject-TAS1{,-full}.sh` as the reference configuration | #12 |
| Drive realistic application traffic instead of a synthetic CBR stream | `src/pcap_writer/` | #15 |
| Document precisely what you deviated from, with a pinned commit | the tree itself | — |

If so:

```bash
git clone https://github.com/ivankl92/tsn-testbed.git /home/ivank/tsn-upstream
```

Keep it unmodified — it is a reference, not a component. Clone it outside
`/home/ivank/tsn-testbed`: that directory is now this repository's working tree, and a
second clone inside it would show up as untracked files.

---

## 5. Setup

### 5.1 Passwordless ssh and sudo to PC 2

```bash
cd /home/ivank/tsn-testbed/i226-adaptation/scripts
sudo ./bootstrap_ssh.sh ivank@172.16.28.17
```

The password is prompted for (or taken from `$SSHPASS`) and is never written to
disk or logs. This creates a key for **root** on PC 1 — the campaign runs under
`sudo`, so root's key is the one used — installs it on PC 2, and writes
`/etc/sudoers.d/99-tsn-flextest` there for non-interactive `sudo`.

**Verify:**

```bash
sudo ssh -o BatchMode=yes ivank@172.16.28.17 'sudo -n true && echo OK'
```

### 5.2 Copy the pack to PC 2

Clone the same repository on PC 2:

```bash
ssh ivank@172.16.28.17 'git clone https://github.com/ivankl92/FlexTest.git /home/ivank/tsn-testbed'
```

If PC 1 carries local changes that are not committed and pushed — an edited
`config.conf`, a patched script — copy the tree instead, so both nodes run
identical code:

```bash
rsync -a /home/ivank/tsn-testbed/i226-adaptation/ ivank@172.16.28.17:/home/ivank/tsn-testbed/i226-adaptation/
```

### 5.3 Run node setup — PC 2 first, then PC 1

PC 2 (listener):

```bash
sudo /home/ivank/tsn-testbed/i226-adaptation/scripts/setup_node.sh \
     --stream-if enp1s0 --bg-if enp2s0 \
     --stream-ip 192.168.1.71/24 --bg-ip 192.168.1.72/24 --role listener
```

PC 1 (talker):

```bash
sudo /home/ivank/tsn-testbed/i226-adaptation/scripts/setup_node.sh \
     --stream-if enp1s0 --bg-if enp2s0 \
     --stream-ip 192.168.1.61/24 --bg-ip 192.168.1.62/24 --role talker
```

The script is idempotent — re-run it freely. `--no-ip-config` skips address
assignment if your addresses are managed elsewhere.

**Exit codes:** `0` success (prints `gPTP LOCKED`); `5` ptp4l rejected the
generated `/etc/linuxptp/gPTP.cfg` (its own message is printed — fix the config
before anything else); `4` no gPTP lock within 90 s
(prints `pmc` state and the last 20 ptp4l journal lines).

### 5.4 What setup changes on each machine

Know this before running it on a shared machine, and for teardown (§12).

| Change | Path / effect |
|---|---|
| Packages installed | `build-essential linuxptp ethtool iperf3 tcpdump iproute2 git python3-{numpy,pandas,matplotlib}` |
| **Services disabled** | `chrony`, `systemd-timesyncd` — they fight `phc2sys` for `CLOCK_REALTIME` |
| gPTP profile written | `/etc/linuxptp/gPTP.cfg` |
| Units written & started | `/etc/systemd/system/ptp4l@.service`, `phc2sys@.service`, and on the listener `iperf3-bg.service` |
| Binaries installed | `/opt/tsn-flextest/{tsn_tx,tsn_rx,qos_config.sh}` |
| IP addresses | added to the two interfaces — **runtime only, lost on reboot** |
| sysctl | `arp_filter=1`, `arp_announce=2`, `arp_ignore=1`, `rp_filter=0` per interface — **runtime only, lost on reboot** |

> ⚠️ **The last two rows are not persisted.** After a reboot, re-run
> `setup_node.sh` before measuring. Without the strict-ARP settings, Linux may
> answer ARP for `.72` out of `enp1s0` and route background load onto the
> measurement port — which invalidates every result while looking completely
> normal. Persisting these is open item #6 in `REPORT.md`.

---

## 6. Verification gates

Run these in order. Do not measure past a failed gate.

**Gate A — interfaces and timestamping capability** (both PCs)

```bash
ip -br addr show dev enp1s0; ip -br addr show dev enp2s0
ethtool -T enp1s0
```

Require: both links `UP`, correct addresses, and `ethtool -T` listing
`hardware-transmit` and `hardware-receive` under **Capabilities**, plus `all`
under **Hardware Receive Filter Modes**. Depending on ethtool version that last
one prints either as a bare `all` or as `all (HWTSTAMP_FILTER_ALL)` — both mean
the same thing. On I226/`igc` the expected filter list is just `none` and
`all`.

**Gate B — gPTP lock** (both PCs, sustained ~30 s)

```bash
sudo pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET PORT_DATA_SET'    | grep portState
sudo pmc -u -b 0 -f /etc/linuxptp/gPTP.cfg 'GET CURRENT_DATA_SET' | grep offsetFromMaster
```

Require: `portState SLAVE` (or `CLIENT`) and `|offsetFromMaster| < 1000 ns`,
stable over time. `MASTER` means the PC cannot hear the switch.

**Gate C — background path**

```bash
ping -c3 -I enp2s0 192.168.1.72
```

**Gate D — end-to-end functional run**

```bash
cd /home/ivank/tsn-testbed/i226-adaptation/scripts
sudo ./run_measurement.sh --quick        # 2 QoS modes × 3 loads (0/50/105 %) × 5 s, 1 repetition
```

Require: the preflight passes; `tx=` and `rx=` counts within a few per cent at
0 % load; and **no `sw` in the `ts_src` column of any CSV** —

```bash
cut -d, -f4 results/*/*/tx.csv results/*/*/rx.csv | sort -u    # expect: ts_src, hw
```

**Gate E — TX timestamp yield** (run this before the first full campaign, and
before raising `STREAM_RATE`)

The direct test needs one PC only — no switch, no PC 2, no gPTP:

```bash
sudo /home/ivank/tsn-testbed/i226-adaptation/scripts/tx_rate_selftest.sh enp1s0
```

It sweeps the frame rate, reports the hardware-timestamp yield at each step and
names the highest rate that holds ≥90 %. Set `STREAM_RATE` at or below that.
Frames go to an unused MAC and simply leave the port, so it is safe to run at
any time.

Or read it from a completed run:

Check the `tsn_tx` stderr line in `results/*/*/tx.log`:

```
[enp1s0] sent=10000 send_errors=0 hw_tx_timestamps=10000 (100.00%) -> tx.csv
```

Below ~90 % the I226's TX-timestamp registers are saturating. Reduce
`STREAM_RATE` (try 500, then 200) and re-check. This was the highest-ranked
unknown in the design until it was measured: on kernel `6.8.1-1058-realtime`
with `igc`, yield held at 100 % from 200 to 20 000 fps. Re-measure after a
kernel or NIC change — see `REPORT.md` §8.

---

## 7. Configuration reference — `scripts/config.conf`

| Variable | Default | Meaning / effect |
|---|---|---|
| `PC2_USER` | `ivank` | ssh user on the listener |
| `PC2_MGMT` | `172.16.28.17` | management address used for ssh/scp (**not** a measurement path) |
| `PC1_STREAM_IF` | `enp1s0` | talker egress, measured stream |
| `PC1_BG_IF` | `enp2s0` | talker egress, background load |
| `PC2_STREAM_IF` | `enp1s0` | listener ingress, measured stream |
| `PC2_BG_IF` | `enp2s0` | listener ingress, background load |
| `PC1_BG_IP` / `PC2_BG_IP` | `.62` / `.72` | iperf3 source/destination for background load |
| `STREAM_RATE` | `10000` | frames/s of the measured stream — a 100 µs period. Validated at 100 % hardware timestamp yield up to 20 000 fps on kernel 6.8.1-realtime / `igc`. **Re-run gate E and lower this if yield drops.** |
| `STREAM_SIZE` | `512` | frame bytes on the wire incl. Ethernet header; 64…1522 |
| `STREAM_DURATION` | `30` | seconds per measurement point → `RATE × DURATION` frames |
| `STREAM_PCP_LOW` | `0` | PCP for the baseline run |
| `STREAM_PCP_HIGH` | `6` | PCP for the 802.1p run — must map to a high-priority switch queue |
| `STREAM_VID` | `0` | `0` = priority-tagged (no VLAN membership). Set a real VID if the switch requires one |
| `STREAM_SOCKPRIO` | `3` | `SO_PRIORITY`; must match the `map` in `qos_config.sh` (priority 3 → tc 0) |
| `BG_LOADS` | `0 50 80 95 105` | background load points, % of detected link rate. `105` deliberately oversubscribes |
| `BG_STREAMS` | `4` | parallel iperf3 UDP streams (one stream is often CPU-bound below line rate) |
| `BG_DGRAM` | `1400` | UDP payload bytes for background traffic |
| `QOS_MODES` | `none dot1p` | configurations compared. `none` → `STREAM_PCP_LOW`, `dot1p` → `STREAM_PCP_HIGH`. Reverse the order (`dot1p none`) as a control: if a difference follows the *position* rather than the mode, it is drift, not QoS |
| `REPETITIONS` | `3` | measurements per point, interleaved round by round. **1 is not enough** — run-to-run drift on this testbed reached 11 µs at the median, which is larger than any QoS effect seen so far. Runtime scales linearly |
| `STREAM_TS_EVERY` | `1` | request a hardware TX timestamp on every Nth frame. Raise to 4–10 if ptp4l goes FAULTY (§12); the stream on the wire is unchanged |
| `RESULT_ROOT` | `.../results` | where run directories are created |

**Runtime estimate:** `|QOS_MODES| × |BG_LOADS| × (STREAM_DURATION + ~20 s)`.
Defaults ≈ 8–10 minutes. At `STREAM_RATE=10000` each point captures 300 000
frames, so expect roughly 10 MB of CSV per point and ~200 MB per campaign.

Changing `STREAM_SOCKPRIO` requires editing the `map` in `qos_config.sh` to
match — the two are a pair.

---

## 8. Running a measurement

```bash
cd /home/ivank/tsn-testbed/i226-adaptation/scripts
sudo ./run_measurement.sh              # full campaign
sudo ./run_measurement.sh --quick      # smoke test: loads 0/50/105 %, 5 s each, no repetitions (~2.5 min)
sudo ./run_measurement.sh --config /path/to/other.conf
```

The last line printed is the results directory path.

What happens per measurement point: gPTP checked on both nodes → QoS applied on
both → `tsn_rx` started on PC 2 → background load started → `tsn_tx` sends →
`rx.csv` fetched → gPTP re-checked → `meta.json` written → 5 s cool-down.

If gPTP is lost, the point is retried once after 60 s and then **skipped and
marked** with an `ERROR` file — never measured out of sync. A skipped point
appears as a gap in the plots, which is correct and intentional.

Safe to interrupt with Ctrl-C: the exit trap stops `iperf3` and any remote
`tsn_rx`. Completed points remain valid.

---

## 9. Output

```
results/<YYYYmmdd-HHMMSS>/
├── config.conf.used                 exact configuration for this run
├── none_load0/ … dot1p_load105/     one directory per measurement point
│   ├── tx.csv                       seq,tx_hw_ns,sw_tx_ns,ts_src
│   ├── rx.csv                       seq,rx_hw_ns,sw_tx_ns,ts_src
│   ├── tx.log / rx.log              tool stderr — yield and filter warnings
│   ├── iperf3.json / iperf3.err     achieved background load
│   ├── meta.json                    parameters + PTP state before/after + counts
│   └── ERROR                        present only if the point was skipped
├── summary.csv / summary.md         written by analyze_plot.py
└── figures/                         written by analyze_plot.py
```

`RESULT_ROOT` sits inside the repository working tree, so run directories appear
as untracked files in `git status`. The repository's `.gitignore` excludes
`i226-adaptation/results/` for that reason. The campaign runs under `sudo`, so
those directories are owned by **root** — `sudo chown -R "$USER:$USER"` the run
directory if you want to prune or move results without `sudo`.

**CSV schema.** `seq` is the frame's sequence number, the join key.
`tx_hw_ns` / `rx_hw_ns` are raw PHC nanoseconds from the sending and receiving
NIC. `sw_tx_ns` is a software `CLOCK_TAI` reading at `send()`, carried for
diagnostics only and never used to compute latency. `ts_src` is `hw` or `sw` per
row — **any `sw` means that sample fell back to a software timestamp and is not
a NIC-to-NIC measurement.**

**One-way latency** = `rx_hw_ns − tx_hw_ns`. Valid because both values are raw
PHC time and both PHCs are gPTP-disciplined to the same grandmaster; residual
grandmaster offset appears as a constant bias, not as jitter.

---

## 10. Evaluation

```bash
python3 /home/ivank/tsn-testbed/i226-adaptation/analysis/analyze_plot.py \
        /home/ivank/tsn-testbed/i226-adaptation/results/<run-id>
```

Produces `summary.csv`, `summary.md`, and in `figures/` (PNG + PDF):

| Figure | Shows |
|---|---|
| `01_latency_vs_load` | median and p99 latency vs background load, both QoS modes — the headline |
| `02_latency_cdf` | latency distribution at the heaviest load |
| `03_latency_box` | per-load spread, quartiles and 1st…99th percentile |
| `04_frame_loss` | loss of the measured stream |
| `05_latency_timeseries` | per-frame latency at the heaviest load (diagnostic) |

Columns in `summary.csv`: `data_source`, `qos_mode`, `series`,
`background_load_pct`, `repetitions`, `median_across_reps_us`,
`median_ci95_us`, `p99_across_reps_us`, `p99_ci95_us`, `mean_ci99_us_upstream`,
`frames_{sent,received,matched}`, `tx_timestamps`, `tx_ts_yield_pct`,
`loss_pct`, `lat_{min,mean,median,p99,p999,max,std}_us`, `ipdv_p99_abs_us`,
`ipdv_frame_spacing`, `software_timestamps`.

**Which interval to quote.** `median_ci95_us` / `p99_ci95_us` are computed
**across repetitions** and are the ones that say whether a difference between
two configurations is real. `mean_ci99_us_upstream` reproduces upstream's
`confidence-interval.ipynb` formula (per-frame, within a single run, 99 %) so
results stay comparable with the published work — but it is typically ~100×
narrower and is blind to run-to-run drift. Do not use it to argue that two
configurations differ.

**Sanity checks on the numbers**

- A 512 B frame store-and-forwarded through two 1 Gbit/s switches should land in
  the **tens of microseconds**. Sub-microsecond or millisecond values mean
  something is wrong.
- `lat_min_us` far below the physical floor, or negative, means the two PHCs are
  not on a common time base — re-check gate B.
- `software_timestamps = True` on any row invalidates that row as a NIC-to-NIC
  measurement.
- Compare requested load (`meta.json`) with achieved load (`iperf3.json`).
  Report achieved.

**Testing the analysis without hardware**

```bash
python3 analysis/make_fixture.py /tmp/fixture-run
python3 analysis/analyze_plot.py /tmp/fixture-run
```

`make_fixture.py` fabricates data from a random number generator; **nothing it
produces is a measurement**. It writes a `SYNTHETIC` marker that causes every
figure to be watermarked and `summary.csv` to carry
`data_source=SYNTHETIC_FIXTURE`. Never create that marker by hand, never delete
it from a fixture directory, and never present a watermarked figure as a result.

---

## 11. Changing the setup

### Different interface names or addresses

Edit `PC{1,2}_{STREAM,BG}_IF` and `PC{1,2}_BG_IP` in `config.conf`, and pass the
matching `--stream-if/--bg-if/--stream-ip/--bg-ip` to `setup_node.sh`. The
measured stream is addressed by **MAC**, discovered automatically, so its IP is
irrelevant to the measurement — but keep it assigned for diagnostics.

### Different rate, frame size or duration

`STREAM_RATE`, `STREAM_SIZE`, `STREAM_DURATION`. Re-check gate E after raising
the rate. Frame sizes below 64 or above 1522 are rejected.

### Different or additional load points

`BG_LOADS` accepts any space-separated percentages; values above 100
oversubscribe. Runtime scales linearly.

### Swapping talker and listener

Exchange the `PC1_*`/`PC2_*` values and re-run `setup_node.sh` with
`--role listener` on the new listener. Direction is otherwise symmetric.

### Using a real VLAN instead of priority tagging

Configure the VLAN on all five links, then set `STREAM_VID` to that id. No code
changes.

### Adding a third node or a second stream

Not supported by `run_measurement.sh` as written. `tsn_tx`/`tsn_rx` already
distinguish frames by source MAC and sequence number, so the tools are ready;
the campaign driver and `analyze_plot.py` would need extending. Upstream
separates streams by the last byte of the source MAC — worth mirroring.

### Adding TAS (802.1Qbv)

Not implemented. Requires `taprio` on the host plus gate schedules on both
switches. Upstream's `src/measurements/scripts/txinject-TAS1.sh` and
`txinject-TAS1-full.sh` are the reference (open item #12). Add a `tas` entry to `QOS_MODES` and a
corresponding branch in `qos_config.sh`.

### Changing the host queue mapping

`qos_config.sh` maps socket priority 3 → traffic class 0 → hardware queue 0.
If you change `STREAM_SOCKPRIO`, change the `map` line to match: the map is
indexed by priority, and its value is the traffic class.

---

## 12. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `setup_node.sh` exits 4, no gPTP lock | gPTP off on that switch port; wrong port; profile mismatch | Check switch config; `journalctl -u ptp4l@enp1s0 -n 50`; if the switch uses a different sync interval, adjust `logSyncInterval` in `/etc/linuxptp/gPTP.cfg` |
| `portState MASTER` on a PC | PC is not hearing the network at all | gPTP disabled on that port, or cable in the wrong port. The profile already forces `gmCapable 0` |
| `portState FAULTY` and every later point skipped; `journalctl -u ptp4l@enp1s0` shows *timed out while polling for tx timestamp* / *send peer delay request failed* | **`tsn_tx` is starving ptp4l of TX-timestamp registers.** The NIC has only a few; at 10 000 fps this tool holds them continuously and ptp4l cannot get one for its Pdelay messages. Confirm with `ethtool -S enp1s0 \| grep tx_hwtstamp_skipped` — a large count is the proof. Note the fault is *not* load-dependent: it has appeared at 80 % and at 105 %, and not at all in between | Set **`STREAM_TS_EVERY=10`** in `config.conf`. This thins the timestamp *requests* while leaving the stream on the wire unchanged, so the experiment's stimulus is unaffected. Then `sudo systemctl reset-failed ptp4l@enp1s0 && sudo systemctl restart ptp4l@enp1s0`. Raising `tx_timestamp_timeout` only delays the fault; lowering `STREAM_RATE` works but changes the offered load |
| No lock, and `journalctl -u ptp4l@enp1s0` shows `Cannot mix 1588 clientOnly with 802.1AS !gmCapable` / `failed to create a clock` | The profile sets both `gmCapable 0` and `clientOnly`/`slaveOnly`. ptp4l refuses that pair and exits 255; `Restart=always` turns it into a crash loop, so the visible symptom is "no gPTP lock" rather than a config error | Delete the `clientOnly`/`slaveOnly` line from `/etc/linuxptp/gPTP.cfg` — `gmCapable 0` alone is the 802.1AS way to say "never grandmaster". Then `systemctl reset-failed ptp4l@enp1s0 && systemctl restart ptp4l@enp1s0` |
| `ptp4l@…: Start request repeated too quickly` | systemd's restart limit tripped after repeated crashes; it will not retry even once the cause is fixed | `sudo systemctl reset-failed ptp4l@enp1s0` before restarting |
| Preflight: "tagged frames do not pass but untagged do" | KSwitch ports drop VLAN-tagged frames | Permit priority-tagged (VID 0) frames, or configure a real VLAN and set `STREAM_VID`. **Do not** switch to untagged |
| Preflight: no frames arrive at all | Cabling, link down, or switch not forwarding | Check `ip -br link`, switch MAC table, and that both ends use the same port pair |
| `ts_src` shows `sw` | Hardware timestamping unavailable or filter reverted | `ethtool -T enp1s0`; ensure ptp4l started **before** `tsn_rx` — ptp4l narrows the RX filter to PTP and `tsn_rx` widens it back to ALL. A ptp4l restart mid-run re-narrows it |
| `tsn_tx` warns on low timestamp yield | I226 TX-timestamp registers saturating | Lower `STREAM_RATE` to 500 or 200 |
| Latency negative or absurd | PHCs not on a common time base | Re-check gate B on both PCs; a small constant bias is normal |
| Latency plausible but identical in both QoS modes | Switch not applying strict priority on PCP | Check the KSwitch PCP→traffic-class mapping and scheduler. **Report this as a finding** |
| Background load far below requested | iperf3 CPU-bound | Raise `BG_STREAMS`; report achieved load from `iperf3.json` |
| Points skipped with `ERROR` | gPTP lost during the run | Check sync stability; look for switch topology changes or link flaps |
| Background traffic appears on the measurement port | Strict ARP settings lost (e.g. after reboot) | Re-run `setup_node.sh`; see §5.4 |
| `run_measurement.sh`: "passwordless ssh does not work" | Bootstrap not run, or run as the wrong user | Re-run `bootstrap_ssh.sh` under `sudo` — root's key is the one used |
| `sudo ./<script>.sh` → `command not found`, but the file is there | The script has no executable bit — `sudo` reports it this way rather than "permission denied" | `chmod +x scripts/*.sh` (§4). One-off alternative: `sudo bash ./<script>.sh` |
| `git pull` → "Your local changes to the following files would be overwritten by merge", listing the scripts | A manual `chmod +x` counts as a local modification, because git tracks the executable bit. Nothing in the file content differs | Confirm it is mode-only — `git diff -- i226-adaptation/scripts/` shows just `old mode`/`new mode` lines and no content hunks — then `git checkout -- i226-adaptation/scripts/ && git pull`. The scripts arrive executable from the repository |

---

## 13. Teardown

No teardown script exists yet (open item #20 in `REPORT.md`). To restore a
machine by hand:

```bash
sudo systemctl disable --now ptp4l@enp1s0 phc2sys@enp1s0 iperf3-bg 2>/dev/null
sudo rm -f /etc/systemd/system/{ptp4l@,phc2sys@,iperf3-bg}.service
sudo rm -rf /etc/linuxptp/gPTP.cfg /opt/tsn-flextest
sudo systemctl daemon-reload
sudo tc qdisc del dev enp1s0 root 2>/dev/null
sudo systemctl enable --now chrony        # restore normal time sync
sudo rm -f /etc/sudoers.d/99-tsn-flextest # on PC 2
```

The runtime IP and sysctl changes disappear on reboot by themselves.

---

## 14. Reproducibility checklist

Before treating a campaign as citable, record:

- [ ] `config.conf.used` from the run directory
- [ ] Kernel and `igc` driver version on both PCs (`uname -a`, `ethtool -i enp1s0`)
- [ ] `linuxptp` version (`ptp4l -v`)
- [ ] `ethtool -T enp1s0` output from both PCs
- [ ] KSwitch firmware version and the gPTP / PCP-mapping / scheduler configuration of both switches
- [ ] gPTP offset range observed during the campaign (`meta.json`, `ptp_before` / `ptp_after`)
- [ ] TX-timestamp yield per point (`tx.log`) and confirmation that no row has `ts_src=sw`
- [ ] Achieved versus requested background load per point
- [ ] Any points skipped, and why
- [ ] Confirmation that `summary.csv` shows `data_source=measured`, not `SYNTHETIC_FIXTURE`

Cite the TSN-FlexTest papers (NetSoft 2022; IEEE TNSM 21(2) 2024,
doi:10.1109/TNSM.2023.3327108) — the method originates there, even though this
implementation shares no code with it.

Note the deviations from the published method in any writeup: hardware
`SO_TIMESTAMPING` instead of the patched-`igb` payload timestamp, `iperf3`
instead of MoonGen, a synthetic CBR stream instead of replayed application
pcaps, one talker instead of three, and a single run per point without
confidence intervals.
