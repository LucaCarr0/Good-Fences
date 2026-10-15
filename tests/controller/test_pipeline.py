"""Verifiche della pipeline con messaggi OpenFlow Ryu reali, senza rete attiva."""
from copy import deepcopy
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ryu.lib.packet import arp, ethernet, packet
from ryu.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser

from controller.arp_proxy import build_arp_reply
from controller.app import GoodFencesApp
from controller.pipeline import (ARP_COOKIE, BASE_COOKIE, ECMP_GROUP_ID,
                                 TAG_DROP_COOKIE, install_entry_tables,
                                 prepare_pipeline)
from network_models import HostModel, NetworkModel


def installed(model, dpid):
    messages = []
    datapath = SimpleNamespace(id=dpid, ofproto=ofp, ofproto_parser=parser,
                              send_msg=messages.append)
    prepare_pipeline(datapath, model)
    install_entry_tables(datapath, model)
    for message in messages:
        message.serialize()
    assert messages[0].command == ofp.OFPFC_DELETE
    flows = [message for message in messages[1:]
             if isinstance(message, parser.OFPFlowMod)]
    groups = {message.group_id: message for message in messages
              if isinstance(message, parser.OFPGroupMod)
              and message.command == ofp.OFPGC_ADD}
    return flows, groups


