"""Verifica parsing, traduzione e confronto dei contatori senza una rete attiva."""
from dataclasses import replace
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import call, patch

from flow_visualizer import (GroupSnapshot, available_switches, bucket_deltas, delta,
                             main, parse_dump, parse_group_stats, parse_groups,
                             read_state, readable_actions, readable_match, run_ovs,
                             show_groups)


HOST = (' cookie=0x47460040, duration=575.405s, table=0, n_packets=2, '
        'n_bytes=68, priority=200,ip,in_port="leaf1-eth2",'
        'vlan_tci=0x0000/0x1fff,dl_src=00:00:00:00:00:02,nw_src=10.0.0.1 '
        'actions=push_vlan:0x8100,set_field:4198->vlan_vid,goto_table:1')
MISS = (' cookie=0x47460010, duration=575.407s, table=2, n_packets=2, '
        'n_bytes=68, priority=0 actions=drop')
# Formato verificato con ovs-ofctl 3.3.9: il peso predefinito 1 viene omesso.
GROUPS = ('OFPST_GROUP_DESC reply (OF1.3) (xid=0x2):\n'
          ' group_id=1,type=select,bucket=actions=output:5,bucket=actions=output:6')
STATS = ('OFPST_GROUP reply (OF1.3) (xid=0x2):\n'
         ' group_id=1,duration=50.500s,ref_count=2,packet_count=300,byte_count=30000,'
         'bucket0:packet_count=200,byte_count=20000,'
         'bucket1:packet_count=100,byte_count=10000')


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
        self.assertEqual(readable_actions('group:1'), 'Usa gruppo 1')

    def test_forwarding_categories(self):
        flow = parse_dump(HOST)[0]
        for cookie, category in ((0x47460042, 'IPv4 destinazione validata'),
                                 (0x47460050, 'IPv4 consegna host'),
                                 (0x47460051, 'IPv4 remoto / ECMP'),
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


class GroupVisualizerTests(unittest.TestCase):
    def setUp(self):
        self.before = parse_group_stats(STATS, parse_groups(GROUPS))[0]
        self.after = replace(
            self.before, duration=60, packets=1300, byte_count=130000,
            buckets=(replace(self.before.buckets[0], packets=740, byte_count=74000),
                     replace(self.before.buckets[1], packets=560, byte_count=56000)))

    def render(self, current, baseline=None):
        output = StringIO()
        with patch('sys.stdout', output):
            show_groups(current, baseline)
        return output.getvalue()

    def test_parse_group_definitions_and_openflow13_bucket_statistics(self):
        self.assertEqual((self.before.group_id, self.before.kind, self.before.duration),
                         (1, 'select', 50.5))
        self.assertEqual((self.before.packets, self.before.byte_count), (300, 30000))
        self.assertEqual([(bucket.weight, bucket.actions, bucket.packets, bucket.byte_count)
                          for bucket in self.before.buckets],
                         [(1, 'output:5', 200, 20000), (1, 'output:6', 100, 10000)])

    def test_default_weight_and_output_port_name(self):
        group = parse_groups('group_id=7,type=select,bucket=actions=output:"leaf1-eth5"')[0]
        self.assertEqual(group.buckets[0].weight, 1)
        self.assertEqual(group.buckets[0].actions, 'output:"leaf1-eth5"')
        weighted = parse_groups(GROUPS.replace('bucket=actions=', 'bucket=weight:2,actions='))[0]
        self.assertEqual([bucket.weight for bucket in weighted.buckets], [2, 2])

    def test_each_percentage_uses_packet_delta_and_group_delta_sum(self):
        self.assertEqual(bucket_deltas(self.after, self.before),
                         ([(540, 54000), (460, 46000)], None))
        output = self.render(GroupSnapshot((self.after,)), GroupSnapshot((self.before,)))
        for text in ('Gruppo 1', 'select', 'Pkt prima', 'Pkt dopo', 'Byte prima', 'Byte dopo',
                     '540', '460', '54.0%', '46.0%'):
            with self.subTest(text=text):
                self.assertIn(text, output)

    def test_zero_traffic_has_zero_deltas_and_no_observed_percentage(self):
        self.assertEqual(bucket_deltas(self.before, self.before), ([(0, 0), (0, 0)], None))
        output = self.render(GroupSnapshot((self.before,)), GroupSnapshot((self.before,)))
        self.assertIn('quote non calcolate', output)
        self.assertNotIn('0.0%', output)

    def test_group_reinstallation_and_any_counter_decrease_invalidate_comparison(self):
        bucket_reset = replace(self.after.buckets[1], packets=99)
        for group in (replace(self.after, duration=1), replace(self.after, packets=299),
                      replace(self.after, byte_count=29999),
                      replace(self.after, buckets=(self.after.buckets[0], bucket_reset)),
                      replace(self.after, buckets=(replace(self.after.buckets[0], byte_count=1),
                                                   self.after.buckets[1]))):
            with self.subTest(group=group):
                changes, error = bucket_deltas(group, self.before)
                self.assertIsNone(changes)
                self.assertIn('reset', error)

    def test_changed_weight_action_or_bucket_order_invalidates_comparison(self):
        for buckets in ((replace(self.after.buckets[0], weight=2), self.after.buckets[1]),
                        (replace(self.after.buckets[0], actions='output:7'), self.after.buckets[1]),
                        tuple(reversed(self.after.buckets))):
            with self.subTest(buckets=buckets):
                changes, error = bucket_deltas(replace(self.after, buckets=buckets), self.before)
                self.assertIsNone(changes)
                self.assertIn('gruppo modificato', error)

    def test_unsupported_or_missing_bucket_counters_are_explicit(self):
        for unavailable in ('?', str(2**64 - 1)):
            stats = STATS.replace('bucket1:packet_count=100',
                                  f'bucket1:packet_count={unavailable}')
            group = parse_group_stats(stats, parse_groups(GROUPS))[0]
            self.assertIsNone(group.buckets[1].packets)
            changes, error = bucket_deltas(group, self.before)
            self.assertIsNone(changes)
            self.assertIn('non disponibili', error)
            output = self.render(GroupSnapshot((group,)), GroupSnapshot((self.before,)))
            self.assertIn('n/d', output)
            self.assertNotIn('0.0%', output)
        group = parse_group_stats('OFPST_GROUP reply (OF1.3):', parse_groups(GROUPS))[0]
        self.assertIsNone(group.buckets[0].packets)

    def test_no_groups_errors_and_missing_reference_have_distinct_messages(self):
        self.assertEqual(parse_groups('OFPST_GROUP_DESC reply (OF1.3):'), [])
        self.assertIn('Nessun gruppo installato', self.render(GroupSnapshot()))
        self.assertIn('permission denied', self.render(GroupSnapshot(error='permission denied')))
        self.assertIn('riferimento assente', self.render(GroupSnapshot((self.after,))))

    def test_malformed_data_and_bucket_count_change_are_rejected(self):
        for dump in ('', 'group_id=1,bucket=actions=output:5',
                     'group_id=1,type=select,bucket=weight:1'):
            with self.subTest(dump=dump), self.assertRaises(ValueError):
                parse_groups(dump)
        for stats in ('', STATS.replace('bucket1:', 'bucket2:'),
                      STATS.replace('packet_count=100,byte_count=10000', 'packet_count=oops'),
                      STATS.split(',bucket1:')[0]):
            with self.subTest(stats=stats), self.assertRaises(ValueError):
                parse_group_stats(stats, parse_groups(GROUPS))

    @patch('flow_visualizer.run_ovs', side_effect=[HOST, GROUPS, STATS])
    def test_live_read_requests_openflow13_flow_descriptions_and_statistics(self, command):
        flows, snapshot = read_state('leaf1')
        self.assertEqual(len(flows), 1)
        self.assertEqual(snapshot.groups, (self.before,))
        self.assertEqual(command.call_args_list, [
            call(['ovs-ofctl', '-O', 'OpenFlow13', operation, 'leaf1'])
            for operation in ('dump-flows', 'dump-groups', 'dump-group-stats')])

    @patch('flow_visualizer.run_ovs', side_effect=[HOST, RuntimeError('groups unavailable')])
    def test_group_read_error_preserves_flow_display(self, command):
        flows, snapshot = read_state('leaf1')
        self.assertEqual(len(flows), 1)
        self.assertEqual(snapshot.error, 'groups unavailable')

    @patch('flow_visualizer.run_ovs')
    @patch('flow_visualizer.Path.read_text', return_value=HOST)
    def test_file_mode_never_queries_groups(self, read_text, command):
        flows, snapshot = read_state('Dump da file', Path('/tmp/flows.txt'))
        self.assertEqual(len(flows), 1)
        self.assertIsNone(snapshot)
        command.assert_not_called()

    @patch('sys.argv', ['flow_visualizer.py', '--switch', 'leaf1'])
    @patch('sys.stdin.isatty', return_value=True)
    @patch('flow_visualizer.show')
    @patch('flow_visualizer.read_state')
    @patch('builtins.input', side_effect=['r', 'b', 's', 'q'])
    @patch('flow_visualizer.choose_switch', return_value='leaf2')
    def test_refresh_baseline_and_switch_keep_bucket_references_in_sync(
            self, choose, input_command, read, show, isatty):
        flows = parse_dump(HOST)
        initial = GroupSnapshot((self.before,))
        refreshed = GroupSnapshot((self.after,))
        saved = GroupSnapshot((replace(self.after, duration=70),))
        other_switch = GroupSnapshot((replace(self.before, packets=0),))
        read.side_effect = [(flows, initial), (flows, refreshed), (flows, saved),
                            (flows, other_switch)]
        self.assertEqual(main(), 0)
        calls = show.call_args_list
        self.assertEqual([(item.args[0], item.args[4], item.args[5]) for item in calls],
                         [('leaf1', initial, initial), ('leaf1', refreshed, initial),
                          ('leaf1', saved, saved), ('leaf2', other_switch, other_switch)])
        self.assertEqual(read.call_args_list,
                         [call('leaf1', None), call('leaf1', None), call('leaf1', None),
                          call('leaf2')])


if __name__ == '__main__':
    unittest.main()
