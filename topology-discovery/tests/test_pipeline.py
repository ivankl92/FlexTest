#!/usr/bin/env python3
"""Offline tests for the discovery pipeline.

The switches are on someone else's bench, so the live code path -- session
setup, subtree-filter construction, raw capture, error handling -- is
exercised here against a stand-in NETCONF server that replays the synthetic
fixture. That covers everything except the SSH transport itself.

Run:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tsn_discovery import capabilities as caps_mod        # noqa: E402
from tsn_discovery import cnc as cnc_mod                  # noqa: E402
from tsn_discovery import netconf as nc                   # noqa: E402
from tsn_discovery import probe as probe_mod              # noqa: E402
from tsn_discovery import topology as topo_mod            # noqa: E402
from tsn_discovery import xmlutil as X                    # noqa: E402
from tsn_discovery.inventory import parse_system_md       # noqa: E402

import make_fixture                                        # noqa: E402

SYSTEM_MD = """\
# SYSTEM
### Addresses

        IPv4            MAC
UP-1    192.168.1.61    00:07:32:C1:43:30
        192.168.1.62    00:07:32:C1:43:31
RPI1    192.168.1.51    88:a2:9e:4b:97:1b
SW1     192.168.1.10    00:80:82:b9:65:33
SW2     192.168.1.11    00:80:82:bd:25:7c
CTRL    192.168.1.120   00-BB-CC-DD-EE-12
"""


# --------------------------------------------------------------- helpers

class FakeReply:
    def __init__(self, xml):
        self.xml = xml


class FakeManager:
    """Stand-in for ncclient's manager, replaying fixture XML.

    Records every filter it is given so the test can assert that the
    filters the tool builds are well-formed and namespaced correctly.
    """

    def __init__(self, switch, fail_keys=()):
        self.switch = switch
        self.fail_keys = set(fail_keys)
        self.session_id = "42"
        self.filters = []
        self.closed = False
        self.server_capabilities = make_fixture.hello_caps()

    def _payload_for(self, filter_xml):
        if filter_xml is None:
            return make_fixture.wrap("<data/>")
        root = X.parse(filter_xml)
        top = X.lname(root)
        mapping = {
            "system": make_fixture.system_xml,
            "interfaces": make_fixture.interfaces_xml,
            "bridges": make_fixture.bridges_xml,
            "lldp": make_fixture.lldp_xml,
        }
        if top in self.fail_keys:
            raise nc.RPCError_stub(f"unknown element {top}")
        if top == "netconf-state":
            return make_fixture.wrap(make_fixture.schemas_xml())
        if top in mapping:
            return make_fixture.wrap(mapping[top](self.switch))
        # an unimplemented model: NETCONF answers with an empty <data/>
        return make_fixture.wrap("")

    def get(self, filter=None):                       # noqa: A002
        self.filters.append(("get", filter))
        return FakeReply(self._payload_for(filter[1] if filter else None))

    def get_config(self, source="running", filter=None):   # noqa: A002
        self.filters.append(("get-config", filter))
        return FakeReply(self._payload_for(filter[1] if filter else None))

    def close_session(self):
        self.closed = True


class RPCErrorStub(Exception):
    pass


nc.RPCError_stub = RPCErrorStub


def run_live(switch, raw_dir, fail_keys=(), connect_exc=None):
    """Drive SwitchSession through its live path with a fake transport."""
    session = nc.SwitchSession(
        name=switch, host=make_fixture.SWITCHES[switch]["ip"],
        raw_dir=raw_dir)
    fake = FakeManager(switch, fail_keys=fail_keys)

    def _connect(**kwargs):
        if connect_exc:
            raise connect_exc
        return fake

    with mock.patch.object(nc, "manager") as m, \
            mock.patch.object(nc, "NCCLIENT_AVAILABLE", True), \
            mock.patch.object(nc, "RPCError", RPCErrorStub):
        m.connect.side_effect = _connect
        session.connect()
        nc.collect(session)
        session.close()
    return session.result, fake


# --------------------------------------------------------------- tests

class TestXmlUtil(unittest.TestCase):
    def test_mac_normalisation(self):
        for spelling in ("00-A0-A5-5C-6D-B0", "00:a0:a5:5c:6d:b0",
                         "00a0a55c6db0", "00 A0 A5 5C 6D B0"):
            self.assertEqual(X.norm_mac(spelling), "00:a0:a5:5c:6d:b0")
        self.assertIsNone(X.norm_mac("not-a-mac"))
        self.assertIsNone(X.norm_mac(None))
        self.assertIsNone(X.norm_mac("00:a0:a5:5c:6d"))     # too short

    def test_identity_prefix_stripped(self):
        self.assertEqual(X.strip_identity("dot1q:c-vlan-component"),
                         "c-vlan-component")
        self.assertEqual(X.strip_identity("plain"), "plain")

    def test_local_name_matching_ignores_namespace(self):
        root = X.parse('<a xmlns="urn:x"><b xmlns="urn:y">1</b></a>')
        self.assertEqual(X.text(root, "b"), "1")


class TestGateDecoding(unittest.TestCase):
    def test_all_gates_open(self):
        d = caps_mod.decode_gate_states(255)
        self.assertEqual(d["open_traffic_classes"], list(range(8)))
        self.assertEqual(d["binary"], "11111111")

    def test_single_gate(self):
        d = caps_mod.decode_gate_states(32)            # bit 5
        self.assertEqual(d["open_traffic_classes"], [5])

    def test_multiple_gates(self):
        d = caps_mod.decode_gate_states(131)           # bits 0,1,7
        self.assertEqual(d["open_traffic_classes"], [0, 1, 7])

    def test_all_closed(self):
        self.assertEqual(caps_mod.decode_gate_states(0)["open_traffic_classes"],
                         [])


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "SYSTEM.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(SYSTEM_MD)
        self.inv = parse_system_md(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_devices_parsed(self):
        self.assertEqual(len(self.inv.devices), 5)
        self.assertEqual(len(self.inv.switches), 2)

    def test_continuation_line_attaches_to_previous_device(self):
        up1 = self.inv.by_name("UP-1")
        self.assertEqual(len(up1.interfaces), 2)
        self.assertEqual(up1.interfaces[1].ipv4, "192.168.1.62")

    def test_dashed_mac_normalised(self):
        self.assertEqual(self.inv.by_name("CTRL").macs, ["00:bb:cc:dd:ee:12"])

    def test_lookup_by_mac_and_ip(self):
        self.assertEqual(self.inv.by_mac("00-07-32-C1-43-31").name, "UP-1")
        self.assertEqual(self.inv.by_ip("192.168.1.51").name, "RPI1")


class TestLiveSessionPath(unittest.TestCase):
    """The path that runs against real hardware, with a fake transport."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_collect_captures_every_subtree(self):
        result, fake = run_live("SW1", self.dir)
        self.assertTrue(result.reachable)
        for key in ("system", "interfaces", "bridges", "lldp",
                    "netconf-state-schemas"):
            self.assertIn(key, result.captures, key)
            self.assertTrue(result.captures[key].ok, key)
        self.assertTrue(fake.closed, "session must be closed")

    def test_every_filter_is_wellformed_and_namespaced(self):
        _, fake = run_live("SW1", self.dir)
        self.assertTrue(fake.filters)
        for kind, flt in fake.filters:
            if flt is None:
                continue
            self.assertEqual(flt[0], "subtree")
            root = X.parse(flt[1])           # raises if malformed
            self.assertTrue(X.namespace(root),
                            f"filter {flt[1]} carries no namespace")

    def test_raw_xml_written_to_disk(self):
        run_live("SW1", self.dir)
        for name in ("system.xml", "interfaces.xml", "bridges.xml",
                     "lldp.xml", "hello-capabilities.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, name)), name)

    def test_rpc_error_is_recorded_not_raised(self):
        result, _ = run_live("SW1", self.dir, fail_keys={"lldp"})
        self.assertTrue(result.reachable)
        self.assertFalse(result.captures["lldp"].ok)
        self.assertEqual(result.captures["lldp"].error_kind, "rpc-error")
        # the rest of the collection still completed
        self.assertTrue(result.captures["bridges"].ok)

    def test_connect_failure_is_classified(self):
        result, _ = run_live("SW1", self.dir,
                             connect_exc=OSError("Connection refused"))
        self.assertFalse(result.reachable)
        self.assertEqual(result.connect_error_kind, "connection-refused")
        self.assertEqual(len(result.errors()), 1)

    def test_timeout_is_classified(self):
        result, _ = run_live("SW1", self.dir,
                             connect_exc=OSError("operation timed out"))
        self.assertEqual(result.connect_error_kind, "timeout")


class TestExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.results = {}
        for sw in make_fixture.SWITCHES:
            raw = os.path.join(cls.dir, sw)
            res, _ = run_live(sw, raw)
            cls.results[sw] = res

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def record(self, sw="SW1"):
        res = self.results[sw]
        pr = probe_mod.probe(res)
        rec = caps_mod.build(res, pr)
        rec["ptp"] = probe_mod.ptp_finding(pr)
        return rec

    def test_module_probe_finds_documented_modules(self):
        rec = self.record()
        for module in ("ietf-interfaces", "ieee802-dot1q-bridge",
                       "ieee802-dot1q-sched", "ieee802-dot1q-preemption",
                       "ieee802-dot1ab-lldp"):
            self.assertIn(module, rec["yang_modules"], module)

    def test_ptp_reported_absent_with_evidence(self):
        rec = self.record()
        self.assertEqual(rec["ptp"]["status"], "not-exposed-via-netconf")
        self.assertIn("no module matching", rec["ptp"]["evidence"])
        self.assertTrue(rec["ptp"]["consequence"])

    def test_qbv_capability_envelope(self):
        rec = self.record()
        port = next(i for i in rec["interfaces"] if i["name"] == "2.5G 1/1")
        cap = port["qbv"]["capability"]
        self.assertEqual(cap["supported_gcl_entries_max"], 128)
        self.assertEqual(cap["supported_cycle_time_max_ns"], 33538048)
        self.assertEqual(cap["supported_interval_max_ns"], 33538048)

    def test_qbv_gate_control_list_parsed(self):
        rec = self.record()
        port = next(i for i in rec["interfaces"] if i["name"] == "2.5G 1/1")
        cfg = port["qbv"]["configuration"]
        self.assertTrue(cfg["gate_enabled"])
        self.assertEqual(len(cfg["admin_control_list"]), 8)
        self.assertEqual(cfg["admin_cycle_time_ns"], 2_000_000)
        first = cfg["admin_control_list"][0]
        self.assertEqual(first["operation"], "set-gate-states")
        self.assertEqual(first["time_interval_ns"], 300_000)
        self.assertEqual(first["gate_states"]["open_traffic_classes"], [5])

    def test_gcl_sums_to_cycle_time(self):
        rec = self.record()
        port = next(i for i in rec["interfaces"] if i["name"] == "2.5G 1/1")
        self.assertEqual(port["qbv"]["gcl_total_ns"],
                         port["qbv"]["configuration"]["admin_cycle_time_ns"])
        self.assertEqual(port["qbv"]["warnings"], [])

    def test_qbu_status_table(self):
        rec = self.record()
        port = next(i for i in rec["interfaces"] if i["name"] == "Gi 1/1")
        cfg = port["qbu"]["configuration"]
        self.assertEqual(cfg["preemptable_priorities"],
                         ["priority0", "priority1", "priority2"])
        self.assertEqual(len(cfg["express_priorities"]), 5)
        self.assertEqual(port["qbu"]["capability"]["hold_advance_ns"], 12000)

    def test_vlans_and_bridge(self):
        rec = self.record()
        self.assertEqual(rec["summary"]["vlans"], [1, 100])
        bridge = rec["bridges"][0]
        self.assertEqual(bridge["address"], "00:80:82:b9:65:33")
        self.assertEqual(bridge["ports"], 8)

    def test_disabled_gate_default_is_a_note_not_a_warning(self):
        """The factory default cycle time exceeds supported-cycle-max, but
        only matters where the gate is on."""
        rec = self.record()
        self.assertEqual(rec["warnings"], [])
        self.assertTrue(any("supported-cycle-max" in n for n in rec["notes"]))


class TestTopology(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.inv_path = os.path.join(cls.dir, "SYSTEM.md")
        with open(cls.inv_path, "w", encoding="utf-8") as fh:
            fh.write("### Addresses\n")
            for name, d in make_fixture.SWITCHES.items():
                fh.write(f"{name}\t{d['ip']}\t{d['mac']}\n")
            fh.write("UP-1\t192.168.1.61\t00:07:32:C1:43:30\n")
            fh.write("\t192.168.1.62\t00:07:32:C1:43:31\n")
            fh.write("UP-2\t192.168.1.71\t00:07:32:C1:29:69\n")
            fh.write("\t192.168.1.72\t00:07:32:C1:29:6A\n")
            fh.write("RPI1\t192.168.1.51\t88:a2:9e:4b:97:1b\n")
            fh.write("RPI2\t192.168.1.52\t88:a2:9e:a6:d1:5f\n")
            fh.write("RPI3\t192.168.1.53\t88:a2:9e:a6:cb:86\n")
            fh.write("CTRL\t192.168.1.120\t00-BB-CC-DD-EE-12\n")
            fh.write("IO-D0\t192.168.1.130\t00-BB-CC-DD-EE-13\n")
        cls.inv = parse_system_md(cls.inv_path)

        cls.records, cls.lldp = {}, {}
        for sw in make_fixture.SWITCHES:
            res, _ = run_live(sw, os.path.join(cls.dir, sw))
            pr = probe_mod.probe(res)
            rec = caps_mod.build(res, pr)
            rec["ptp"] = probe_mod.ptp_finding(pr)
            cls.records[sw] = rec
            nbrs, _local = topo_mod.parse_lldp(res.xml_of("lldp"))
            cls.lldp[sw] = nbrs
        cls.topo = topo_mod.TopologyBuilder(cls.inv, cls.records,
                                            cls.lldp).build()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_lldp_links_found_and_confirmed_from_both_ends(self):
        lldp = [l for l in self.topo["links"] if l["method"] == "lldp"]
        self.assertEqual(len(lldp), len(make_fixture.LINKS))
        self.assertTrue(all(l["confirmed_bidirectional"] for l in lldp))

    def test_links_match_the_fixture_wiring(self):
        got = {
            frozenset([(l["a"]["node"], l["a"]["port"]),
                       (l["b"]["node"], l["b"]["port"])])
            for l in self.topo["links"] if l["method"] == "lldp"
        }
        want = {frozenset([(a, ap), (b, bp)])
                for a, ap, b, bp in make_fixture.LINKS}
        self.assertEqual(got, want)

    def test_remote_resolved_by_bridge_address(self):
        for l in self.topo["links"]:
            if l["method"] == "lldp":
                self.assertEqual(l["resolution"], "bridge-address match")

    def test_endpoints_attached_from_fdb(self):
        fdb = [l for l in self.topo["links"] if l["method"] == "fdb"]
        attached = {l["b"]["node"] for l in fdb}
        self.assertIn("UP-1", attached)
        self.assertIn("RPI1", attached)
        self.assertIn("CTRL", attached)

    def test_multihomed_endpoint_gets_both_attachments(self):
        up1 = [l for l in self.topo["links"]
               if l["method"] == "fdb" and l["b"]["node"] == "UP-1"]
        self.assertEqual(len(up1), 2)
        self.assertEqual({l["a"]["port"] for l in up1}, {"Gi 1/1", "Gi 1/2"})

    def test_inventory_name_wins_over_lldp_hostname(self):
        sw1 = next(n for n in self.topo["nodes"] if n["id"] == "SW1")
        self.assertEqual(sw1["label"], "SW1")
        self.assertEqual(sw1["hostname"], "kswitch-1")

    def test_unobserved_endpoint_does_not_break_agreement(self):
        cc = self.topo["crosscheck"]
        self.assertIn("RPI3", cc["endpoints_in_inventory_not_observed"])
        self.assertTrue(cc["agreement"])

    def test_no_unresolved_neighbours(self):
        self.assertEqual(self.topo["unresolved_lldp_neighbours"], [])


class TestCncDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = TestTopology
        t.setUpClass()
        cls._t = t
        cls.doc = cnc_mod.build(t.records, t.topo, t.inv,
                                {"run_id": "test", "tool_version": "test"})

    @classmethod
    def tearDownClass(cls):
        cls._t.tearDownClass()

    def test_envelope_is_the_minimum_across_ports(self):
        env = self.doc["network_capability_envelope"]
        self.assertEqual(env["min_supported_gcl_entries_max"], 128)
        self.assertEqual(env["min_supported_cycle_time_max_ns"], 33538048)

    def test_ptp_gap_present_and_blocking(self):
        gap = next(g for g in self.doc["gaps"] if g["capability"] == "ptp")
        self.assertEqual(gap["impact"], "blocking-for-verification")
        self.assertTrue(gap["workaround"])

    def test_datastore_coherence_gap_always_recorded(self):
        caps = {g["capability"] for g in self.doc["gaps"]}
        self.assertIn("datastore-coherence", caps)

    def test_port_roles_classified(self):
        sw2 = next(b for b in self.doc["network"]["bridges"]
                   if b["id"] == "SW2")
        roles = {p["name"]: p["role"] for p in sw2["ports"]}
        self.assertEqual(roles["2.5G 1/1"], "inter-switch")
        self.assertEqual(roles["2.5G 1/2"], "inter-switch")
        self.assertEqual(roles["Gi 1/1"], "unused")

    def test_network_wide_support_flags(self):
        s = self.doc["network_wide_support"]
        self.assertTrue(s["qbv"]["all_bridges"])
        self.assertTrue(s["qbu"]["all_bridges"])
        self.assertFalse(s["ptp"]["any_bridge"])
        self.assertFalse(s["qci"]["any_bridge"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
