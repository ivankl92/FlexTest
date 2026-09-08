#!/usr/bin/env python3
"""
make_fixture.py - generate FABRICATED measurement data for testing the analysis
pipeline.

    ####################################################################
    #  NOTHING THIS SCRIPT PRODUCES IS A MEASUREMENT.                  #
    #  Every number comes out of a random number generator whose       #
    #  parameters were chosen by hand. The "result" visible in the     #
    #  resulting plots is an ASSUMPTION, not an observation.           #
    ####################################################################

Why this exists
---------------
analyze_plot.py has to be exercised - CSV parsing, the sequence-number join,
percentile maths, figure rendering, label collisions - and that needs input of
the right *shape*. Shape is the only thing this script provides. It says nothing
about any NIC, switch, or network.

The queueing model below is invented. It was tuned so the No-QoS series climbs
and blows out under overload while the 802.1p series stays flat - which is the
outcome one *hopes* to measure. Never let that be mistaken for having measured
it. Whether your KSwitches actually behave this way is exactly the open question
the real campaign answers.

Safety interlock
----------------
This script writes a `SYNTHETIC` marker file into the output directory.
analyze_plot.py detects it and watermarks every figure, labels summary.md, and
adds a `data_source=SYNTHETIC_FIXTURE` column to summary.csv. run_measurement.sh
never writes that marker, so real campaigns are never watermarked and fixtures
always are.

Usage:
    python3 make_fixture.py /tmp/fixture-run
    python3 analyze_plot.py /tmp/fixture-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# --- the invented model ----------------------------------------------------
# All of these are guesses. None were measured.
FLOOR_NS = 12_000          # guessed floor: 512 B store-and-forwarded twice at 1 Gbit/s
JITTER_NS = 60             # guessed symmetric noise
LOADS = (0, 50, 80, 95, 105)
QOS_MODES = ("none", "dot1p")


def invented_queueing_delay(rng, qos: str, load: int, n: int) -> np.ndarray:
    """A hand-tuned queueing model. Not derived from any measurement."""
    if qos == "none":
        q = rng.exponential(scale=max(200.0, load * 38.0), size=n)
        if load >= 95:
            q += rng.gamma(2.0, load * 22.0, n)
        if load >= 105:
            q += (rng.random(n) < 0.35) * rng.exponential(load * 70.0, n)
    else:
        q = rng.exponential(scale=90.0 + load * 0.8, size=n)
    return q


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", type=Path, help="directory to create the fixture in")
    ap.add_argument("--rate", type=int, default=1000, help="frames per second (default 1000)")
    ap.add_argument("--duration", type=int, default=30, help="seconds per point (default 30)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    root: Path = args.outdir
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    n = args.rate * args.duration
    base_ns = 1_788_000_000_000_000_000

    print("!" * 78, file=sys.stderr)
    print("!! Generating SYNTHETIC FIXTURE DATA. These are not measurements.", file=sys.stderr)
    print("!" * 78, file=sys.stderr)

    for qos in QOS_MODES:
        for load in LOADS:
            d = root / f"{qos}_load{load}"
            d.mkdir(exist_ok=True)

            seq = np.arange(n)
            tx = base_ns + seq * (10 ** 9 // args.rate) + rng.integers(-800, 800, n)
            lat = FLOOR_NS + invented_queueing_delay(rng, qos, load, n) \
                + rng.normal(0, JITTER_NS, n)
            rx = tx + lat.astype(np.int64)

            # invented loss: baseline only, and only past the overload point
            keep = np.ones(n, bool)
            if qos == "none" and load >= 105:
                keep = rng.random(n) > 0.012

            with open(d / "tx.csv", "w") as f:
                f.write("seq,tx_hw_ns,sw_tx_ns,ts_src\n")
                for i in seq:
                    f.write(f"{i},{tx[i]},{tx[i] - 1500},hw\n")
            with open(d / "rx.csv", "w") as f:
                f.write("seq,rx_hw_ns,sw_tx_ns,ts_src\n")
                for i in seq[keep]:
                    f.write(f"{i},{rx[i]},{tx[i] - 1500},hw\n")

            json.dump({
                "run_id": root.name,
                "label": f"{qos}_load{load}",
                "qos_mode": qos,
                "pcp": 6 if qos == "dot1p" else 0,
                "vid": 0,
                "background_load_percent": load,
                "background_mbps": 10 * load,
                "link_speed_mbps": 1000,
                "stream_rate_pps": args.rate,
                "stream_frame_bytes": 512,
                "stream_duration_s": args.duration,
                "SYNTHETIC": True,
            }, open(d / "meta.json", "w"), indent=2)
            print(f"  {qos}_load{load}: {n} fabricated frames")

    (root / "SYNTHETIC").write_text(
        "SYNTHETIC FIXTURE DATA - NOT A MEASUREMENT\n"
        f"generated {datetime.now(timezone.utc).isoformat()} by make_fixture.py "
        f"(seed={args.seed}, rate={args.rate}pps, duration={args.duration}s)\n"
        "Every value here came from numpy's random number generator with "
        "hand-picked parameters.\n"
        "No network, NIC or switch was involved. Do not publish or cite.\n"
    )

    print(f"\nfixture -> {root}", file=sys.stderr)
    print("marker written: SYNTHETIC (analyze_plot.py will watermark all figures)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
