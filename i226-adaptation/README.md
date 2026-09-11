# FlexTest — TSN latency measurement on Intel I226

One-way latency and frame loss across a TSN network, measured NIC-to-NIC with
hardware timestamps, comparing a No-QoS baseline against IEEE 802.1p under
increasing background load.

An adaptation of [TSN-FlexTest](https://github.com/ivankl92/tsn-testbed) for
hardware the original cannot run on. Upstream measures latency by patching the
Intel **igb** driver to write the NIC's TX timestamp into the frame payload;
the I226 uses **igc**, so that patch does not apply. This implementation uses
the standard Linux `SO_TIMESTAMPING` API instead — **no kernel patch, no custom
kernel, no reboot.** No upstream code is used; see
`i226-adaptation/docs/REPORT.md` §7.

## Hardware

- 2 × Linux PC, two Intel I226 NICs each (reference: UP Squared Pro 7000 Edge)
- 2 × Kontron KSwitch D10 with gPTP enabled, one acting as grandmaster
- Stock Ubuntu kernel

```
PC1 enp1s0 ──► KSw1 p1 ┐                    ┌ KSw2 p1 ──► PC2 enp1s0   measured stream
                       ├── inter-switch ────┤
PC1 enp2s0 ──► KSw1 p2 ┘   link (shared)    └ KSw2 p2 ──► PC2 enp2s0   background load
```

Both port pairs cross the **same** inter-switch link, so background traffic
contends for the egress queue the measured stream must pass through. That
contention is the experiment.

## Quick start

Everything lives under `i226-adaptation/`; run from there.

```bash
git clone https://github.com/ivankl92/FlexTest.git
cd FlexTest/i226-adaptation

# 1. before anything else — confirm the NIC can timestamp at your target rate
gcc -O2 -o /tmp/tsn_tx tools/tsn_tx.c
sudo env TSN_TX_BIN=/tmp/tsn_tx ./scripts/tx_rate_selftest.sh enp1s0

# 1b. on BOTH nodes: iperf 3.16 (Ubuntu 24.04's version) segfaults on UDP, so
#     the background load silently never happens. Skip this and every load
#     point returns the unloaded floor with no error anywhere.
iperf3 -v                                     # 3.18+ required, 3.21 recommended
sudo ./scripts/fix_iperf3.sh                  # only if it is older

# 2. passwordless ssh + sudo to the listener (prompts for PC 2's password)
sudo ./scripts/bootstrap_ssh.sh <user>@<pc2-management-ip>

# 3. set up both nodes (listener first)
sudo ./scripts/setup_node.sh --stream-if enp1s0 --bg-if enp2s0 \
     --stream-ip 192.168.1.71/24 --bg-ip 192.168.1.72/24 --role listener

# 4. edit scripts/config.conf, then run
sudo ./scripts/run_measurement.sh --quick     # smoke test
sudo ./scripts/run_measurement.sh             # full campaign, ~10 min

# 5. statistics and figures
python3 analysis/analyze_plot.py results/<run-id>
```

Full procedure, switch requirements and troubleshooting:
**`i226-adaptation/docs/RUNBOOK.md`**.

## Layout

```
i226-adaptation/
  tools/      tsn_tx.c, tsn_rx.c — paced talker and listener, hardware timestamps
  scripts/    node setup, gPTP config, QoS, campaign driver, TX rate self-test,
              iperf3 repair
  analysis/   join, statistics, figures; plus a fixture generator for testing
  docs/       REPORT.md  (design, patching, deltas vs upstream, open items)
              RUNBOOK.md (operating manual)
  MISSION.md  standalone brief for an autonomous agent doing the setup
```

## How it measures

`tsn_tx` requests `SOF_TIMESTAMPING_TX_HARDWARE` and reads the NIC's real TX
timestamp back from the socket error queue; `tsn_rx` takes a hardware RX
timestamp on arrival. Frames carry a sequence number, so the two sides join
offline:

```
latency = rx_hw_ns (PC2 PHC) − tx_hw_ns (PC1 PHC)
```

Both values are raw PHC time and both PHCs are gPTP-disciplined to the switches,
so this is a true **one-way** NIC-to-NIC latency — not RTT/2, which would assume
a path symmetry this testbed deliberately breaks. Residual grandmaster offset
appears as a constant bias, not as jitter.

## Status

**The measurement path runs end to end on real hardware** as of 2026-09-09. A
`--quick` smoke campaign across two KSwitch D10 switches produced 4 points ×
100 000 frames at 10 000 fps, with a **hardware** timestamp on both ends of
every recorded frame (`ts_src=hw` throughout) and gPTP held within ±8 ns
before and after each point.

**That validates the mechanism, not the experiment.** No full campaign has been
run and no latency figure has been analysed or interpreted yet. Whether IEEE
802.1p actually changes anything on this hardware is still an open question —
it depends on the switches applying strict priority on PCP, which is unverified.
`i226-adaptation/docs/REPORT.md` §8 states exactly what has and has not been
exercised; read it before trusting any output.

`tx_rate_selftest.sh` separately returned **100 % hardware TX timestamp yield
from 200 to 20 000 fps** (kernel `6.8.1-1058-realtime`, `igc`, 512 B frames), so
a 100 µs period is supported and `STREAM_RATE` defaults to 10000.

`analysis/make_fixture.py` generates fabricated data for testing the analysis
code. It writes a `SYNTHETIC` marker that causes every figure to be watermarked
and `summary.csv` to carry `data_source=SYNTHETIC_FIXTURE`. Real campaigns are
never watermarked; fixtures always are.

## Citation

The method originates in the TSN-FlexTest papers — NetSoft 2022, and IEEE TNSM
21(2) 2024, [doi:10.1109/TNSM.2023.3327108](https://doi.org/10.1109/TNSM.2023.3327108).
Cite them in anything published from this work, and note the deviations listed
in `i226-adaptation/docs/RUNBOOK.md` §14.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
