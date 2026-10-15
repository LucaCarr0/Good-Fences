"""Verifiche della pipeline con messaggi OpenFlow Ryu reali, senza rete attiva."""
from copy import deepcopy
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
import unittest

from ryu.lib.packet import arp, ethernet, packet
from ryu.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser

from controller.arp_proxy import build_arp_reply
from controller.pipeline import (ARP_COOKIE, BASE_COOKIE, TAG_DROP_COOKIE,
                                 install_entry_tables)
from network_models import HostModel, NetworkModel


def installed(model, dpid):
    messages = []
    datapath = SimpleNamespace(id=dpid, ofproto=ofp, ofproto_parser=parser,
                              send_msg=messages.append)
    install_entry_tables(datapath, model)
    for message in messages:
        message.serialize()
    assert messages[0].command == ofp.OFPFC_DELETE
    return messages[1:]


def traverse(flows, fields):
    """Applica match/priorità e le sole azioni usate dalla pipeline al frame."""
    fields, table, outputs, visited = dict(fields), 0, [], []
    while table is not None:
        def matches(flow):
            for key, value in flow.match.items():
                actual = fields.get(key)
                if isinstance(value, tuple):
                    if actual is None or actual & value[1] != value[0] & value[1]:
                        return False
                elif actual != value:
                    return False
            return True

        flow = max((flow for flow in flows
                    if flow.table_id == table and matches(flow)),
                   key=lambda flow: flow.priority)
        visited.append((table, flow.cookie))
        table = None
        for instruction in flow.instructions:
            if isinstance(instruction, parser.OFPInstructionGotoTable):
                assert instruction.table_id > flow.table_id
                table = instruction.table_id
                continue
            assert instruction.type == ofp.OFPIT_APPLY_ACTIONS
            for action in instruction.actions:
                if isinstance(action, parser.OFPActionPushVlan):
                    fields['vlan_vid'] = ofp.OFPVID_PRESENT
                elif isinstance(action, parser.OFPActionSetField):
                    fields[action.key] = action.value
                elif isinstance(action, parser.OFPActionPopVlan):
                    fields['vlan_vid'] = ofp.OFPVID_NONE
                else:
                    assert isinstance(action, parser.OFPActionOutput)
                    outputs.append((action.port, dict(fields)))
    return outputs, visited


