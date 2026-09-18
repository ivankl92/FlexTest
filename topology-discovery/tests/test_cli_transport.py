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
Gi 1/3     Enabled  Auto    Yes        RJ45       N/A          Down
Gi 1/4     Enabled  Auto    Yes        RJ45       N/A          Down
Gi 1/5     Enabled  Auto    Yes        RJ45       N/A          1Gfdx
Gi 1/6     Enabled  Auto    Yes        RJ45       N/A          Down
2.5G 1/1   Enabled  Auto    Yes        SFP        None         Down
2.5G 1/2   Enabled  Auto    Yes        SFP        None         Down
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
 ptp 0 announce interval 0 timeout 3
 ptp 0 sync-interval -3
 ptp 0 delay-req interval 0
 ptp 0 gptp-interval 0
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
        self.assertEqual(len(rows), 8)
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
        """`show ptp 0 default` prints four tables in one output on
        GA-3.06: the clock, its quality, the protocol settings, and the
        gmCapable/sdoId pair."""
        tables = P.parse_tables(fix.PTP_DEFAULT)
        self.assertEqual(len(tables), 4)

    def test_pager_residue_row_is_recovered(self):
        """The firmware erases its `-- more --` prompt with a run of spaces
        and writes the next row on that same line. Slicing by column would
        read the displaced row as empty and drop it -- one FDB entry per
        paged command, silently."""
        clean = "Dynamic 1    00:bb:cc:dd:ee:12 GigabitEthernet 1/1"
        self.assertIn(clean, SAMPLE_MAC)
        displaced = SAMPLE_MAC.replace(clean, " " * 51 + clean, 1)
        self.assertEqual(P.parse_table(displaced), P.parse_table(SAMPLE_MAC))

    def test_right_aligned_value_is_not_mistaken_for_residue(self):
        """`show ptp 0 current` indents a genuine value 31 columns. The
        residue test is geometric, not a space count, so this survives."""
        rows = P.parse_tables(fix.PTP_CURRENT)[1]
        self.assertEqual(rows[0]["lastGMPhaseChange"], "0.000,000,000")

    def test_value_wider_than_its_dashes_is_not_truncated(self):
        """ParentPortIdentity is a 22-dash column holding a 23-character
        clock identity. Slicing to the dash width loses the last byte of
        every identity -- and two clock identities that differ only in the
        last byte then compare equal."""
        rows = P.parse_tables(fix.PTP_PARENT)[0]
        self.assertEqual(rows[0]["ParentPortIdentity"],
                         "00:80:82:ff:fe:b9:65:33")


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
                         ["2.5G 1/1", "2.5G 1/2", "Gi 1/1", "Gi 1/2",
                          "Gi 1/3", "Gi 1/4", "Gi 1/5", "Gi 1/6"])

    def test_port_numbers_follow_table_order_not_sorted_order(self):
        """The record is sorted by name for readability, but port numbers
        must come from the switch's own table order: Gi 1/1..1/6 = 1..6,
        then 2.5G 1/1..1/2 = 7..8. LLDP corroborates this by reporting Port
        ID 4 for GigabitEthernet 1/4."""
        by_name = {i["name"]: i["bridge_port"]["port_number"]
                   for i in self.ifaces}
        self.assertEqual(by_name["Gi 1/1"], 1)
        self.assertEqual(by_name["Gi 1/4"], 4)
        self.assertEqual(by_name["Gi 1/6"], 6)
        self.assertEqual(by_name["2.5G 1/1"], 7)
        self.assertEqual(by_name["2.5G 1/2"], 8)

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

    def test_mst_region_name_is_not_taken_for_the_bridge_address(self):
        """It is a region name that happens to look like a MAC. On four of
        the five switches in this testbed it is 00-22-33-44-55-66, which is
        not their address, so it is recorded as what it is and nothing else
        reads it as an address."""
        self.assertEqual(self.cfg["mst_region_name"], "00-80-82-b9-65-33")
        self.assertNotIn("bridge_address", self.cfg)

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
        """"-0.000,000,001,028" seconds is -1.028 ns."""
        self.assertAlmostEqual(
            self.record["sync_summary"]["offset_from_master_ns"], -1.028,
            places=6)

    def test_all_three_models_emitted(self):
        self.assertEqual(set(self.record["models"]),
                         {"ietf-ptp", "ieee1588-ptp-tt",
                          "ieee802-dot1as-gptp"})

    def test_each_model_names_its_module_and_reference(self):
        for key, model in self.record["models"].items():
            self.assertEqual(model["module"], key)
            self.assertTrue(model["reference"], key)
            self.assertTrue(model["namespace"].startswith("urn:"), key)

    # --- ietf-ptp (RFC 8575) --------------------------------------------
    def test_ietf_ptp_shape(self):
        inst = self.record["models"]["ietf-ptp"]["instance-list"][0]
        for key in ("default-ds", "current-ds", "parent-ds",
                    "time-properties-ds", "port-ds-list"):
            self.assertIn(key, inst, key)

    def test_ietf_ptp_uses_2008_terminology(self):
        cur = self.record["models"]["ietf-ptp"]["instance-list"][0]["current-ds"]
        self.assertIn("offset-from-master", cur)
        self.assertIn("mean-path-delay", cur)
        self.assertNotIn("offset-from-time-transmitter", cur)

    def test_ietf_ptp_port_states_keep_2008_role_names(self):
        ports = self.record["models"]["ietf-ptp"]["instance-list"][0]["port-ds-list"]
        states = {p["port-number"]: p["port-state"] for p in ports}
        self.assertEqual(states[4], "slave")
        self.assertEqual(states[5], "master")
        self.assertEqual(states[6], "listening")
        self.assertEqual(states[1], "disabled")

    def test_clock_quality_decomposed(self):
        ds = self.record["models"]["ietf-ptp"]["instance-list"][0]["default-ds"]
        self.assertEqual(ds["clock-quality"]["clock-class"], 248)

    # --- ieee1588-ptp-tt (IEEE 1588-2019) -------------------------------
    def test_ieee1588_uses_2019_terminology(self):
        inst = self.record["models"]["ieee1588-ptp-tt"]["instances"]["instance"][0]
        cur = inst["current-ds"]
        self.assertIn("offset-from-time-transmitter", cur)
        self.assertIn("mean-delay", cur)
        self.assertNotIn("offset-from-master", cur)

    def test_ieee1588_port_states_use_2019_role_names(self):
        inst = self.record["models"]["ieee1588-ptp-tt"]["instances"]["instance"][0]
        states = {p["port-number"]: p["port-ds"]["port-state"]
                  for p in inst["ports"]["port"]}
        self.assertEqual(states[4], "time-receiver")
        self.assertEqual(states[5], "time-transmitter")
        self.assertEqual(states[1], "disabled")

    def test_ieee1588_nests_ports(self):
        """1588-2019 uses ports/port/port-ds, not a flat port-ds-list."""
        inst = self.record["models"]["ieee1588-ptp-tt"]["instances"]["instance"][0]
        self.assertIn("ports", inst)
        self.assertNotIn("port-ds-list", inst)
        self.assertIn("port-ds", inst["ports"]["port"][0])

    def test_same_reading_under_both_names(self):
        """The renamed leaves must carry identical values."""
        ietf = self.record["models"]["ietf-ptp"]["instance-list"][0]["current-ds"]
        ieee = (self.record["models"]["ieee1588-ptp-tt"]
                ["instances"]["instance"][0]["current-ds"])
        self.assertEqual(ietf["offset-from-master"],
                         ieee["offset-from-time-transmitter"])
        self.assertEqual(ietf["mean-path-delay"], ieee["mean-delay"])

    # --- ieee802-dot1as-gptp (IEEE 802.1AS-2020) ------------------------
    def test_dot1as_is_augmentation_only(self):
        """802.1AS defines no top-level containers, so the projection must
        not pretend to carry the base datasets."""
        m = self.record["models"]["ieee802-dot1as-gptp"]
        self.assertIn("augments", m)
        self.assertNotIn("instance-list", m)
        self.assertNotIn("instances", m)
        self.assertIn("augments", m["path"])

    def test_dot1as_lifts_time_properties_into_default_ds(self):
        aug = self.record["models"]["ieee802-dot1as-gptp"]["augments"]
        self.assertIn("ptp-timescale", aug["default-ds"])
        self.assertIn("time-source", aug["default-ds"])

    def test_dot1as_profile_detected(self):
        m = self.record["models"]["ieee802-dot1as-gptp"]
        self.assertTrue(m["profile_active"])
        self.assertEqual(m["profile_reported"], "802.1as")

    def test_dot1as_port_augmentations(self):
        ports = self.record["models"]["ieee802-dot1as-gptp"]["augments"][
            "ports/port/port-ds"]
        by_num = {p["port-number"]: p for p in ports}
        self.assertEqual(by_num[4]["current-log-sync-interval"], -3)
        self.assertEqual(by_num[4]["gptp-port-role"], "Slave")

    def test_as_capable_is_read_not_guessed(self):
        """`show ptp 0 port-state` prints an `802.1AS port status` table with
        an as-cap column. An earlier version declared this leaf unobtainable
        and never parsed that table; the values below come from it."""
        ports = self.record["models"]["ieee802-dot1as-gptp"]["augments"][
            "ports/port/port-ds"]
        by_num = {p["port-number"]: p for p in ports}
        self.assertTrue(by_num[4]["as-capable"])
        self.assertTrue(by_num[5]["as-capable"])
        self.assertFalse(by_num[6]["as-capable"])
        self.assertFalse(by_num[1]["as-capable"])

    def test_is_measuring_delay_comes_from_its_own_column(self):
        """Not from Peer-delay OK/FAIL, which is link health and a different
        assertion. Port 1 is Peer-delay OK but is-mes-del False; reading the
        wrong column reports it as measuring."""
        ports = self.record["models"]["ieee802-dot1as-gptp"]["augments"][
            "ports/port/port-ds"]
        by_num = {p["port-number"]: p for p in ports}
        self.assertFalse(by_num[1]["is-measuring-delay"])
        self.assertEqual(by_num[1]["peer-delay-mechanism-status"], "OK")
        self.assertTrue(by_num[4]["is-measuring-delay"])

    def test_neighbor_rate_ratio_keeps_its_raw_integer(self):
        """The column is unlabelled, so the 2^41 wire scaling is an
        interpretation. It is applied, declared as derived, and the integer
        is kept so it can be checked."""
        ports = self.record["models"]["ieee802-dot1as-gptp"]["augments"][
            "ports/port/port-ds"]
        by_num = {p["port-number"]: p for p in ports}
        self.assertEqual(by_num[4]["neighbor-rate-ratio-scaled"], 241)
        self.assertAlmostEqual(by_num[4]["neighbor-rate-ratio"],
                               1.0 + 241 / float(1 << 41), places=15)
        m = self.record["models"]["ieee802-dot1as-gptp"]
        self.assertTrue(any("neighbor-rate-ratio" in d for d in m["derived"]))

    def test_every_model_declares_gaps_and_inferences(self):
        for key, model in self.record["models"].items():
            self.assertTrue(model["unavailable"], f"{key} claims no gaps")
            self.assertIsInstance(model["derived"], list, key)

    def test_gptp_interval_has_a_standard_home(self):
        """`ptp 0 gptp-interval` was previously stored under an invented
        name; 802.1AS calls it current-log-gptp-cap-interval."""
        ports = self.record["models"]["ieee802-dot1as-gptp"]["augments"][
            "ports/port/port-ds"]
        self.assertTrue(any("current-log-gptp-cap-interval" in p
                            for p in ports))

    def test_grandmaster_identity(self):
        """SW2's own clock is bd:25:7c and its grandmaster is b9:65:33
        (SW1), one step removed. The two must not be conflated -- that is
        what separates a synchronised switch from the reference itself."""
        self.assertEqual(self.record["sync_summary"]["grandmaster_identity"],
                         "00:80:82:ff:fe:b9:65:33")
        self.assertEqual(self.record["sync_summary"]["clock_identity"],
                         "00:80:82:ff:fe:bd:25:7c")
        self.assertFalse(self.record["sync_summary"]["is_grandmaster"])

    def test_lock_verdict_and_caveat(self):
        lock = ptp_mod.lock_assessment(self.record)
        self.assertEqual(lock["verdict"], "locked")
        self.assertTrue(lock["safe_to_schedule"])
        self.assertIn("one sample", lock["caveat"].lower())

    def test_grandmaster_is_not_reported_as_locked(self):
        """A grandmaster reports zero offset and a free-running servo
        because it is the reference, not because anything verified it.
        Calling that `locked` hands a CNC a check that never happened."""
        record = dict(self.record)
        summary = dict(record["sync_summary"])
        summary["is_grandmaster"] = True
        summary["clock_identity"] = "00:80:82:ff:fe:b9:65:33"
        summary["offset_from_master_ns"] = 0.0
        summary["clock_class"] = 248
        summary["time_source"] = 160
        record["sync_summary"] = summary

        lock = ptp_mod.lock_assessment(record)
        self.assertEqual(lock["verdict"], "grandmaster")
        self.assertTrue(lock["safe_to_schedule"])
        self.assertFalse(lock["traceable_to_external_reference"])
        self.assertIn("oscillator", lock["caveat"])

    def test_as_capable_false_on_the_syncing_port_is_a_contradiction(self):
        """A healthy offset read through a port that says it is not
        gPTP-capable is two readings that cannot both be right."""
        record = dict(self.record)
        summary = dict(record["sync_summary"])
        summary["port_states"] = {"Gi 1/4": "slave"}
        summary["as_capable"] = {"Gi 1/4": False}
        record["sync_summary"] = summary

        lock = ptp_mod.lock_assessment(record)
        self.assertEqual(lock["verdict"], "inconsistent")
        self.assertFalse(lock["safe_to_schedule"])

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


