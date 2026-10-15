"""Tabelle interattive dei contatori OpenFlow per leaf e spine (sola lettura)."""
import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys
import textwrap


CATEGORIES = {
    0x47460010: 'Default DROP',
    0x47460020: 'VLAN host bloccata',
    0x47460021: 'ARP bloccato',
    0x47460030: 'ARP al controller',
    0x47460040: 'IPv4 host / tag tenant',
    0x47460041: 'IPv4 da uplink',
    0x47460042: 'IPv4 destinazione validata',
    0x47460050: 'IPv4 consegna host',
    0x47460051: 'IPv4 uplink singolo',
    0x47460052: 'IPv4 forwarding spine',
}
METADATA = {'cookie', 'duration', 'table', 'n_packets', 'n_bytes',
            'idle_age', 'hard_age', 'idle_timeout', 'hard_timeout', 'priority'}


@dataclass
class Flow:
    cookie: int
    table: int
    priority: int
    packets: int
    byte_count: int
    duration: float
    match: tuple
    actions: str
    raw: str

    @property
    def key(self):
        return self.table, self.priority, self.cookie, tuple(sorted(self.match)), self.actions

    @property
    def category(self):
        if self.cookie == 0x47460021:
            if 'arp_spa=0.0.0.0' in self.match:
                return 'ARP Probe DROP'
            if any(token.startswith('arp_tpa=') for token in self.match):
                return 'ARP gratuito DROP'
        return CATEGORIES.get(self.cookie, f'Cookie {hex(self.cookie)}')


def parse_dump(dump):
    flows = []
    for line in dump.splitlines():
        if not re.search(r'\bcookie=', line):
            continue
        try:
            fields, actions = re.split(r'\s+actions=', line.strip(), maxsplit=1)
            tokens = [token.strip() for token in fields.split(',')]
            values = dict(token.split('=', 1) for token in tokens if '=' in token)
            match = tuple(token for token in tokens
                          if token.split('=', 1)[0] not in METADATA)
            flows.append(Flow(int(values['cookie'], 0), int(values['table']),
                              int(values['priority']), int(values['n_packets']),
                              int(values['n_bytes']), float(values['duration'].rstrip('s')),
                              match, actions.strip(), line.strip()))
        except (KeyError, ValueError) as error:
            raise ValueError(f'Riga OpenFlow non valida: {line.strip()}') from error
    if not flows:
        raise ValueError('Il dump non contiene regole OpenFlow con contatori.')
    return sorted(flows, key=lambda f: (f.table, -f.priority, f.match))


def run_ovs(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except FileNotFoundError as error:
        raise RuntimeError(f'{command[0]} non trovato: installare Open vSwitch.') from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Open vSwitch non risponde entro 10 secondi.') from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Comando Open vSwitch fallito.')
    return result.stdout


def available_switches():
    names = run_ovs(['ovs-vsctl', 'list-br']).splitlines()
    return sorted((name for name in names if re.fullmatch(r'(leaf|spine)\d+', name)),
                  key=lambda name: (name.startswith('spine'), int(re.search(r'\d+', name)[0])))


def choose_switch():
    switches = available_switches()
    if not switches:
        raise RuntimeError('Nessun bridge leaf/spine: avviare prima la rete Mininet.')
    print('\nSwitch disponibili:')
    for index, name in enumerate(switches, 1):
        print(f'  {index}. {name}')
    while True:
        choice = input('Scegli numero o nome (q = esci): ').strip()
        if choice == 'q':
            return None
        if choice in switches:
            return choice
        if choice.isdigit() and 1 <= int(choice) <= len(switches):
            return switches[int(choice) - 1]
        print('Scelta non valida.')


def readable_match(flow):
    labels = {'in_port': 'porta', 'dl_src': 'MAC sorgente', 'dl_dst': 'MAC destinazione',
              'nw_src': 'IP sorgente', 'nw_dst': 'IP destinazione',
              'arp_spa': 'IP ARP sorgente', 'arp_tpa': 'IP ARP richiesto',
              'arp_sha': 'MAC ARP sorgente', 'arp_tha': 'MAC ARP destinatario',
              'dl_vlan': 'VLAN', 'dl_type': 'EtherType'}
    parts = []
    for token in flow.match:
        if token == 'vlan_tci=0x0000/0x1fff':
            parts.append('senza VLAN')
        elif token == 'vlan_tci=0x1000/0x1000':
            parts.append('VLAN presente')
        elif token == 'arp_op=1':
            parts.append('ARP Request')
        elif '=' in token:
            key, value = token.split('=', 1)
            parts.append(f'{labels.get(key, key)}={value.strip(chr(34))}')
        else:
            parts.append({'ip': 'IPv4', 'arp': 'ARP', 'ipv6': 'IPv6'}.get(token, token))
    return ', '.join(parts) or 'qualsiasi pacchetto (table-miss)'


def readable_actions(actions):
    if actions == 'drop':
        return 'DROP'
    parts = []
    for action in actions.split(','):
        if action.startswith('CONTROLLER:'):
            parts.append('Invia al controller')
        elif action.startswith('goto_table:'):
            parts.append('Vai a tabella ' + action.split(':', 1)[1])
        elif action.startswith('push_vlan:'):
            parts.append('Aggiungi tag VLAN')
        elif action == 'pop_vlan':
            parts.append('Rimuovi tag VLAN')
        elif action.startswith('output:'):
            parts.append('Invia a porta ' + action.split(':', 1)[1])
        elif re.fullmatch(r'set_field:\d+->vlan_vid', action):
            value = int(action.split(':')[1].split('->')[0])
            parts.append(f'VLAN {value & 0xfff}')
        else:
            parts.append(action)
    return '; '.join(parts)


