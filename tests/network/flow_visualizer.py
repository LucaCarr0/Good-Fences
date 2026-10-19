"""Tabelle interattive di flow e bucket OpenFlow per leaf e spine (sola lettura)."""
from __future__ import annotations

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
    0x47460051: 'IPv4 remoto / ECMP',
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


@dataclass(frozen=True)
class Bucket:
    weight: int
    actions: str
    packets: int | None = None
    byte_count: int | None = None


@dataclass(frozen=True)
class Group:
    group_id: int
    kind: str
    buckets: tuple[Bucket, ...]
    duration: float | None = None
    packets: int | None = None
    byte_count: int | None = None

    @property
    def definition(self):
        # In OpenFlow 1.3 i contatori identificano i bucket per posizione.
        return self.kind, tuple((bucket.weight, bucket.actions) for bucket in self.buckets)


@dataclass(frozen=True)
class GroupSnapshot:
    groups: tuple[Group, ...] = ()
    error: str | None = None


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


def parse_groups(dump):
    groups = []
    for line in dump.splitlines():
        if not re.search(r'\bgroup_id=', line):
            continue
        try:
            header, *descriptions = re.split(r',\s*bucket=', line.strip())
            fields = dict(token.strip().split('=', 1)
                          for token in header.split(',') if '=' in token)
            buckets = []
            for description in descriptions:
                settings, actions = description.split('actions=', 1)
                weight = re.search(r'(?:^|,)\s*weight[:=](\d+)', settings)
                buckets.append(Bucket(int(weight[1]) if weight else 1, actions.strip()))
            groups.append(Group(int(fields['group_id'], 0), fields['type'], tuple(buckets)))
        except (KeyError, ValueError) as error:
            raise ValueError(f'Descrizione gruppo non valida: {line.strip()}') from error
    if not groups and 'OFPST_GROUP_DESC' not in dump:
        raise ValueError('Il dump non contiene descrizioni dei gruppi OpenFlow.')
    if len({group.group_id for group in groups}) != len(groups):
        raise ValueError('Il dump contiene ID di gruppo duplicati.')
    return sorted(groups, key=lambda group: group.group_id)


def parse_counter(value):
    # OVS usa "?" quando lo switch non offre un contatore.
    if value in (None, '?', 'unknown', '-1', str(2**64 - 1)):
        return None
    counter = int(value)
    if counter < 0:
        raise ValueError('Contatore negativo.')
    return counter


def counter_fields(fields):
    values = dict(token.strip().split('=', 1)
                  for token in fields.split(',') if '=' in token)
    return parse_counter(values.get('packet_count')), parse_counter(values.get('byte_count'))


def parse_group_stats(dump, groups):
    """Associa i bucket dei contatori alle descrizioni, nello stesso ordine OF1.3."""
    statistics = {}
    for line in dump.splitlines():
        if not re.search(r'\bgroup_id=', line):
            continue
        try:
            header, *bucket_fields = re.split(r',\s*bucket(\d+):', line.strip())
            values = dict(token.strip().split('=', 1)
                          for token in header.split(',') if '=' in token)
            group_id = int(values['group_id'], 0)
            if group_id in statistics:
                raise ValueError('ID di gruppo duplicato.')
            bucket_counts = []
            for index, fields in zip(bucket_fields[::2], bucket_fields[1::2]):
                if int(index) != len(bucket_counts):
                    raise ValueError('Ordine dei bucket non valido.')
                bucket_counts.append(counter_fields(fields))
            duration = values.get('duration')
            statistics[group_id] = (float(duration.rstrip('s')) if duration else None,
                                    counter_fields(header), bucket_counts)
        except (KeyError, ValueError) as error:
            raise ValueError(f'Contatori gruppo non validi: {line.strip()}') from error
    if not statistics and 'OFPST_GROUP' not in dump:
        raise ValueError('Il dump non contiene contatori dei gruppi OpenFlow.')
    result = []
    for group in groups:
        duration, totals, counters = statistics.get(group.group_id, (None, (None, None), []))
        if counters and len(counters) != len(group.buckets):
            raise ValueError(f'Il gruppo {group.group_id} ha cambiato bucket durante la lettura.')
        buckets = tuple(Bucket(bucket.weight, bucket.actions, *counts)
                        for bucket, counts in zip(group.buckets, counters))
        result.append(Group(group.group_id, group.kind, buckets or group.buckets,
                            duration, *totals))
    return tuple(result)


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