def ipv4(source, destination):
    return dict(in_port=source.port, vlan_vid=ofp.OFPVID_NONE,
                eth_type=0x0800, eth_src=source.mac, ipv4_src=source.ip,
                eth_dst=destination.mac, ipv4_dst=destination.ip)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = NetworkModel(str(Path(__file__).resolve().parents[2] / 'config'))
        cls.flows = {dpid: installed(cls.model, dpid)
                     for dpid in (1, 2, 3, 101, 102)}
        cls.source, cls.destination = cls.model.tenants[1].hosts[:2]

    def test_all_same_tenant_interleaf_paths(self):
        for tenant in self.model.tenants:
            for source, destination in permutations(tenant.hosts, 2):
                with self.subTest(source=source.name, destination=destination.name):
                    source_leaf = (source.host_id - 1) // self.model.hosts_per_leaf + 1
                    target_leaf = (destination.host_id - 1) // self.model.hosts_per_leaf + 1
                    outputs, _ = traverse(self.flows[source_leaf], ipv4(source, destination))
                    uplink, tagged = outputs[0]
                    self.assertEqual((len(outputs), uplink, tagged['vlan_vid']),
                                     (1, 5, ofp.OFPVID_PRESENT | tenant.tag))
                    outputs, _ = traverse(self.flows[101], dict(tagged, in_port=source_leaf))
                    self.assertEqual((len(outputs), outputs[0][0]), (1, target_leaf))
                    outputs, _ = traverse(self.flows[target_leaf], dict(outputs[0][1], in_port=5))
                    self.assertEqual((len(outputs), outputs[0][0], outputs[0][1]['vlan_vid']),
                                     (1, destination.port, ofp.OFPVID_NONE))

    def test_same_leaf_delivery_in_both_directions(self):
        model = deepcopy(self.model)
        model.tenants[0].hosts[2] = HostModel(name='h4', leaf='leaf1', port=4, ip='10.0.0.3')
        flows = installed(model, 1)
        for source, destination in permutations((model.tenants[0].hosts[0],
                                                model.tenants[0].hosts[2]), 2):
            outputs, _ = traverse(flows, ipv4(source, destination))
            self.assertEqual((len(outputs), outputs[0][0], outputs[0][1]['vlan_vid']),
                             (1, destination.port, ofp.OFPVID_NONE))

    def test_access_spoofing_tags_and_spare_are_dropped(self):
        for changes in (dict(eth_src='00:00:00:00:00:01'),
                        dict(ipv4_src='10.0.0.99'), dict(in_port=4),
                        *(dict(vlan_vid=ofp.OFPVID_PRESENT | tag) for tag in (0, 101, 102)),
                        *(dict(eth_type=tpid) for tpid in (0x8100, 0x88a8, 0x9100))):
            with self.subTest(changes=changes):
                outputs, visited = traverse(self.flows[1], dict(ipv4(self.source, self.destination), **changes))
                self.assertEqual(outputs, [])
                self.assertEqual(visited[0][0], 0)
                self.assertEqual(visited[-1][1], TAG_DROP_COOKIE
                                 if 'vlan_vid' in changes or 'eth_type' in changes else BASE_COOKIE)

    def test_destination_tuple_overlap_and_non_unicast_are_dropped(self):
        for changes in (dict(ipv4_dst='10.0.0.13'),
                        dict(eth_dst='00:00:00:00:00:05'),  # h5, stesso IP di h6, tenant A.
                        dict(eth_dst='00:00:00:00:00:ff', ipv4_dst='10.0.0.99'),
                        dict(eth_dst='ff:ff:ff:ff:ff:ff'),
                        dict(eth_dst='01:00:5e:00:00:01')):
            with self.subTest(changes=changes):
                outputs, visited = traverse(self.flows[1], dict(ipv4(self.source, self.destination), **changes))
                self.assertEqual(outputs, [])
                self.assertEqual(visited[-1], (1, BASE_COOKIE))

    def test_fabric_return_paths_and_unused_uplink_are_dropped(self):
        tagged = dict(ipv4(self.source, self.destination), vlan_vid=ofp.OFPVID_PRESENT | 102)
        for dpid, changes, drop_table in ((1, dict(in_port=5), 2),
                                         (1, dict(in_port=6), 0),
                                         (101, dict(in_port=2), 0),
                                         (101, dict(in_port=99), 0),
                                         (101, dict(in_port=1, vlan_vid=ofp.OFPVID_PRESENT | 101), 0),
                                         (102, dict(in_port=1), 0)):
            with self.subTest(dpid=dpid, changes=changes):
                outputs, visited = traverse(self.flows[dpid], dict(tagged, **changes))
                self.assertEqual(outputs, [])
                self.assertEqual(visited[-1], (drop_table, BASE_COOKIE))
        self.assertEqual(len(self.flows[102]), 1)

    def test_arp_proxy_preserves_overlapping_tenants(self):
        access, tenants = self.model.get_arp_bindings()
        for source_port, target_mac in ((1, '00:00:00:00:00:05'),
                                        (2, '00:00:00:00:00:06')):
            source = access[(1, source_port)][1]
            fields = dict(in_port=source_port, vlan_vid=ofp.OFPVID_NONE,
                          eth_type=0x0806, eth_src=source.mac, arp_sha=source.mac,
                          arp_spa=source.ip, arp_tpa='10.0.0.2', arp_op=arp.ARP_REQUEST)
            outputs, visited = traverse(self.flows[1], fields)
            self.assertEqual((outputs[0][0], visited[-1]), (ofp.OFPP_CONTROLLER, (0, ARP_COOKIE)))
            request = packet.Packet()
            request.add_protocol(ethernet.ethernet(src=source.mac, ethertype=0x0806))
            request.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac=source.mac,
                                         src_ip=source.ip, dst_ip='10.0.0.2'))
            request.serialize()
            reply = packet.Packet(build_arp_reply(request.data, access[(1, source_port)], tenants))
            self.assertEqual(reply.get_protocol(arp.arp).src_mac, target_mac)
            self.assertEqual(reply.get_protocol(ethernet.ethernet).dst, source.mac)


if __name__ == '__main__':
    unittest.main()