def delta(flow, baseline):
    before = baseline.get(flow.key)
    if before is None:
        return 'nuova', 'nuova'
    if (flow.duration < before.duration or flow.packets < before.packets
            or flow.byte_count < before.byte_count):
        return 'reset', 'reset'
    return flow.packets - before.packets, flow.byte_count - before.byte_count


def table(headers, rows, widths):
    def row_lines(row):
        cells = [textwrap.wrap(str(cell), width, break_long_words=True,
                               break_on_hyphens=False) or ['']
                 for cell, width in zip(row, widths)]
        return [' | '.join((cell[i] if i < len(cell) else '').ljust(width)
                          for cell, width in zip(cells, widths))
                for i in range(max(map(len, cells)))]
    print('\n'.join(row_lines(headers)))
    print('-+-'.join('-' * width for width in widths))
    for row in rows:
        print('\n'.join(row_lines(row)))


def show(switch, flows, baseline, active_only=False):
    if sys.stdout.isatty():
        print('\033[2J\033[H', end='')
    print(f'{switch} | {datetime.now().strftime("%H:%M:%S")} | {len(flows)} regole')
    print('Delta dal riferimento salvato. "reset": contatore diminuito o regola reinstallata.')
    print('I contatori misurano passaggi nelle regole, non pacchetti unici tra tabelle.')
    print('ARP al controller conta richieste, non le reply del proxy.')
    grouped = defaultdict(list)
    for flow in flows:
        grouped[flow.table, flow.category].append(flow)
    rows = []
    for (table_id, category), group in sorted(grouped.items()):
        changes = [delta(flow, baseline)[0] for flow in group]
        change = sum(changes) if all(isinstance(x, int) for x in changes) else 'nuova/reset'
        rows.append((table_id, category, len(group), sum(f.packets for f in group),
                     sum(f.byte_count for f in group), change))
    print('\nRIEPILOGO PER TABELLA E TIPO DI REGOLA')
    table(('Tab', 'Regola', 'N.', 'Pacchetti', 'Byte', 'Delta pkt'), rows,
          (3, 27, 3, 10, 10, 12))
    print('\nREGOLE' + (' CON PACCHETTI' if active_only else ' (tutte)'))
    rows = []
    for index, flow in enumerate(flows, 1):
        if active_only and not flow.packets:
            continue
        port = next((t.split('=', 1)[1].strip('"') for t in flow.match
                     if t.startswith('in_port=')), '*')
        rows.append((index, flow.table, flow.priority, port, flow.category,
                     flow.packets, flow.byte_count, delta(flow, baseline)[0]))
    table(('ID', 'Tab', 'Prio', 'Porta', 'Regola', 'Pkt', 'Byte', 'Delta pkt'), rows,
          (3, 3, 4, 12, 27, 8, 10, 12))
    if not rows:
        print('Nessuna regola con pacchetti.')
    print('\nInserisci un ID per match, azioni e delta byte della singola regola.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--switch', help='Bridge da leggere, per esempio leaf1 o spine1')
    parser.add_argument('--file', type=Path, help='Leggi un dump salvato, senza OVS o sudo')
    parser.add_argument('--once', action='store_true', help='Stampa una sola schermata')
    parser.add_argument('--active', action='store_true', help='Mostra solo regole con pacchetti')
    args = parser.parse_args()
    if args.switch and not re.fullmatch(r'(leaf|spine)\d+', args.switch):
        parser.error('--switch deve essere leafN o spineN')
    try:
        switch = args.switch or ('Dump da file' if args.file else choose_switch())
        if switch is None:
            return 0
        def read():
            dump = (args.file.read_text() if args.file else
                    run_ovs(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', switch]))
            return parse_dump(dump)
        flows = read()
        baseline = {f.key: f for f in flows}
        active = args.active
        while True:
            show(switch, flows, baseline, active)
            if args.once or not sys.stdin.isatty():
                return 0
            command = input('\nr aggiorna | b riferimento prima del test | a tutte/attive | '
                            's cambia switch | ID dettaglio | q esci\n> ').strip().lower()
            if command == 'q':
                return 0
            try:
                if command == 'r':
                    flows = read()
                elif command == 'b':
                    flows = read()
                    baseline = {f.key: f for f in flows}
                elif command == 'a':
                    active = not active
                elif command == 's' and not args.file:
                    selected = choose_switch()
                    if selected is None:
                        return 0
                    # Confermare la nuova lettura prima di cambiare selezione.
                    new_flows = parse_dump(run_ovs([
                        'ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', selected]))
                    switch, flows = selected, new_flows
                    baseline = {f.key: f for f in flows}
                elif command.isdigit() and 1 <= int(command) <= len(flows):
                    flow = flows[int(command) - 1]
                    dp, db = delta(flow, baseline)
                    print(f'\nRegola {command}: {flow.category} | cookie {hex(flow.cookie)}')
                    print(f'Tabella {flow.table}, priorita {flow.priority}, durata {flow.duration}s')
                    print('MATCH: ' + readable_match(flow))
                    print('AZIONI: ' + readable_actions(flow.actions))
                    print(f'Pacchetti: {flow.packets} (delta {dp}); byte: {flow.byte_count} (delta {db})')
                    print('OVS originale: ' + flow.raw)
                    input('\nInvio per tornare alle tabelle...')
                else:
                    input('Comando non valido. Invio per continuare...')
            except (RuntimeError, ValueError, OSError) as error:
                print(f'Errore: {error}\nLa schermata precedente resta disponibile.')
                input('Invio per continuare...')
    except (RuntimeError, ValueError, OSError) as error:
        print(f'Errore: {error}', file=sys.stderr)
        if not args.file:
            print('Avviare la rete e usare sudo per accedere a OVS.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nChiusura visualizzatore.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