def read_state(switch, source_file=None):
    dump = (source_file.read_text() if source_file else
            run_ovs(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', switch]))
    flows = parse_dump(dump)
    if source_file:
        return flows, None
    try:
        groups = parse_groups(run_ovs([
            'ovs-ofctl', '-O', 'OpenFlow13', 'dump-groups', switch]))
        stats = run_ovs(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-group-stats', switch])
        return flows, GroupSnapshot(parse_group_stats(stats, groups))
    except (RuntimeError, ValueError) as error:
        # Un errore dei gruppi non deve nascondere le tabelle dei flow.
        return flows, GroupSnapshot(error=str(error))


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
        elif action.startswith('group:'):
            parts.append('Usa gruppo ' + action.split(':', 1)[1])
        elif action.startswith('meter:'):
            parts.append('Applica meter ' + action.split(':', 1)[1])
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


def bucket_deltas(group, before):
    """Un confronto non valido invalida anche la percentuale dell'intero gruppo."""
    if before is None:
        return None, 'nuovo gruppo / riferimento assente'
    if group.definition != before.definition:
        return None, 'gruppo modificato: salvare un nuovo riferimento con b'
    if (group.duration is not None and before.duration is not None
            and group.duration < before.duration):
        return None, 'reset: gruppo reinstallato'
    totals = ((group.packets, before.packets), (group.byte_count, before.byte_count))
    if any(after is not None and old is not None and after < old for after, old in totals):
        return None, 'reset: contatori diminuiti'
    changes = []
    for bucket, old in zip(group.buckets, before.buckets):
        if None in (bucket.packets, old.packets, bucket.byte_count, old.byte_count):
            return None, 'contatori bucket non disponibili'
        packets = bucket.packets - old.packets
        byte_count = bucket.byte_count - old.byte_count
        if packets < 0 or byte_count < 0:
            return None, 'reset: contatori bucket diminuiti'
        changes.append((packets, byte_count))
    return changes, None


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


def show_groups(snapshot, baseline):
    print('\nGRUPPI E BUCKET DELLO SWITCH')
    if snapshot.error:
        print(f'Errore nella lettura dei gruppi: {snapshot.error}')
        return
    if not snapshot.groups:
        print('Nessun gruppo installato.')
        return
    previous = ({group.group_id: group for group in baseline.groups}
                if baseline and not baseline.error else {})
    print('Prima = riferimento b; dopo = ultima lettura. n/d = contatore non disponibile.')
    print('Quota = delta pacchetti del bucket / somma dei delta del gruppo; non implica 50/50.')
    for group in snapshot.groups:
        before = previous.get(group.group_id)
        changes, error = bucket_deltas(group, before)
        total = sum(packets for packets, _ in changes) if changes is not None else 0
        print(f'\nGruppo {group.group_id} | tipo {group.kind} | {len(group.buckets)} bucket')
        if error:
            print(f'Delta e quote non disponibili: {error}.')
        elif not total:
            print('Nessun incremento dei pacchetti: quote non calcolate.')
        rows = []
        for index, bucket in enumerate(group.buckets):
            old = before.buckets[index] if before and index < len(before.buckets) else None
            packets, byte_count = changes[index] if changes is not None else ('n/d', 'n/d')
            percentage = f'{100 * packets / total:.1f}%' if total else 'n/d'
            rows.append((index, bucket.weight, readable_actions(bucket.actions),
                         old.packets if old else None, bucket.packets, packets,
                         old.byte_count if old else None, bucket.byte_count, byte_count, percentage))
        rows = [tuple('n/d' if cell is None else cell for cell in row) for row in rows]
        table(('Bucket', 'Peso', 'Azioni / uscita', 'Pkt prima', 'Pkt dopo', 'Delta pkt',
               'Byte prima', 'Byte dopo', 'Delta byte', 'Quota pkt'), rows,
              (6, 4, 16, 10, 10, 10, 10, 10, 10, 9))


def show(switch, flows, baseline, active_only=False, groups=None, group_baseline=None):
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
    if groups is not None:
        show_groups(groups, group_baseline)
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
        flows, groups = read_state(switch, args.file)
        baseline = {f.key: f for f in flows}
        group_baseline = groups
        active = args.active
        while True:
            show(switch, flows, baseline, active, groups, group_baseline)
            if args.once or not sys.stdin.isatty():
                return 0
            command = input('\nr aggiorna | b riferimento prima del test | a tutte/attive | '
                            's cambia switch | ID dettaglio | q esci\n> ').strip().lower()
            if command == 'q':
                return 0
            try:
                if command == 'r':
                    flows, groups = read_state(switch, args.file)
                elif command == 'b':
                    flows, groups = read_state(switch, args.file)
                    baseline = {f.key: f for f in flows}
                    group_baseline = groups
                elif command == 'a':
                    active = not active
                elif command == 's' and not args.file:
                    selected = choose_switch()
                    if selected is None:
                        return 0
                    # Confermare la nuova lettura prima di cambiare selezione.
                    new_flows, new_groups = read_state(selected)
                    switch, flows, groups = selected, new_flows, new_groups
                    baseline = {f.key: f for f in flows}
                    group_baseline = groups
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