class TestPtpPortJoin(unittest.TestCase):
    """The port-number join between `show ptp 0 port-state` and
    `show interface * status` is the one place PTP state could be attached
    to the wrong interface. An interface table that does not account for a
    PTP port must be reported, not silently absorbed."""

    def test_unmatched_ptp_port_is_flagged(self):
        texts = dict(_full_result()._texts)
        texts["ptp-port-state"] = (
            "Port  Enabled  PTP-State  Internal  Link  Peer-delay\n"
            "----  -------  ---------  --------  ----  ----------\n"
            "5     TRUE     slve       FALSE     Up    OK\n"
            "99    TRUE     mstr       FALSE     Up    OK\n")
        result = _FakeResult(texts)
        cfg = P.parse_running_config(SAMPLE_RUNNING)
        ifaces = P.parse_interfaces(result)
        P.apply_interface_config(ifaces, cfg)
        record = ptp_mod.build(result, cfg, ifaces)

        self.assertTrue(any("99" in w for w in record["warnings"]))
        ports = record["models"]["ietf-ptp"]["instance-list"][0]["port-ds-list"]
        orphan = next(p for p in ports if p["port-number"] == 99)
        self.assertNotIn("underlying-interface", orphan)

    def test_matched_port_carries_its_interface(self):
        record = ptp_mod.build(
            _full_result(),
            P.parse_running_config(SAMPLE_RUNNING),
            _configured_interfaces())
        ports = record["models"]["ietf-ptp"]["instance-list"][0]["port-ds-list"]
        by_num = {p["port-number"]: p for p in ports}
        self.assertEqual(by_num[5]["underlying-interface"], "Gi 1/5")
        self.assertEqual(by_num[1]["underlying-interface"], "Gi 1/1")
        self.assertEqual(record["warnings"], [])


