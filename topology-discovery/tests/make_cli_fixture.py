#!/usr/bin/env python3
"""Build a CLI raw-capture directory for testing the ISTAX parsers.

Two sources, and the distinction is kept visible in the output because it
matters:

* ``--from-transcript`` splits a **real** captured terminal session (paste a
  session into a file; `docs/config.txt` in the Kontron docs folder is one)
  into one file per command. These are genuine switch output and the
  strongest test material available without a live switch.

* The remaining commands, which no transcript happened to contain, are
  filled from the **worked examples in the vendor application notes**
  (Microchip AN1185 for TSN, AN1295 for PTP). Those are real output too, but
  from Microchip's reference hardware rather than this testbed -- a
  SparX-5i with different port names and a different `SupportedListMax`.
  Every such file is marked, and the run directory carries a `SYNTHETIC`
  marker, the same interlock as `analysis/make_fixture.py` upstream.

Never cite a fixture run as a property of the testbed.

Usage:
    python3 tests/make_cli_fixture.py --out results/cli-fixture \\
            --from-transcript /path/to/config.txt
    python3 -m tsn_discovery.cli --reanalyse results/cli-fixture \\
            --inventory ../SYSTEM.md --no-publish
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Dict

# Command text -> capture key, mirroring istax.GLOBAL_COMMANDS.
COMMAND_KEYS = {
    "show running-config": "running-config",
    "show running-config all-defaults": "running-config-all-defaults",
    "show interface * status": "interface-status",
    "show lldp neighbors": "lldp-neighbors",
    "show mac address-table": "mac-address-table",
    "show vlan": "vlan",
    "show tsn tas status": "tas-status",
    "show tsn frame-preemption status": "frame-preemption-status",
    "show ptp 0 default": "ptp-default",
    "show ptp 0 current": "ptp-current",
    "show ptp 0 parent": "ptp-parent",
    "show ptp 0 time-property": "ptp-time-property",
    "show ptp 0 port-state": "ptp-port-state",
    "show ptp 0 slave": "ptp-slave",
    "show tsn current-time": "tsn-current-time",
    "show tsn stream filter status": "psfp-status",
    "show tsn frer": "frer-status",
}

PROMPT_RE = re.compile(r"^([\w.\-]+)#\s*(.+?)\s*$")


# --- vendor-example fill-ins ---------------------------------------------
# Verbatim from the application notes, port names rewritten to this
# hardware's so the fixture exercises the name mapping. Marked SYNTHETIC.

TAS_STATUS = """\
interface GigabitEthernet 1/5
 GateEnabled           : TRUE
 OperGateStates        : 0x1f
 OperCycleTime         : 2 ms
 OperCycleTimeExtension: 9000 nanoseconds
 OperBaseTime          : 4300 seconds, 500 nanoseconds
 ConfigChangeTime      : 4300 seconds, 500 nanoseconds
 TickGranularity       : 0 tenths of nanoseconds
 CurrentTime           : 4311 seconds, 827669856 nanoseconds
 ConfigPending         : FALSE
 ConfigChangeError     : 0
 SupportedListMax      : 256
 OperControlListLength : 3
 GateControlEntry 0    : GateStates 0x80, TimeInterval 500000 nanoseconds,
GateOperation set-hold
 GateControlEntry 1    : GateStates 0x60, TimeInterval 500000 nanoseconds,
GateOperation set-release
 GateControlEntry 2    : GateStates 0x1f, TimeInterval 1000000 nanoseconds,
GateOperation set
"""

FP_STATUS = """\
interface GigabitEthernet 1/5
 HoldAdvance         : 1016 nanoseconds
 ReleaseAdvance      : 1016 nanoseconds
 PreemptionActive    : FALSE
 HoldRequest         : FALSE
 StatusVerify        : disabled
 LocPreemptSupported : TRUE
 LocPreemptEnabled   : TRUE
 LocPreemptActive    : FALSE
 LocAddFragSize      : 0 (64 octets)
"""

QOS = """\
qos queue-shaper queue 0: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 1: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 2: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 3: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 4: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 5: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
qos queue-shaper queue 6: enabled, rate 800 kbps, mode: line-rate, excess: disabled, credit: enabled
qos queue-shaper queue 7: disabled, rate 500 kbps, mode: line-rate, excess: disabled, credit: disabled
"""

PTP_DEFAULT = """\
ClockId  HW-Domain  DeviceType  Profile   2StepFlag  Ports  vtss_appl_clock_identity
-------  ---------  ----------  --------  ---------  -----  ------------------------
0        0          Ord-Bound   802.1as   True       8      00:80:82:ff:fe:b9:65:33

Dom  vtss_appl_clock_quality          Pri1  Pri2  Lpri
---  -------------------------------  ----  ----  ----
0    Cl:248 Ac:Unknwn Va:00000        128   128   128

