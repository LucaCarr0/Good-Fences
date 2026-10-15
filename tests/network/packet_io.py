"""Invio e cattura di frame dalla CLI Mininet per verificare la pipeline."""
import argparse

from scapy.all import ARP, IP, UDP, Dot1Q, Ether, conf, sniff


CASES = ('arp-request', 'arp-probe', 'gratuitous-arp', 'rarp',
         'ipv4-tagged', 'ipv4-untagged', 'ecmp-udp')

ECMP_FIRST_SOURCE_PORT = 40000
ECMP_DESTINATION_PORT = 9000


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


def build_ecmp_packets(mac, ip, target_ip, target_mac, flows):
    """Genera un frame per flusso UDP, variando la porta sorgente."""
    for index in range(flows):
        yield (Ether(src=mac, dst=target_mac)
               / IP(src=ip, dst=target_ip)
               / UDP(sport=ECMP_FIRST_SOURCE_PORT + index,
                     dport=ECMP_DESTINATION_PORT))


def send_ecmp_packets(iface, packets, packets_per_flow):
    """Invia ogni flusso sullo stesso socket e conta gli invii completati."""
    sent = 0
    with conf.L2socket(iface=iface) as sender:
        for packet in packets:
            for _ in range(packets_per_flow):
                sender.send(packet)
                sent += 1
    return sent


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


def parse_args(argv=None):
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
    parser.add_argument('--flows', type=int,
                        help='Flussi UDP del caso ecmp-udp (default: 100)')
    parser.add_argument('--packets-per-flow', type=int,
                        help='Pacchetti per flusso di ecmp-udp (default: 10)')
    args = parser.parse_args(argv)
    if args.case == 'ecmp-udp':
        if args.flows is None:
            args.flows = 100
        if args.packets_per_flow is None:
            args.packets_per_flow = 10
        if args.flows <= 0 or args.packets_per_flow <= 0:
            parser.error('--flows e --packets-per-flow devono essere positivi')
        if ECMP_FIRST_SOURCE_PORT + args.flows - 1 > 65535:
            parser.error('--flows genera porte UDP sorgenti oltre 65535')
    elif args.flows is not None or args.packets_per_flow is not None:
        parser.error('--flows e --packets-per-flow richiedono --case ecmp-udp')
    if args.timeout is None:
        args.timeout = 5 if args.listen else 1
    if args.timeout <= 0:
        parser.error('--timeout deve essere positivo')
    return args


def main(argv=None):
    args = parse_args(argv)
    timeout = args.timeout
    if args.listen:
        packets = capture(args.iface, timeout)
        print(f'Frame IPv4 protocollo 253 catturati: {len(packets)}')
        if not packets:
            print(f'  Nessun frame catturato entro {timeout:g} secondi.')
        for index, packet in enumerate(packets, start=1):
            print(f'Frame {index}:')
            print(describe_packet(packet))
        return
    if args.case == 'ecmp-udp':
        last_port = ECMP_FIRST_SOURCE_PORT + args.flows - 1
        print(f'Caso: {args.case} | Interfaccia: {args.iface}')
        print(f'Ethernet: {args.mac} -> {args.target_mac}')
        print(f'IPv4: {args.ip} -> {args.target_ip}')
        print(f'Porte UDP: {ECMP_FIRST_SOURCE_PORT}-{last_port}'
              f' -> {ECMP_DESTINATION_PORT}')
        print(f'Traffico offerto: {args.flows} flussi UDP,'
              f' {args.packets_per_flow} pacchetti per flusso'
              f' ({args.flows * args.packets_per_flow} pacchetti).', flush=True)
        packets = build_ecmp_packets(args.mac, args.ip, args.target_ip,
                                    args.target_mac, args.flows)
        sent = send_ecmp_packets(args.iface, packets, args.packets_per_flow)
        print(f'Pacchetti effettivamente inviati: {sent}')
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