def _configured_interfaces():
    result = _full_result()
    cfg = P.parse_running_config(SAMPLE_RUNNING)
    ifaces = P.parse_interfaces(result)
    P.apply_interface_config(ifaces, cfg)
    return ifaces


# --- QoS -----------------------------------------------------------------

class TestQos(unittest.TestCase):
    """`show qos interface` carries the answer to the one question Qbv
    cannot answer for itself: which traffic class does a tagged frame land
    in. An earlier version read only the queue shapers and threw the rest
    away, which hid both facts asserted below."""

    def setUp(self):
        self.qos = P._parse_qos(fix.QOS)

    def test_ingress_trust_is_read(self):
        classification = self.qos["values"]["ingress-classification"]
        self.assertIn("trust_tag", classification)
        self.assertIn("default_cos", classification)

    def test_priority_regeneration_map_is_read(self):
        regen = self.qos["values"]["priority_regeneration"]
        self.assertEqual(len(regen), 8)
        for pcp, tc in regen.items():
            self.assertIn(int(pcp), range(8))
            self.assertIn(tc, range(8))

    def test_nothing_is_silently_discarded(self):
        """Every `qos` line lands in a named field or in `vendor`. A line
        that matches nothing is recorded under `unparsed` rather than
        dropped, so a firmware that adds output is noticed."""
        vendor = self.qos["values"].get("vendor", {})
        self.assertEqual(vendor.get("unparsed", []), [])