Protocol         One-Way    VID    PCP  DSCP  PathTraceEnable
---------------  ---------  -----  ---  ----  ---------------
Ethernet         False      1      6    0     False
"""

PTP_CURRENT = """\
stpRm  OffsetFromMaster    MeanPathDelay
-----  ------------------  ------------------
1      -0.000,000,000,386  0.000,000,002,072
"""

PTP_PARENT = """\
ParentPortIdentity       port  Pstat  Var  ChangeRate
-----------------------  ----  -----  ---  ----------
00:80:82:ff:fe:bd:25:7c  4     False  0    -142

GrandmasterIdentity      GrandmasterClockQuality      Pri1  Pri2
-----------------------  ---------------------------  ----  ----
00:80:82:ff:fe:bd:25:7c  Cl:248 Ac:Unknwn Va:00000    128   10
"""

PTP_TIME_PROPERTY = """\
UtcOffset  Valid  leap59  leap61  TimeTrac  FreqTrac  ptpTimeScale  TimeSource
---------  -----  ------  ------  --------  --------  ------------  ----------
0          False  False   False   False     False     True          160
"""

PTP_PORT_STATE = """\
Port  Enabled  PTP-State  Internal  Link  Port-Timer  Vlan-forw  Phy-timestamper  Peer-delay
----  -------  ---------  --------  ----  ----------  ---------  ---------------  ----------
5     TRUE     slve       FALSE     Up    In Sync     Forward    FALSE            OK
1     TRUE     mstr       FALSE     Up    In Sync     Forward    FALSE            OK
"""

PTP_SLAVE = """\
Slave port  Slave state    Holdover(ppb)
----------  -------------  -------------
5           PHASE_LOCKED   290.4
"""

VENDOR_EXAMPLES: Dict[str, str] = {
    "tas-status": TAS_STATUS,
    "frame-preemption-status": FP_STATUS,
    "qos": QOS,
    "ptp-default": PTP_DEFAULT,
    "ptp-current": PTP_CURRENT,
    "ptp-parent": PTP_PARENT,
    "ptp-time-property": PTP_TIME_PROPERTY,
    "ptp-port-state": PTP_PORT_STATE,
    "ptp-slave": PTP_SLAVE,
}

VENDOR_COMMAND = {
    "tas-status": "show tsn tas status",
    "frame-preemption-status": "show tsn frame-preemption status",
    "qos": "show qos interface GigabitEthernet 1/5",
    "ptp-default": "show ptp 0 default",
    "ptp-current": "show ptp 0 current",
    "ptp-parent": "show ptp 0 parent",
    "ptp-time-property": "show ptp 0 time-property",
    "ptp-port-state": "show ptp 0 port-state",
    "ptp-slave": "show ptp 0 slave",
}


def split_transcript(path: str) -> Dict[str, tuple]:
    """Split a pasted terminal session into {key: (command, output)}."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")

    captured: Dict[str, tuple] = {}
    key = None
    command = None
    body: list = []

    def flush():
        if key and body:
            # Trailing blank lines and any stray prompt are not output.
            text = "\n".join(body).rstrip() + "\n"
            captured[key] = (command, text)

    for line in lines:
        m = PROMPT_RE.match(line)
        if m:
            flush()
            key, command, body = None, None, []
            cmd = " ".join(m.group(2).split())
            if cmd in COMMAND_KEYS:
                key, command = COMMAND_KEYS[cmd], cmd
            continue
        if key is not None:
            body.append(line)
    flush()
    return captured


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="results/cli-fixture")
    ap.add_argument("--switch", default="SW1")
    ap.add_argument("--from-transcript", default=None,
                    help="a pasted ISTAX terminal session to split")
    ap.add_argument("--no-vendor-examples", action="store_true",
                    help="only write what the transcript contained")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.out)
    raw = os.path.join(run_dir, "raw", args.switch)
    os.makedirs(raw, exist_ok=True)

    real: Dict[str, tuple] = {}
    if args.from_transcript:
        real = split_transcript(args.from_transcript)

    written = []
    for key, (command, text) in real.items():
        with open(os.path.join(raw, f"{key}.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"! command: {command}\n{text}")
        written.append((key, "real capture"))

    if not args.no_vendor_examples:
        for key, text in VENDOR_EXAMPLES.items():
            if key in real:
                continue
            with open(os.path.join(raw, f"{key}.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(f"! command: {VENDOR_COMMAND[key]}\n"
                         f"! SYNTHETIC: vendor application-note example, not "
                         f"this testbed\n{text}")
            written.append((key, "vendor example (SYNTHETIC)"))

    with open(os.path.join(run_dir, "SYNTHETIC"), "w", encoding="utf-8") as fh:
        fh.write(
            "This run directory mixes real captured switch output with vendor\n"
            "application-note examples. It is test material, not a measurement.\n"
            "Do not cite anything here as a property of the testbed.\n\n")
        for key, origin in sorted(written):
            fh.write(f"  {key:<28} {origin}\n")

    print(f"wrote {len(written)} captures to {raw}")
    for key, origin in sorted(written):
        print(f"  {key:<28} {origin}")
    print("\nAnalyse with:")
    print(f"  python3 -m tsn_discovery.cli --reanalyse {args.out} "
          "--inventory ../SYSTEM.md --no-publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
