"""Verifica parsing, traduzione e confronto dei contatori senza una rete attiva."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from flow_visualizer import (available_switches, delta, parse_dump,
                             readable_actions, readable_match, run_ovs)


HOST = (' cookie=0x47460040, duration=575.405s, table=0, n_packets=2, '
        'n_bytes=68, priority=200,ip,in_port="leaf1-eth2",'
        'vlan_tci=0x0000/0x1fff,dl_src=00:00:00:00:00:02,nw_src=10.0.0.1 '
        'actions=push_vlan:0x8100,set_field:4198->vlan_vid,goto_table:1')
MISS = (' cookie=0x47460010, duration=575.407s, table=2, n_packets=2, '
        'n_bytes=68, priority=0 actions=drop')


class VisualizerTests(unittest.TestCase):
    def test_dump_with_terminal_prompt_and_header(self):
        flows = parse_dump('user$ sudo ovs-ofctl dump-flows leaf1\n'
                           'OFPST_FLOW reply (OF1.3):\n' + MISS + '\n' + HOST)
        self.assertEqual(len(flows), 2)
        self.assertEqual((flows[0].table, flows[0].packets, flows[0].byte_count), (0, 2, 68))
        self.assertEqual(flows[0].actions,
                         'push_vlan:0x8100,set_field:4198->vlan_vid,goto_table:1')
        self.assertIn('senza VLAN', readable_match(flows[0]))
        self.assertIn('porta=leaf1-eth2', readable_match(flows[0]))
        self.assertEqual(readable_match(flows[1]), 'qualsiasi pacchetto (table-miss)')

    def test_vlan_present_bit_is_removed_from_displayed_id(self):
        self.assertEqual(readable_actions(parse_dump(HOST)[0].actions),
                         'Aggiungi tag VLAN; VLAN 102; Vai a tabella 1')
        self.assertEqual(readable_actions('CONTROLLER:65535'), 'Invia al controller')
        self.assertEqual(readable_actions('drop'), 'DROP')
        self.assertEqual(readable_actions('pop_vlan,output:3'),
                         'Rimuovi tag VLAN; Invia a porta 3')

    def test_forwarding_categories(self):
        flow = parse_dump(HOST)[0]
        for cookie, category in ((0x47460042, 'IPv4 destinazione validata'),
                                 (0x47460050, 'IPv4 consegna host'),
                                 (0x47460051, 'IPv4 uplink singolo'),
                                 (0x47460052, 'IPv4 forwarding spine')):
            with self.subTest(cookie=hex(cookie)):
                self.assertEqual(replace(flow, cookie=cookie).category, category)

    def test_delta_survives_match_reordering(self):
        before = parse_dump(HOST)[0]
        after = replace(before, packets=3, byte_count=102, duration=580,
                        match=tuple(reversed(before.match)))
        self.assertEqual(delta(after, {before.key: before}), (1, 34))

    def test_reinstalled_rule_new_rule_and_counter_reset(self):
        before = parse_dump(HOST)[0]
        baseline = {before.key: before}
        self.assertEqual(delta(replace(before, duration=1), baseline), ('reset', 'reset'))
        self.assertEqual(delta(replace(before, packets=0), baseline), ('reset', 'reset'))
        self.assertEqual(delta(replace(before, byte_count=0), baseline), ('reset', 'reset'))
        self.assertEqual(delta(replace(before, priority=201), baseline), ('nuova', 'nuova'))

    def test_probe_and_gratuitous_are_distinct(self):
        template = ('cookie=0x47460021, duration=5s, table=0, n_packets=1, '
                    'n_bytes=42, priority=350,arp,{} actions=drop')
        probe = parse_dump(template.format('arp_spa=0.0.0.0'))[0]
        gratuitous = parse_dump(template.format('arp_tpa=10.0.0.1'))[0]
        self.assertEqual(probe.category, 'ARP Probe DROP')
        self.assertEqual(gratuitous.category, 'ARP gratuito DROP')

    def test_empty_or_malformed_dump_is_reported(self):
        for dump in ('', 'OFPST_FLOW reply (OF1.3):', 'cookie=0x1 actions=drop'):
            with self.subTest(dump=dump), self.assertRaises(ValueError):
                parse_dump(dump)

    @patch('flow_visualizer.run_ovs', return_value='spine2\nleaf10\nleaf2\nspine1\nbr0\n')
    def test_switch_discovery_filters_and_sorts(self, command):
        self.assertEqual(available_switches(), ['leaf2', 'leaf10', 'spine1', 'spine2'])
        command.assert_called_once_with(['ovs-vsctl', 'list-br'])

    @patch('flow_visualizer.subprocess.run')
    def test_ovs_failure_keeps_error_message(self, command):
        command.return_value.returncode = 1
        command.return_value.stderr = 'permission denied'
        with self.assertRaisesRegex(RuntimeError, 'permission denied'):
            run_ovs(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', 'leaf1'])


if __name__ == '__main__':
    unittest.main()
