# Handoff — `i226-adaptation` measurement chain

Status as of **2026-09-17**. Written for an agent picking this up cold.
Scope: the latency-measurement testbed in `i226-adaptation/`. The sibling
subproject `topology-discovery/` (NETCONF/YANG capability retrieval, added
2026-09-14) has its own handover and is unrelated to what follows.

---

## 1. One-paragraph orientation

Two Ubuntu PCs with Intel I226 NICs measure one-way latency of a paced
512-byte stream across two Kontron KSwitch D10 switches, under iperf3
background load, comparing **No-QoS (PCP 0)** against **IEEE 802.1p
(PCP 6)**. It is an independent reimplementation of the TSN-FlexTest method
(NetSoft 2022; IEEE TNSM 21(2) 2024, doi:10.1109/TNSM.2023.3327108) — it
shares no code with upstream. Timestamps are hardware `SO_TIMESTAMPING` on
both ends, with gPTP (802.1AS via linuxptp) giving the common time base.

**The measurement chain now works end to end.** As of run
`20260911-163017`, background load actually reaches the link, latency
responds to it monotonically, and every gate passes. That was not true for
most of the preceding work; §3 explains why, and those failure modes are the
most useful thing in this document.

---

## 2. Where everything is

| | |
|---|---|
| Repo | `github.com/ivankl92/FlexTest`, subdirectory `i226-adaptation/` |
| On UP-1 (talker) | `/home/ivank/tsn-testbed/i226-adaptation/` |
| On UP-2 (listener) | same absolute path — the driver ssh's by absolute path |
| Working copy in chat sessions | connected folder `C:\Users\ivank\Nextcloud\Dissertation\TSN\Benchmarking\FlexTest` |
| Results | `i226-adaptation/results/<run-id>/` |

**Workflow the user has asked for: make edits in the connected folder. The
user pushes to GitHub manually.** Do not attempt to push. Files written from
a session go into the connected folder; the user commits.

Nodes: UP-1 `172.16.0.91`, UP-2 `172.16.28.17`, user `ivank` on both.
`enp1s0` = measured stream, `enp2s0` = background load, on both machines.

Read `docs/RUNBOOK.md` (operating manual, troubleshooting) and
`docs/REPORT.md` (design rationale, verification status, open items) before
changing anything. They are current.

---

## 3. What was wrong, and how it was found

Four failures, in the order they were diagnosed. **All four were silent or
misattributed** — that pattern is the point, and it is written up in
REPORT §8.

### 3.1 ptp4l driven FAULTY by TX-timestamp starvation

`ethtool -S enp1s0` showed `tx_hwtstamp_skipped: 78719`. The I226 has only a
few TX-timestamp registers; at 10 000 fps `tsn_tx` held them continuously and
ptp4l could not get one for its Pdelay messages, so the port went FAULTY and
every subsequent measurement point aborted.

Fixed by adding `-N EVERY` to `tsn_tx` (config: `STREAM_TS_EVERY`), which
requests a hardware TX timestamp on every Nth frame via a per-packet
`SO_TIMESTAMPING` cmsg. **The stream on the wire is unchanged** — only the
sampling of it — so the offered load is unaffected. The kernel masks the
per-packet value with `SOF_TIMESTAMPING_TX_RECORD_MASK`, so only the TX
*record* bits are overridable per packet; the generation bits stay on the
socket. The user runs with `STREAM_TS_EVERY=4` on the machine
(`config.conf` in the repo still ships `1`).

### 3.2 iperf 3.16 segfaults on every UDP test

Ubuntu 24.04 ships iperf 3.16 — the release that made iperf3 multi-threaded
— and its thread-lifetime bugs kill the *server* the moment a UDP test
attaches:

```
iperf3-bg.service: Main process exited, code=dumped, status=11/SEGV
Scheduled restart job, restart counter is at 14.
```

Consequence: **no background traffic at all, and no error anywhere else.**
A whole campaign returned 13.14 µs at 0 %, 50 % *and* 105 % load — a clean,
self-consistent, entirely meaningless dataset that reads as "load has no
effect on latency".

