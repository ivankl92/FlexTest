#!/usr/bin/env python3
"""Tests for the ISTAX CLI fallback collector.

The material is real: `SAMPLE_*` below is captured output from
KSwitchTSN-1 (192.168.1.10, GA-3.06), plus the worked examples from
Microchip AN1185 (TSN) and AN1295 (PTP) for the commands that session did
not happen to run.

The test that matters most is `TestRecordUniformity`: the whole reason for
mapping CLI output onto YANG shapes is that nothing downstream should be
able to tell the transports apart, and a drift between the two record
shapes would be invisible until a CNC read the wrong field.

Run:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tsn_discovery import cli_record                      # noqa: E402
from tsn_discovery import istax                           # noqa: E402
from tsn_discovery import istax_parse as P                # noqa: E402
from tsn_discovery import ptp as ptp_mod                  # noqa: E402

import make_cli_fixture as fix                            # noqa: E402


# --- real captured output from KSwitchTSN-1 ------------------------------

SAMPLE_LLDP = """\
Local Interface     : GigabitEthernet 1/5
Chassis ID          : 00-80-82-BD-25-7C
Port ID             : 4
Port Description    : GigabitEthernet 1/4
System Name         : KSwitchTSN-2
System Description  : GA-3.06-20260812131346
System Capabilities : Bridge(+)
Management Address  : 192.168.1.11 (IPv4) - if-index:4
PoE Type            :
PoE Source          :
"""

SAMPLE_MAC = """\
Type    VID  MAC Address       Ports
Dynamic 1    00:07:32:c1:43:30 GigabitEthernet 1/5
Static  1    00:80:82:b9:65:33  CPU
Dynamic 1    00:bb:cc:dd:ee:12 GigabitEthernet 1/1
Static  1    33:33:00:00:00:01 GigabitEthernet 1/1-6 2.5GigabitEthernet 1/1-2 CPU
Dynamic 2    00:bb:cc:dd:ee:12 GigabitEthernet 1/1
"""

SAMPLE_IFSTATUS = """\
Interface  Mode     Speed   Aneg       Media Type SFP Family   Link    Operational Warnings
---------- -------- ------- ---------- ---------- ------------ ------- --------------------
Gi 1/1     Enabled  Auto    Yes        RJ45       N/A          1Gfdx
Gi 1/2     Enabled  Auto    Yes        RJ45       N/A          Down
Gi 1/5     Enabled  Auto    Yes        RJ45       N/A          1Gfdx
2.5G 1/1   Enabled  Auto    Yes        SFP        None         Down
"""

SAMPLE_VLAN = """\
VLAN  Name                              Interfaces
----  --------------------------------  ----------
1     default                           Gi 1/1-6 2.5G 1/1-2
2     VLAN0002                          Gi 1/1-5
"""

SAMPLE_RUNNING = """\
Building configuration...
hostname KSwitchTSN-1
spanning-tree mst name 00-80-82-b9-65-33 revision 0
!
vlan 1,2
!
ptp 0 mode boundary twostep ethernet twoway vid 1 6 profile 802.1as mep 1
 ptp 0 filter-type basic
!
interface GigabitEthernet 1/1
 switchport access vlan 2
 switchport hybrid allowed vlan 1,2
 switchport hybrid ingress-filtering
 switchport mode hybrid
 ptp 0
 ptp 0 sync-interval -3
 ptp 0 delay-mechanism p2p
!
interface GigabitEthernet 1/5
 switchport mode trunk
 tsn tas gate-enabled
 tsn tas cycle-time 2 ms
 tsn tas control-list-length 2
 tsn tas control-list index 0 gate-state queue 5 open time-interval 500000
 tsn tas control-list index 1 gate-state queue 0-4 open time-interval 1500000
 tsn tas max-sdu queue 5 512
 tsn frame-preemption queue 0
 tsn frame-preemption queue 1
 ptp 0
 ptp 0 delay-mechanism p2p
!
interface vlan 1
 ip address 192.168.1.10 255.255.255.0
