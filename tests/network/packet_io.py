"""Invio e cattura di frame dalla CLI Mininet per verificare la pipeline."""
import argparse

from scapy.all import ARP, IP, Dot1Q, Ether, conf, sniff


CASES = ('arp-request', 'arp-probe', 'gratuitous-arp', 'rarp',
         'ipv4-tagged', 'ipv4-untagged')


def build_packet(case, mac, ip, target_ip, target_mac, vlan=102):
    if case.startswith('ipv4-'):
        frame = Ether(src=mac, dst=target_mac)
        if case == 'ipv4-tagged':
            frame /= Dot1Q(vlan=vlan)
        # Protocollo sperimentale: non genera risoluzione ARP nel kernel.
        return frame / IP(src=ip, dst=target_ip, proto=253)

    request = ARP(op=1, hwsrc=mac, psrc=ip, pdst=target_ip,
                  hwdst='00:00:00:00:00:00')
    ethertype = 0x0806
    if case == 'arp-probe':
        request.psrc = '0.0.0.0'
    elif case == 'gratuitous-arp':
        request.pdst = ip
    elif case == 'rarp':
        ethertype = 0x8035
        request.op = 3
        request.psrc = request.pdst = '0.0.0.0'
        request.hwdst = mac
    elif case != 'arp-request':
        raise ValueError(case)
    return Ether(src=mac, dst='ff:ff:ff:ff:ff:ff', type=ethertype) / request


def exchange(iface, frame, timeout=1):
    # Aprire la cattura PRIMA dell'invio evita di perdere reply immediate.
    with conf.L2listen(iface=iface) as receiver:
        with conf.L2socket(iface=iface) as sender:
            sender.send(frame)
        packets = sniff(opened_socket=receiver, timeout=timeout)
    return [pkt for pkt in packets
            if ARP in pkt and pkt[ARP].op in (2, 4)]


def capture(iface, timeout):
    # Sul destinatario, promisc include anche eventuali consegne al MAC errato.
    with conf.L2listen(iface=iface, promisc=True) as receiver:
        print(f'Cattura pronta su {iface}: IPv4 protocollo 253 ({timeout:g}s).',
              flush=True)
        return sniff(opened_socket=receiver, timeout=timeout,
                     lfilter=lambda pkt: IP in pkt and pkt[IP].proto == 253)


def describe_packet(packet):
    eth = packet[Ether]
    lines = [f'  Ethernet: {eth.src} -> {eth.dst}']
    lines.append(f'  VLAN: {packet[Dot1Q].vlan}' if Dot1Q in packet
                 else '  VLAN: assente (untagged)')
    if ARP in packet:
        arp = packet[ARP]
        operation = {1: 'ARP Request', 2: 'ARP Reply',
                     3: 'RARP Request', 4: 'RARP Reply'}.get(
                         int(arp.op), f'ARP opcode {arp.op}')
        lines.extend([f'  Operazione: {operation}',
                      f'  Mittente: IP {arp.psrc}, MAC {arp.hwsrc}',
                      f'  Destinatario: IP {arp.pdst}, MAC {arp.hwdst}'])
        if arp.op == 1:
            lines.append(f'  Richiesta: chi ha {arp.pdst}? Rispondi a {arp.psrc}.')
        elif arp.op == 2:
            lines.append(f'  Risposta: {arp.psrc} ha MAC {arp.hwsrc}.')
    elif IP in packet:
        ip = packet[IP]
        lines.extend([f'  IPv4: {ip.src} -> {ip.dst}',
                      f'  Protocollo IP: {ip.proto}'])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iface', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--case', choices=CASES)
    mode.add_argument('--listen', action='store_true',
                      help='Cattura sul destinatario prima di inviare da un altro host')
    parser.add_argument('--timeout', type=float,
                        help='Secondi di cattura (default: 5 in ascolto, 1 per le reply)')
    parser.add_argument('--mac', default='00:00:00:00:00:02')
    parser.add_argument('--ip', default='10.0.0.1')
    parser.add_argument('--target-ip', default='10.0.0.2')
    parser.add_argument('--target-mac', default='00:00:00:00:00:06')
    parser.add_argument('--vlan', type=int, default=102)
    args = parser.parse_args()
    timeout = args.timeout if args.timeout is not None else (5 if args.listen else 1)
    if timeout <= 0:
        parser.error('--timeout deve essere positivo')
    if args.listen:
        packets = capture(args.iface, timeout)
        print(f'Frame IPv4 protocollo 253 catturati: {len(packets)}')
        if not packets:
            print(f'  Nessun frame catturato entro {timeout:g} secondi.')
        for index, packet in enumerate(packets, start=1):
            print(f'Frame {index}:')
            print(describe_packet(packet))
        return
    frame = build_packet(args.case, args.mac, args.ip, args.target_ip,
                         args.target_mac, args.vlan)
    print(f'Caso: {args.case} | Interfaccia: {args.iface}')
    print('Pacchetto inviato:')
    print(describe_packet(frame), flush=True)
    if IP in frame:
        with conf.L2socket(iface=args.iface) as sender:
            sender.send(frame)
        print('Consegna verificabile con --listen avviato prima sul destinatario.')
        return
    replies = exchange(args.iface, frame, timeout)
    print(f'Risposte ARP/RARP ricevute: {len(replies)}')
    if not replies:
        print(f'  Nessuna risposta ricevuta entro {timeout:g} secondi.')
    for index, reply in enumerate(replies, start=1):
        print(f'Risposta {index}:')
        print(describe_packet(reply))


if __name__ == '__main__':
    main()