Upstream fixes: 3.18 (#1801, #1760, #1750, PR#1755), 3.19 (#1807), 3.21
(socket-close race, plus erroneous zero-loss reporting on lossy UDP tests —
which matters because the harness reads achieved rate from iperf3's JSON).
No Ubuntu suite ships 3.21; noble has no backport, and 26.04 tops out at
3.20. Hence `scripts/fix_iperf3.sh`, which builds 3.21 into `/usr/local`,
repoints the systemd unit, and runs a loopback UDP self-test. Both nodes now
run 3.21.

### 3.3 Source-address selection put the reply on the wrong address

With iperf3 fixed, background load *still* did not flow. The client failed
after exactly 30 s with `unable to read from stream socket: Resource
temporarily unavailable` while the server logged a healthy
`Server listening on 5201 (test #1)`. `tcpdump` on the talker:

```
enp2s0 Out IP 192.168.1.62.44668 > 192.168.1.72.5201: UDP, length 4
enp2s0 In  IP 192.168.1.71.5201 > 192.168.1.62.44668: UDP, length 4
               ^^^^^^^^^^^^ PC2's STREAM address, not .72
```

All four measurement addresses are one `192.168.1.0/24` across two
interfaces per node, so the kernel holds two equal-cost routes for that
prefix and breaks the tie by interface index. `ip route get 192.168.1.62` on
UP-2 returned `dev enp1s0 src 192.168.1.71`. iperf3's `iperf_udp_connect()`
waits on a socket *connected* to `.72` with a 30 s `SO_RCVTIMEO`; a datagram
from `.71` is discarded by the kernel before iperf3 sees it.

Two traps here, both of which cost time:

- The existing `arp_filter`/`arp_ignore`/`arp_announce` sysctls were applied
  and correct, and a comment in `setup_node.sh` claimed they kept the two
  port pairs separated. **They govern who answers ARP, not which source
  address a locally-originated packet carries.** That is a separate route
  lookup. The comment has been corrected.
- TCP is immune, because an accepted socket's addresses are fixed by the
  handshake. So the control connection worked, the server looked healthy,
  and every symptom pointed at iperf3.

Fixed with a `/32` host route carrying an explicit `src`, which wins on
longest-prefix match. `run_measurement.sh` installs **and verifies** it on
both nodes at the start of every campaign, because like the sysctls it is
runtime state lost on reboot. RUNBOOK §5.6 has the full account.

### 3.4 The preflight hid iperf3's own error

The first version of the background probe reported a bare `0 of 100 Mbit/s`.
In `--json` mode iperf3 puts its error in an `error` key on **stdout**, which
the parser was swallowing as "no throughput". It now surfaces the error
string, the process's stderr, and a checklist. Minor, but it delayed 3.3 by a
round trip.

---

## 4. Guards now in place

Everything below was added because something failed silently. Do not remove
any of it without understanding which failure it catches.

| Guard | Where | Catches |
|---|---|---|
| iperf3 ≥ 3.18 version gate, both nodes | `setup_node.sh`, `run_measurement.sh` | §3.2 |
| `scripts/fix_iperf3.sh` + loopback UDP self-test | new script | §3.2 |
| `/32` host route pinned and verified on both nodes | `run_measurement.sh` | §3.3 |
| Same-subnet warning; `--peer-bg-ip` to pin the route | `setup_node.sh` | §3.3 |
| 2 s live UDP probe before the campaign starts | `run_measurement.sh` | all of §3.2–3.4 |
| `background_mbps_achieved` in every `meta.json`; `LOAD_SHORTFALL` file below 80 % | `run_measurement.sh` | partial load |
| Flat-latency detector across loads | `analyze_plot.py` | §3.2 |
| `DEGRADED` marker below 90 % timestamp yield; one-shot ptp4l recovery | `run_measurement.sh` | §3.1 |
| Degraded / single-run / sampled-timestamping warnings | `analyze_plot.py` | misreading output |

Overrides exist (`ALLOW_OLD_IPERF3=1`, `SKIP_BG_PREFLIGHT=1`) and are
documented. They are for deliberate exceptions, not for getting past a
failure.

---

## 5. Current results — run `20260911-163017` (`--quick`, 1 rep, 5 s/point)

```
             series   load%  median_us  p99_us   max_us  loss%
IEEE 802.1p (PCP 6)       0     13.142  13.575   14.135    0.0
IEEE 802.1p (PCP 6)      50     13.249  25.516   39.838    0.0
IEEE 802.1p (PCP 6)     105     32.588  38.907   39.953    0.0
     No-QoS (PCP 0)       0     13.142  13.609   14.069    0.0
     No-QoS (PCP 0)      50     13.267  25.636   41.009    0.0
     No-QoS (PCP 0)     105     32.632  38.983   42.935    0.0
```

**The floor reproduces exactly.** 13.142 µs at 0 % load, identical to three
decimals across every campaign since the instrument was validated. Of that,
512 B × 8 ÷ 1 Gbit/s × 3 hops = **12.288 µs** is pure serialisation, leaving
**0.854 µs** for two switch fabrics.

**Queueing behaves as store-and-forward theory predicts.** Converting excess
delay over the floor into bytes in the bottleneck queue:

| point | queue @ median | queue @ p99 | queue @ max |
|---|---|---|---|
| 50 % | 13 B | **1547 B** | 3337 B |
| 105 % | **2431 B** | 3221 B | 3351 B |

One background frame is 1400 B payload ≈ 1470 B on the wire ≈ **11.76 µs** at
1 Gbit/s. The 50 % p99 is one measured frame arriving just after a background
frame began transmitting. At 105 % there is a standing backlog of ~2.4 kB and
the peak queue never exceeds ~3.7 kB — a very shallow buffer, and directly
usable for the queue-occupancy work in §6.4.

**802.1p shows no measurable effect.** At 105 % the median differs by
**0.044 µs** and the p99 by **0.076 µs**. Measured run-to-run drift on this
testbed reaches **11 µs**. The difference is indistinguishable from zero.

Note *which* point carries the evidence. At 50 % neither mode can differ:
strict priority is non-preemptive, so a PCP-6 frame still waits out the frame
already on the wire, and both modes must show ~25 µs p99. The 105 % point is
the discriminating one — a strict-priority scheduler would let PCP 6 jump the
19.4 µs standing queue, and it did not move at all.

---

## 6. Open work, ranked

### 6.1 Resolve the 802.1p null result — do this before any long campaign

The null is currently ambiguous between "the switch is not configured for
strict priority" and "802.1p is genuinely ineffective here". Three checks,
the first decisive:

1. **Confirm RUNBOOK §3 requirement 4 on both switches** — PCP → traffic
   class mapping with Strict Priority Queuing on the egress queue of the
   inter-switch link. The datasheet confirms the hardware supports 8 queues
   per port with SPQ and DWRR, so this is a configuration question. 802.1Q
   was toggled on the switch between earlier runs and the state during
   `20260911-163017` was never recorded — **record it explicitly next time**.
2. **Confirm frames still carry PCP 6 on arrival.** On UP-2 during the dot1p
   leg: `sudo timeout 20 tcpdump -nei enp1s0 -c 5 vlan and ether src 00:07:32:c1:43:30`.
   If an ingress port remaps or strips priority, the experiment is void.
3. **Read back the achieved background rate**:
   `grep -H background results/20260911-163017/*/meta.json`.

Do not spend 30 minutes on `REPETITIONS=3` before check 1 — it would only
reconfirm a null at higher confidence.

### 6.2 The oversubscription ceiling — an experimental design limit

A 1 Gbit/s bottleneck cannot be oversubscribed from a single 1 Gbit/s source
port. Background is capped at line rate, the stream adds 41 Mbit/s, so the
worst case is ~104 % — a knee, not a congested regime, with very little for a
priority scheduler to arbitrate. Zero loss at nominal "105 %" confirms it.

The I226 NICs are **2.5 GbE**; the 1 Gbit/s figure in the logs is the
*negotiated* link speed, which the latency floor independently corroborates.
Per the datasheet, the **KSwitch D10 MMT 8G** has "6x 10/100/1000Mbps RJ45
ports + 2x 10/100/1000/2500Mbps" — exactly two 2.5G copper ports per switch.
The 6G variant has none; 6G-2GS has 2.5G only on SFP.

If the user has the 8G variant, the right topology is **both PC access ports
at 2.5 Gbit/s, inter-switch link at 1 Gbit/s** — up to 2.5:1 oversubscription,
a queue that fills and stays full, real loss, and a regime where strict
priority has genuine work to do.

**Pending check** (asked of the user, not yet answered):

```bash
ethtool enp1s0 | sed -n '/Supported link modes/,/Link detected/p'
ethtool enp2s0 | sed -n '/Supported link modes/,/Link detected/p'
```

If `2500baseT/Full` appears under *Link partner advertised*, the port is
2.5G-capable and something is forcing 1G; if not, it is a re-patching job.

**Code change this implies, offered but not yet made:** `BG_LOADS` is
currently a percentage of the background *port's* link rate, auto-detected
from `/sys/class/net/<if>/speed`. With 2.5G access ports that is the wrong
denominator — the bottleneck is the 1 Gbit/s inter-switch link, so 40 % of
2500 already saturates it. Add an explicit `BOTTLENECK_MBPS` to
`config.conf` and compute loads against that.

### 6.3 Statistics — single runs are not enough, and that is measured

`REPETITIONS=3` is already the default in `config.conf`, and the analysis
computes across-run t intervals (`median_across_reps_us`, `median_ci95_us`,
`p99_across_reps_us`, `p99_ci95_us`) alongside the upstream-style CI.

The finding that motivated it: between-run drift reached **11.98 µs** at the
median, while within-run intervals are **±0.036 µs**. A spurious ~10 µs
"802.1p effect" once appeared at 50 % load in a run where 802.1Q was
*disabled* on the switch — i.e. where the mechanism could not act at all.
Within-run CIs see none of this because the variability is between runs, not
between frames. This is the central methodological result so far and belongs
in the dissertation.

Still open: the **reversed-order control**, `QOS_MODES="dot1p none"` with
`REPETITIONS=3`. If a difference follows the *position* in the sweep rather
than the mode, it is drift, not QoS.

### 6.4 Queue-occupancy measurement

Deferred earlier ("we go back to queue size measurement later") and now much
better supported, because §5 gives real delay distributions. The Kontron
datasheet publishes **no per-port buffer figure** — only "full
wire-speed/non-blocking Gigabit switchcore" — so a delay-derived estimate is
genuinely novel. Offered and not started: randomised/Poisson sampling
(PASTA) and an effective-sample-size diagnostic for autocorrelation.

### 6.5 Literature note

Offered, not started: a related-work note on published TSN switch buffer
sizes for the dissertation.

---

## 7. Traps a new agent will otherwise fall into

- **`sudo ./script.sh` → `command not found`** means the executable bit is
  missing, not the file. Scripts are committed mode `100755`; a checkout
  through Windows or a cloud-sync folder loses it. Do **not** `chmod +x`
  reflexively — that creates an uncommitted mode-only change and the next
  `git pull` aborts with "local changes would be overwritten". Check first;
  `git diff` showing only `old mode`/`new mode` confirms it.
- **`rm /tmp/rx_*.csv` on UP-2 fails with EPERM.** `tsn_rx` runs under sudo,
  `/tmp` is sticky, so the file needs `sudo rm`.
- **ptp4l rejects `gmCapable 0` combined with `clientOnly`/`slaveOnly`** and
  exits 255; `Restart=always` turns that into a crash loop whose visible
  symptom is "no gPTP lock". `gmCapable 0` alone is the 802.1AS way to say
  "never grandmaster". After fixing, `systemctl reset-failed` before restart.
- **ptp4l must start before `tsn_rx`.** ptp4l narrows the RX timestamp filter
  to PTP; `tsn_rx` widens it back to ALL. A ptp4l restart mid-run re-narrows
  it and `ts_src` silently becomes `sw`.
- **The strict-ARP sysctls and the `/32` routes are runtime-only and lost on
  reboot.** Re-run `setup_node.sh` after every boot. The campaign driver
  re-pins the routes itself; it does not re-apply the sysctls.
- **`ethtool -T` prints the RX filter as `all`, not
  `HWTSTAMP_FILTER_ALL`.** An earlier check grepped only the symbolic name
  and produced a false warning.
- **Don't trust a plausible number.** Every failure in §3 produced
  well-formed output. The components that fail loudly here are the ones that
  were instrumented; the ones that fail quietly are the ones that were
  assumed.

---

## 8. Security — outstanding

The SSH password for both PCs was **publicly exposed** in
`i226-adaptation/MISSION.md` on GitHub. It has been scrubbed from the
working tree but **remains in local git history (commit `a9bede0`)**.

**The password must be rotated on both PCs, and the pushed history checked.**
This is still open and should not be deferred further.

`bootstrap_ssh.sh` reads the password interactively or from `$SSHPASS` and
never writes it to disk or logs. Never place it in any file, and never enter
it into a form or field.

---

## 9. Immediate next action

Get the answer to the `ethtool` check in §6.2 and the switch-configuration
check in §6.1. Those two determine whether the next campaign is worth
running, and in which topology. Everything else waits on them.