class TestQbuPriorityMapping(unittest.TestCase):
    """802.1Q's recommended default swaps PCP 0 and PCP 1, so "queue N is
    priority N" is wrong out of the box on this hardware."""

    NON_IDENTITY = {"0": 1, "1": 0, "2": 2, "3": 3,
                    "4": 4, "5": 5, "6": 6, "7": 7}

    def test_identity_map_maps_queue_n_to_priority_n(self):
        qbu = P._fp_status_to_qbu({}, [0, 1], {str(i): i for i in range(8)})
        self.assertEqual(qbu["configuration"]["preemptable_priorities"],
                         ["priority0", "priority1"])
        self.assertTrue(
            qbu["configuration"]["priority_regeneration_is_identity"])

    def test_swapped_map_is_applied_not_assumed(self):
        """Queues 2 and 3 are preemptable. Under the swapped map those are
        still priorities 2 and 3 -- but make queue 0 preemptable and the
        priority that reaches it is 1, not 0."""
        qbu = P._fp_status_to_qbu({}, [0], self.NON_IDENTITY)
        self.assertEqual(qbu["configuration"]["preemptable_priorities"],
                         ["priority1"])
        self.assertFalse(
            qbu["configuration"]["priority_regeneration_is_identity"])
        self.assertIn("NOT the identity", qbu["mapping_note"])

    def test_unknown_map_says_so_rather_than_assuming(self):
        qbu = P._fp_status_to_qbu({}, [0], None)
        self.assertIn("was not read", qbu["mapping_note"])


class TestTickGranularity(unittest.TestCase):
    def test_value_and_unit_share_the_field(self):
        self.assertEqual(P._tick_granularity("1 tenths of nanoseconds"), 1)

    def test_unexpected_unit_is_left_unread(self):
        """802.1Q's leaf is in tenths of a nanosecond. A firmware printing
        anything else would need rescaling, and a wrong scale here is worse
        than a null."""
        self.assertIsNone(P._tick_granularity("1 microseconds"))


class TestCreditBasedShaper(unittest.TestCase):
    def test_credit_enabled_shaper_is_recognised_as_cbs(self):
        """802.1Qav has no YANG module on this hardware, but a queue shaper
        with credit enabled is a credit-based shaper by another name, and a
        CNC reasoning about bandwidth reservation needs to know."""
        qos = P._parse_qos(fix.QOS_WITH_CBS)
        self.assertTrue(qos["credit_based_shaper_active"])

    def test_no_credit_shaper_makes_no_claim(self):
        self.assertNotIn("credit_based_shaper_active", P._parse_qos(fix.QOS))
