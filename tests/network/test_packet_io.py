"""Verifica frame di attacco e cattura senza richiedere una rete attiva."""
import unittest
import json
from io import StringIO
from unittest.mock import patch

from scapy.all import ARP, IP, UDP, Dot1Q, Ether

from packet_io import (build_ecmp_packets, build_packet, capture, describe_packet,
                       execute_case, main, parse_args, send_ecmp_packets)


class PacketIoTests(unittest.TestCase):
    def test_ecmp_generates_distinct_udp_tuples_with_configured_addresses(self):
        packets = list(build_ecmp_packets('00:00:00:00:00:02', '10.0.0.1',
                                         '10.0.0.3', '00:00:00:00:00:08', 100))
        tuples = {(pkt[IP].src, pkt[IP].dst, pkt[IP].proto,
                   pkt[UDP].sport, pkt[UDP].dport) for pkt in packets}
        self.assertEqual(len(tuples), 100)
        self.assertEqual({pkt[UDP].sport for pkt in packets}, set(range(40000, 40100)))
        for packet in packets:
            self.assertEqual((packet[Ether].src, packet[Ether].dst),
                             ('00:00:00:00:00:02', '00:00:00:00:00:08'))
            self.assertEqual((packet[IP].src, packet[IP].dst, packet[IP].proto),
                             ('10.0.0.1', '10.0.0.3', 17))
            self.assertEqual(packet[UDP].dport, 9000)
            self.assertNotIn(Dot1Q, packet)

    @patch('packet_io.conf.L2socket')
    def test_ecmp_sends_each_flow_and_reports_completed_packet_count(self, socket):
        packets = build_ecmp_packets('00:00:00:00:00:02', '10.0.0.1',
                                    '10.0.0.2', '00:00:00:00:00:06', 2)
        self.assertEqual(send_ecmp_packets('h1-eth0', packets, 3), 6)
        socket.assert_called_once_with(iface='h1-eth0')
        sender = socket.return_value.__enter__.return_value
        self.assertEqual(sender.send.call_count, 6)
        self.assertEqual([call.args[0][UDP].sport for call in sender.send.call_args_list],
                         [40000, 40000, 40000, 40001, 40001, 40001])

    def test_ecmp_argument_defaults_and_highest_valid_source_port(self):
        args = parse_args(['--iface', 'h1-eth0', '--case', 'ecmp-udp'])
        self.assertEqual((args.flows, args.packets_per_flow), (100, 10))
        args = parse_args(['--iface', 'h1-eth0', '--case', 'ecmp-udp',
                           '--flows', '25536', '--packets-per-flow', '1'])
        self.assertEqual(args.flows, 25536)
        self.assertEqual(args.packets_per_flow, 1)

    def test_ecmp_rejects_invalid_counts_and_options_in_other_modes(self):
        invalid = [(['--case', 'ecmp-udp', '--flows', '0']),
                   (['--case', 'ecmp-udp', '--flows', '-1']),
                   (['--case', 'ecmp-udp', '--flows', '25537']),
                   (['--case', 'ecmp-udp', '--packets-per-flow', '0']),
                   (['--case', 'ecmp-udp', '--packets-per-flow', '-1']),
                   (['--case', 'ipv4-untagged', '--flows', '10']),
                   (['--case', 'arp-request', '--packets-per-flow', '10']),
                   (['--listen', '--flows', '10'])]
        for options in invalid:
            with self.subTest(options=options), patch('sys.stderr', new_callable=StringIO):
                with self.assertRaises(SystemExit) as error:
                    parse_args(['--iface', 'h1-eth0', *options])
                self.assertEqual(error.exception.code, 2)

    @patch('packet_io.send_ecmp_packets', return_value=6)
    @patch('sys.stdout', new_callable=StringIO)
    def test_ecmp_cli_prints_offered_traffic_and_completed_packets(self, output, send):
        main(['--iface', 'h1-eth0', '--case', 'ecmp-udp',
              '--flows', '2', '--packets-per-flow', '3'])
        self.assertIn('Ethernet: 00:00:00:00:00:02 -> 00:00:00:00:00:06',
                      output.getvalue())
        self.assertIn('IPv4: 10.0.0.1 -> 10.0.0.2', output.getvalue())
        self.assertIn('Porte UDP: 40000-40001 -> 9000', output.getvalue())
        self.assertIn('Traffico offerto: 2 flussi UDP, 3 pacchetti per flusso'
                      ' (6 pacchetti).', output.getvalue())
        self.assertIn('Pacchetti effettivamente inviati: 6', output.getvalue())
        self.assertEqual(send.call_args.args[0], 'h1-eth0')
        self.assertEqual(send.call_args.args[2], 3)

    def test_overlapping_ip_attack_keeps_explicit_victim_mac(self):
        packet = build_packet('ipv4-untagged', '00:00:00:00:00:02',
                              '10.0.0.1', '10.0.0.1', '00:00:00:00:00:04')
        self.assertEqual(packet[Ether].dst, '00:00:00:00:00:04')
        self.assertEqual((packet[IP].src, packet[IP].dst, packet[IP].proto),
                         ('10.0.0.1', '10.0.0.1', 253))
        self.assertNotIn(Dot1Q, packet)
        self.assertIn('00:00:00:00:00:04', describe_packet(packet))

    @patch('packet_io.print')
    @patch('packet_io.sniff')
    @patch('packet_io.conf.L2listen')
    def test_capture_is_ready_before_sniff_and_accepts_any_destination(self,
                                                                    listen,
                                                                    sniff,
                                                                    output):
        valid = Ether(dst='00:00:00:00:00:04') / Dot1Q(vlan=101) / IP(proto=253)
        unrelated = Ether() / IP(proto=1)
        arp = Ether() / ARP()

        ready = unittest.mock.Mock()

        def receive(**kwargs):
            ready.assert_called_once_with()
            output.assert_not_called()
            self.assertIs(kwargs['opened_socket'], listen.return_value.__enter__.return_value)
            self.assertEqual(kwargs['timeout'], 5)
            return [pkt for pkt in (valid, unrelated, arp) if kwargs['lfilter'](pkt)]

        sniff.side_effect = receive
        self.assertEqual(capture('h6-eth0', 5, on_ready=ready), [valid])
        listen.assert_called_once_with(iface='h6-eth0', promisc=True)
        self.assertIn('VLAN: 101', describe_packet(valid))

    @patch('packet_io.print')
    @patch('packet_io.exchange')
    def test_core_returns_structured_arp_replies_without_printing(self, exchange, output):
        exchange.return_value = [Ether(src='00:00:00:00:00:06') /
                                 ARP(op=2, psrc='10.0.0.2', pdst='10.0.0.1')]
        result = execute_case('h2-eth0', 'arp-request')
        self.assertEqual(result['sent_packets'], 1)
        self.assertEqual(result['frame']['operation'], 1)
        self.assertEqual(result['replies'][0]['source_ip'], '10.0.0.2')
        self.assertEqual(result['replies'][0]['operation'], 2)
        json.dumps(result)
        output.assert_not_called()

    @patch('packet_io.send_ecmp_packets', return_value=6)
    @patch('sys.stdout', new_callable=StringIO)
    def test_json_mode_returns_one_document_without_cli_output(self, output, send):
        main(['--iface', 'h2-eth0', '--case', 'ecmp-udp', '--flows', '2',
              '--packets-per-flow', '3', '--json'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['sent_packets'], 6)
        self.assertEqual(result['flows'], 2)
        self.assertEqual(result['last_source_port'], 40001)

    @patch('packet_io.conf.L2socket')
    def test_invalid_core_parameters_are_rejected_before_opening_socket(self, socket):
        for options in ({'mac': 'bad'}, {'ip': 'not-an-ip'}, {'vlan': 4095},
                        {'timeout': float('nan')}, {'timeout': float('inf')},
                        {'flows': 25537}, {'packets_per_flow': 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                execute_case('h2-eth0', 'ecmp-udp', **options)
        socket.assert_not_called()


if __name__ == '__main__':
    unittest.main()
