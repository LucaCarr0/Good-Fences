"""Verifica frame di attacco e cattura senza richiedere una rete attiva."""
import unittest
from unittest.mock import patch

from scapy.all import ARP, IP, Dot1Q, Ether

from packet_io import build_packet, capture, describe_packet


class PacketIoTests(unittest.TestCase):
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

        def receive(**kwargs):
            output.assert_called_once_with(
                'Cattura pronta su h6-eth0: IPv4 protocollo 253 (5s).', flush=True)
            self.assertIs(kwargs['opened_socket'], listen.return_value.__enter__.return_value)
            self.assertEqual(kwargs['timeout'], 5)
            return [pkt for pkt in (valid, unrelated, arp) if kwargs['lfilter'](pkt)]

        sniff.side_effect = receive
        self.assertEqual(capture('h6-eth0', 5), [valid])
        listen.assert_called_once_with(iface='h6-eth0', promisc=True)
        self.assertIn('VLAN: 101', describe_packet(valid))


if __name__ == '__main__':
    unittest.main()