def traverse(flows, fields, groups=None, bucket_index=0):
    """Verifica un percorso scelto esplicitamente, senza simulare l'hash OVS."""
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
            actions = []
            for action in instruction.actions:
                if isinstance(action, parser.OFPActionGroup):
                    group = groups[action.group_id]
                    assert group.type == ofp.OFPGT_SELECT
                    actions.extend(group.buckets[bucket_index].actions)
                else:
                    actions.append(action)
            for action in actions:
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
        pipelines = {dpid: installed(cls.model, dpid)
                     for dpid in (1, 2, 3, 101, 102)}
        cls.flows = {dpid: values[0] for dpid, values in pipelines.items()}
        cls.groups = {dpid: values[1] for dpid, values in pipelines.items()}
        cls.source, cls.destination = cls.model.tenants[1].hosts[:2]

    def test_all_same_tenant_interleaf_paths(self):
        for tenant in self.model.tenants:
            for source, destination in permutations(tenant.hosts, 2):
                for bucket_index, uplink in enumerate(self.model.get_leaf_uplink_ports()):
                    self.check_interleaf_path(tenant, source, destination, bucket_index, uplink)

    def check_interleaf_path(self, tenant, source, destination, bucket_index, uplink):
        with self.subTest(source=source.name, destination=destination.name, uplink=uplink):
            source_leaf = (source.host_id - 1) // self.model.hosts_per_leaf + 1
            target_leaf = (destination.host_id - 1) // self.model.hosts_per_leaf + 1
            outputs, _ = traverse(self.flows[source_leaf], ipv4(source, destination),
                                  self.groups[source_leaf], bucket_index)
            selected_port, tagged = outputs[0]
            self.assertEqual((len(outputs), selected_port, tagged['vlan_vid']),
                             (1, uplink, ofp.OFPVID_PRESENT | tenant.tag))
            spine_id = 101 + bucket_index
            outputs, _ = traverse(self.flows[spine_id], dict(tagged, in_port=source_leaf))
            self.assertEqual((len(outputs), outputs[0][0]), (1, target_leaf))
            outputs, _ = traverse(self.flows[target_leaf], dict(outputs[0][1], in_port=uplink))
            self.assertEqual((len(outputs), outputs[0][0], outputs[0][1]['vlan_vid']),
                             (1, destination.port, ofp.OFPVID_NONE))

    def test_same_leaf_delivery_in_both_directions(self):
        model = deepcopy(self.model)
        model.tenants[0].hosts[2] = HostModel(name='h4', leaf='leaf1', port=4, ip='10.0.0.3')
        flows, _ = installed(model, 1)
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

    def test_fabric_return_paths_are_dropped_on_both_paths(self):
        tagged = dict(ipv4(self.source, self.destination), vlan_vid=ofp.OFPVID_PRESENT | 102)
        for dpid, changes, drop_table in ((1, dict(in_port=5), 2),
                                         (1, dict(in_port=6), 2),
                                         (1, dict(in_port=7), 0),
                                         (101, dict(in_port=2), 0),
                                         (101, dict(in_port=99), 0),
                                         (101, dict(in_port=1, vlan_vid=ofp.OFPVID_PRESENT | 101), 0),
                                         (102, dict(in_port=2), 0),
                                         (102, dict(in_port=99), 0),
                                         (102, dict(in_port=1, vlan_vid=ofp.OFPVID_PRESENT | 101), 0)):
            with self.subTest(dpid=dpid, changes=changes):
                outputs, visited = traverse(self.flows[dpid], dict(tagged, **changes))
                self.assertEqual(outputs, [])
                self.assertEqual(visited[-1], (drop_table, BASE_COOKIE))

    def test_select_groups_have_equal_weights_and_no_failover_watches(self):
        for dpid, groups in self.groups.items():
            if self.model.is_spine(dpid):
                self.assertEqual(groups, {})
                continue
            self.assertEqual(set(groups), {ECMP_GROUP_ID})
            group = groups[ECMP_GROUP_ID]
            self.assertEqual(group.type, ofp.OFPGT_SELECT)
            self.assertEqual([bucket.weight for bucket in group.buckets], [1, 1])
            for bucket in group.buckets:
                self.assertEqual((bucket.watch_port, bucket.watch_group),
                                 (ofp.OFPP_ANY, ofp.OFPG_ANY))

    def start_controller_installation(self):
        """Crea solo lo stato necessario agli handler, senza avviare Ryu."""
        app = GoodFencesApp.__new__(GoodFencesApp)
        app.model = self.model
        app.datapaths, app.pending, app.ready = {}, {}, set()
        app.expected_dpids, app.logger = {1}, Mock()
        messages = []
        dp = SimpleNamespace(id=1, ofproto=ofp, ofproto_parser=parser,
                             send_msg=messages.append, close=Mock())
        dp.set_xid = lambda message: setattr(message, 'xid', len(messages) + 1)
        features = SimpleNamespace(msg=SimpleNamespace(datapath=dp, n_tables=3))
        with patch('controller.app.hub.spawn_after'):
            app.switch_features_handler(features)
        return app, dp, messages

    def acknowledge_barrier(self, app, dp, xid):
        event = SimpleNamespace(msg=SimpleNamespace(datapath=dp, xid=xid))
        with patch('controller.app.hub.spawn_after'):
            app.barrier_reply_handler(event)

    def test_group_barrier_precedes_permissive_flows_and_ready(self):
        app, dp, messages = self.start_controller_installation()
        group_xid = messages[-1].xid
        self.assertEqual(app.ready, set())
        self.assertTrue(all(message.priority == 0 for message in messages
                            if isinstance(message, parser.OFPFlowMod)
                            and message.command == ofp.OFPFC_ADD))
        group_commands = [message.command for message in messages
                          if isinstance(message, parser.OFPGroupMod)]
        self.assertEqual(group_commands, [ofp.OFPGC_DELETE, ofp.OFPGC_ADD])
        self.acknowledge_barrier(app, dp, group_xid)
        self.assertTrue(any(message.priority > 0 for message in messages
                            if isinstance(message, parser.OFPFlowMod)))
        self.assertEqual(app.ready, set())
        # La scadenza della prima fase non deve interrompere la seconda.
        app._installation_timeout(dp, group_xid, 'groups')
        dp.close.assert_not_called()
        self.acknowledge_barrier(app, dp, group_xid)  # Reply duplicata, da ignorare.
        self.acknowledge_barrier(app, dp, messages[-1].xid)
        self.assertEqual(app.ready, {1})
        self.assertEqual(app.pending, {})

    def test_switch_errors_and_timeouts_prevent_ready_in_both_phases(self):
        for phase in ('groups', 'flows'):
            for failure in ('error', 'timeout'):
                with self.subTest(phase=phase, failure=failure):
                    app, dp, messages = self.start_controller_installation()
                    if phase == 'flows':
                        self.acknowledge_barrier(app, dp, messages[-1].xid)
                    xid = messages[-1].xid
                    if failure == 'error':
                        event = SimpleNamespace(msg=SimpleNamespace(
                            datapath=dp, xid=xid, type=6, code=1, data=b''))
                        app.error_handler(event)
                        app.logger.error.assert_called_once()
                    else:
                        app._installation_timeout(dp, xid, phase)
                    message_count = len(messages)
                    self.acknowledge_barrier(app, dp, xid)
                    self.assertEqual(len(messages), message_count)
                    self.assertEqual(app.ready, set())
                    self.assertEqual(app.pending, {})
                    dp.close.assert_called_once()

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