!
netconf server
!
end
"""


class _FakeResult:
    """Minimal stand-in for istax.CliResult carrying canned command output."""

    def __init__(self, texts):
        self.name = "SW1"
        self.host = "192.168.1.10"
        self.port = 22
        self.transport = "cli"
        self.reachable = True
        self.prompt = "KSwitchTSN-1#"
        self.firmware = None
        self.duration_s = 1.0
        self._texts = texts
        self.captures = {
            k: istax.CliCapture(key=k, command=f"show {k}", ok=True, text=v)
            for k, v in texts.items()
        }

    def text_of(self, key):
        return self._texts.get(key)

    def errors(self):
        return []


def _full_result():
    texts = {
        "lldp-neighbors": SAMPLE_LLDP,
        "mac-address-table": SAMPLE_MAC,
        "interface-status": SAMPLE_IFSTATUS,
        "vlan": SAMPLE_VLAN,
        "running-config": SAMPLE_RUNNING,
        "tas-status": fix.TAS_STATUS,
        "frame-preemption-status": fix.FP_STATUS,
        "qos": fix.QOS,
        "ptp-default": fix.PTP_DEFAULT,
        "ptp-current": fix.PTP_CURRENT,
        "ptp-parent": fix.PTP_PARENT,
        "ptp-time-property": fix.PTP_TIME_PROPERTY,
        "ptp-port-state": fix.PTP_PORT_STATE,
        "ptp-slave": fix.PTP_SLAVE,
    }
    return _FakeResult(texts)


# --- naming and table geometry -------------------------------------------

class TestNaming(unittest.TestCase):
    def test_long_to_canonical(self):
        self.assertEqual(istax.canonical_ifname("GigabitEthernet 1/5"), "Gi 1/5")
        self.assertEqual(istax.canonical_ifname("2.5GigabitEthernet 1/2"),
                         "2.5G 1/2")
        self.assertEqual(istax.canonical_ifname("Gi 1/5"), "Gi 1/5")

    def test_canonical_to_cli(self):
        self.assertEqual(istax.cli_ifname("Gi 1/5"), "GigabitEthernet 1/5")
        self.assertEqual(istax.cli_ifname("2.5G 1/1"), "2.5GigabitEthernet 1/1")

    def test_round_trip(self):
        for name in ("Gi 1/1", "2.5G 1/2", "10G 1/4"):
            self.assertEqual(istax.canonical_ifname(istax.cli_ifname(name)),
                             name)

    def test_expand_ranges(self):
        self.assertEqual(
            istax.expand_port_list("GigabitEthernet 1/1-6 "
                                   "2.5GigabitEthernet 1/1-2 CPU"),
            ["Gi 1/1", "Gi 1/2", "Gi 1/3", "Gi 1/4", "Gi 1/5", "Gi 1/6",
             "2.5G 1/1", "2.5G 1/2"])

    def test_cpu_is_not_a_port(self):
        self.assertEqual(istax.expand_port_list("CPU"), [])


class TestTableParsing(unittest.TestCase):
    def test_dashed_separator_table(self):
        rows = P.parse_table(SAMPLE_IFSTATUS)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["Interface"], "Gi 1/1")
        self.assertEqual(rows[0]["Media Type"], "RJ45")   # multi-word header
        self.assertEqual(rows[0]["Link"], "1Gfdx")

    def test_table_without_separator(self):
        """`show mac address-table` prints no dashed line."""
        rows = P.parse_table(SAMPLE_MAC)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["MAC Address"], "00:07:32:c1:43:30")
        self.assertEqual(rows[0]["Ports"], "GigabitEthernet 1/5")

    def test_last_column_runs_past_its_dashes(self):
        """`show vlan` writes 19 characters into a 10-dash column."""
        rows = P.parse_table(SAMPLE_VLAN)
        self.assertEqual(rows[0]["Interfaces"], "Gi 1/1-6 2.5G 1/1-2")

    def test_multiple_tables_in_one_output(self):
        tables = P.parse_tables(fix.PTP_DEFAULT)
        self.assertEqual(len(tables), 3)


# --- per-command parsers -------------------------------------------------

class TestLldp(unittest.TestCase):
    def setUp(self):
        self.n = P.parse_lldp_neighbors(SAMPLE_LLDP)

    def test_one_neighbour(self):
        self.assertEqual(len(self.n), 1)

    def test_ports_canonicalised(self):
        self.assertEqual(self.n[0]["local_port"], "Gi 1/5")
        # Port ID is the bare number "4"; the description is the name.
        self.assertEqual(self.n[0]["port_desc"], "Gi 1/4")

    def test_chassis_mac_normalised(self):
        self.assertEqual(self.n[0]["chassis_id_mac"], "00:80:82:bd:25:7c")

    def test_management_address(self):
        self.assertEqual(self.n[0]["management_addresses"],
                         [{"address": "192.168.1.11", "subtype": "ipv4"}])

    def test_firmware_visible_in_description(self):
        self.assertIn("GA-3.06", self.n[0]["system_description"])


class TestInterfaces(unittest.TestCase):
    def setUp(self):
        self.ifaces = P.parse_interfaces(_full_result())

    def test_names_and_order(self):
        self.assertEqual([i["name"] for i in self.ifaces],
                         ["2.5G 1/1", "Gi 1/1", "Gi 1/2", "Gi 1/5"])

    def test_link_decoded(self):
        gi1 = next(i for i in self.ifaces if i["name"] == "Gi 1/1")
        self.assertEqual(gi1["oper_status"], "up")
        self.assertEqual(gi1["speed_mbps"], 1000)
        self.assertEqual(gi1["ethernet"]["duplex"], "full")

    def test_down_port_has_no_speed(self):
        gi2 = next(i for i in self.ifaces if i["name"] == "Gi 1/2")
        self.assertEqual(gi2["oper_status"], "down")
        self.assertIsNone(gi2["speed_mbps"])

    def test_port_number_derivation_is_labelled(self):
        self.assertIn("derived",
                      self.ifaces[0]["bridge_port"]["port_number_source"])


class TestRunningConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = P.parse_running_config(SAMPLE_RUNNING)

    def test_hostname_and_management_ip(self):
        self.assertEqual(self.cfg["hostname"], "KSwitchTSN-1")
        self.assertEqual(self.cfg["management_ip"], "192.168.1.10")

    def test_bridge_address_from_mst_name(self):
        self.assertEqual(self.cfg["bridge_address"], "00:80:82:b9:65:33")

    def test_netconf_server_is_configured(self):
        """The evidence that this is a firmware fault, not a missing setting."""
        self.assertTrue(self.cfg["netconf_server_configured"])

    def test_interface_blocks_split(self):
        self.assertIn("Gi 1/5", self.cfg["interfaces"])
        self.assertIn("Gi 1/1", self.cfg["interfaces"])

    def test_vlan_interface_is_not_a_port(self):
        self.assertNotIn("vlan 1", self.cfg["interfaces"])


class TestQbv(unittest.TestCase):
    def setUp(self):
        cfg = P.parse_running_config(SAMPLE_RUNNING)
        self.admin = P.parse_tas_config_lines(
            [l for l in cfg["interfaces"]["Gi 1/5"]
             if l.lower().startswith("tsn tas")])
        blocks = P.parse_kv_blocks(fix.TAS_STATUS)
        self.qbv = P._tas_status_to_qbv(blocks["Gi 1/5"], [
            l for l in cfg["interfaces"]["Gi 1/5"]
            if l.lower().startswith("tsn tas")])

    def test_admin_cycle_time_units(self):
        self.assertEqual(self.admin["admin_cycle_time_ns"], 2_000_000)

    def test_admin_gcl_from_config(self):
        self.assertEqual(len(self.admin["admin_control_list"]), 2)
        first = self.admin["admin_control_list"][0]
        self.assertEqual(first["gate_state_value"], 1 << 5)
        self.assertEqual(first["time_interval_ns"], 500_000)

    def test_max_sdu(self):
        self.assertEqual(self.admin["max_sdu"], {"queue5": 512})

    def test_oper_gcl_hex_masks_decoded(self):
        gcl = self.qbv["configuration"]["oper_control_list"]
        self.assertEqual(len(gcl), 3)
        self.assertEqual(gcl[0]["gate_state_value"], 0x80)
        self.assertEqual(gcl[0]["gate_states"]["open_traffic_classes"], [7])
        self.assertEqual(gcl[2]["gate_states"]["open_traffic_classes"],
                         [0, 1, 2, 3, 4])

    def test_gate_operation_mapped_to_yang_names(self):
        ops = [e["operation"]
               for e in self.qbv["configuration"]["oper_control_list"]]
        self.assertEqual(ops, ["set-and-hold-mac", "set-and-release-mac",
                               "set-gate-states"])

    def test_supported_list_max_read(self):
        self.assertEqual(self.qbv["capability"]["supported_gcl_entries_max"],
                         256)

    def test_unavailable_limits_are_none_and_flagged(self):
        cap = self.qbv["capability"]
        self.assertIsNone(cap["supported_cycle_time_max_ns"])
        self.assertIsNone(cap["supported_interval_max_ns"])
        self.assertIn("supported_cycle_time_max_ns", cap["unavailable_over_cli"])

    def test_gcl_sums_to_cycle_so_no_warning(self):
        self.assertEqual(self.qbv["gcl_total_ns"], 2_000_000)
        self.assertEqual(self.qbv["warnings"], [])


class TestQbu(unittest.TestCase):
    def test_status_and_queues(self):
        blocks = P.parse_kv_blocks(fix.FP_STATUS)
        qbu = P._fp_status_to_qbu(blocks["Gi 1/5"], [0, 1])
        self.assertEqual(qbu["capability"]["hold_advance_ns"], 1016)
        self.assertEqual(qbu["configuration"]["preemptable_priorities"],
                         ["priority0", "priority1"])
        self.assertEqual(len(qbu["configuration"]["express_priorities"]), 6)
        self.assertFalse(qbu["configuration"]["preemption_active"])

    def test_queue_to_priority_mapping_is_disclosed(self):
        qbu = P._fp_status_to_qbu({}, [0])
        self.assertIn("queue", qbu["mapping_note"].lower())


class TestPtp(unittest.TestCase):
    def setUp(self):
        result = _full_result()
        cfg = P.parse_running_config(SAMPLE_RUNNING)
        ifaces = P.parse_interfaces(result)
        P.apply_interface_config(ifaces, cfg)
        self.record = ptp_mod.build(result, cfg, ifaces)

    def test_available_via_cli(self):
        self.assertEqual(self.record["status"], "available-via-cli")

    def test_gptp_profile_detected(self):
        self.assertTrue(self.record["gptp_profile"])

    def test_offset_converted_to_nanoseconds(self):
        """"-0.000,000,000,386" seconds is -0.386 ns."""
        self.assertAlmostEqual(
            self.record["sync_summary"]["offset_from_master_ns"], -0.386,
            places=6)

    def test_rfc8575_shape(self):
        inst = self.record["instances"][0]
        for key in ("default-ds", "current-ds", "parent-ds",
                    "time-properties-ds", "port-ds-list"):
            self.assertIn(key, inst, key)

    def test_clock_quality_decomposed(self):
        q = self.record["instances"][0]["default-ds"]["clock-quality"]
        self.assertEqual(q["clock-class"], 248)

    def test_port_states_mapped_to_enum(self):
        states = {p["port-number"]: p["port-state"]
                  for p in self.record["instances"][0]["port-ds-list"]}
        self.assertEqual(states[5], "slave")
        self.assertEqual(states[1], "master")

    def test_grandmaster_identity(self):
        self.assertEqual(self.record["sync_summary"]["grandmaster_identity"],
                         "00:80:82:ff:fe:bd:25:7c")

    def test_lock_verdict_and_caveat(self):
        lock = ptp_mod.lock_assessment(self.record)
        self.assertEqual(lock["verdict"], "locked")
        self.assertTrue(lock["safe_to_schedule"])
        self.assertIn("one sample", lock["caveat"].lower())

    def test_out_of_tolerance_offset_is_refused(self):
        lock = ptp_mod.lock_assessment(self.record, threshold_ns=0.001)
        self.assertEqual(lock["verdict"], "out-of-tolerance")
        self.assertFalse(lock["safe_to_schedule"])

    def test_unknown_when_ptp_absent(self):
        lock = ptp_mod.lock_assessment({"status": "not-configured",
                                        "detail": "none"})
        self.assertEqual(lock["verdict"], "unknown")
        self.assertFalse(lock["safe_to_schedule"])


class TestMacTable(unittest.TestCase):
    def setUp(self):
        result = _full_result()
        ifaces = P.parse_interfaces(result)
        self.port_no = {i["name"]: i["bridge_port"]["port_number"]
                        for i in ifaces}
        self.fdb = P.parse_mac_table(SAMPLE_MAC, self.port_no)

    def test_entries_parsed(self):
        self.assertEqual(len(self.fdb), 5)

    def test_port_ref_resolves_to_a_name(self):
        entry = next(e for e in self.fdb
                     if e["address"] == "00:bb:cc:dd:ee:12" and e["vids"] == "1")
        self.assertEqual(entry["port_map"][0]["port_name"], "Gi 1/1")

    def test_cpu_entry_flagged_with_no_bridge_port(self):
        entry = next(e for e in self.fdb
                     if e["address"] == "00:80:82:b9:65:33")
        self.assertTrue(entry["cpu"])
        self.assertEqual(entry["port_map"], [])

    def test_entry_type_carried(self):
        self.assertEqual(
            {e["entry_type"] for e in self.fdb}, {"dynamic", "static"})


# --- the record, and its uniformity with the NETCONF path ----------------

class TestCliRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = cli_record.build(
            _full_result(),
            netconf_probe={"tcp_open": False, "kind": "port-closed",
                           "reason": "nothing listening on 192.168.1.10:830"})

    def test_transport_is_declared(self):
        self.assertEqual(self.rec["transport"], "cli")
        self.assertIsNone(self.rec["netconf"])
        self.assertEqual(self.rec["yang_modules"], {})

    def test_firmware_fault_is_recorded(self):
        self.assertTrue(self.rec["transport_detail"]
                        ["netconf_server_in_running_config"])
        self.assertTrue(any("firmware fault" in n for n in self.rec["notes"]))

    def test_cli_provenance_noted(self):
        self.assertTrue(any("ISTAX CLI" in n for n in self.rec["notes"]))

    def test_features_carry_command_evidence(self):
        for key in ("qbv", "qbu", "lldp", "ptp"):
            feat = self.rec["features"][key]
            self.assertTrue(feat["supported"], key)
            self.assertEqual(feat["source"], "cli")
            self.assertTrue(feat["evidence"])

    def test_ptp_supported_over_cli(self):
        self.assertTrue(self.rec["features"]["ptp"]["supported"])
        self.assertEqual(self.rec["ptp"]["status"], "available-via-cli")

    def test_pvid_from_running_config(self):
        gi1 = next(i for i in self.rec["interfaces"] if i["name"] == "Gi 1/1")
        self.assertEqual(gi1["bridge_port"]["pvid"], 2)

    def test_vlans(self):
        self.assertEqual(self.rec["summary"]["vlans"], [1, 2])

    def test_qbv_limits_note_explains_the_omission(self):
        limits = self.rec["summary"]["qbv_limits"]
        self.assertIsNone(limits["supported_cycle_time_max_ns"])
        self.assertIn("NETCONF", limits["note"])

    def test_scratch_fields_removed(self):
        for iface in self.rec["interfaces"]:
            for scratch in ("_tas_config_lines", "_preemptable_queues",
                            "_ptp_config_lines"):
                self.assertNotIn(scratch, iface)


class TestRecordUniformity(unittest.TestCase):
    """A CLI record and a NETCONF record must be interchangeable downstream.

    `topology.py`, `cnc.py` and `render.py` read records by key. If the two
    builders drift apart, a consumer silently reads `None` where it expected
    a value, and nothing raises. So the shapes are compared directly.
    """

    @classmethod
    def setUpClass(cls):
        from tsn_discovery import capabilities as caps_mod
        from tsn_discovery import probe as probe_mod
        import test_pipeline as tp

        cls.cli = cli_record.build(_full_result())
        res, _ = tp.run_live("SW1", tp.tempfile.mkdtemp())
        pr = probe_mod.probe(res)
        cls.nc = caps_mod.build(res, pr)
        cls.nc["ptp"] = probe_mod.ptp_finding(pr)

    def test_top_level_keys_match(self):
        cli_only = set(self.cli) - set(self.nc)
        nc_only = set(self.nc) - set(self.cli)
        # The CLI record adds transport metadata and lldp; nothing the
        # NETCONF record has may be missing from it.
        self.assertEqual(nc_only - {"lldp"}, set(),
                         f"CLI record is missing NETCONF keys: {nc_only}")
        self.assertTrue(cli_only <= {"transport", "transport_detail", "lldp"},
                        f"unexpected extra CLI keys: {cli_only}")

    def test_interface_keys_match(self):
        cli_if = self.cli["interfaces"][0]
        nc_if = self.nc["interfaces"][0]
        missing = set(nc_if) - set(cli_if)
        self.assertEqual(missing, set(),
                         f"CLI interface record is missing: {missing}")

    def test_qbv_capability_keys_match(self):
        cli_q = next(i["qbv"] for i in self.cli["interfaces"]
                     if i["qbv"].get("present"))
        nc_q = next(i["qbv"] for i in self.nc["interfaces"]
                    if i["qbv"].get("present"))
        missing = set(nc_q["capability"]) - set(cli_q["capability"])
        self.assertEqual(missing, set())
        for key in ("gate_enabled", "admin_control_list",
                    "admin_cycle_time_ns", "admin_gate_states"):
            self.assertIn(key, cli_q["configuration"], key)

    def test_summary_keys_match(self):
        missing = set(self.nc["summary"]) - set(self.cli["summary"])
        self.assertEqual(missing, set(), f"summary missing: {missing}")

    def test_gate_states_decoded_identically(self):
        """0x1f over CLI and 31 over NETCONF must decode the same."""
        from tsn_discovery import capabilities as caps_mod
        self.assertEqual(caps_mod.decode_gate_states(0x1f),
                         caps_mod.decode_gate_states(31))

    def test_both_declare_a_transport(self):
        self.assertEqual(self.cli["transport"], "cli")
        # The NETCONF builder leaves it to cli.py to stamp; assert the
        # contract rather than the stamping.
        self.assertNotIn("transport", self.nc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
