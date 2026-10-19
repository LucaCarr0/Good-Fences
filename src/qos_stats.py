"""Letture meter OVS e delta: nessun dato mancante viene sostituito con zero."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re

from tests.network.flow_visualizer import parse_counter, run_ovs


@dataclass(frozen=True)
class Meter:
    meter_id: int
    flags: tuple
    bands: tuple  # (type, rate, burst_size)
    flow_count: int | None = None
    packets: int | None = None
    byte_count: int | None = None
    drop_packets: int | None = None
    drop_bytes: int | None = None
    duration: float | None = None

    @property
    def definition(self):
        return self.flags, self.bands


@dataclass(frozen=True)
class MeterSnapshot:
    meters: tuple = ()
    features: str = ''
    error: str | None = None
    raw_config: str = ''
    raw_stats: str = ''


def fields(line):
    return dict(re.findall(r'([a-z_]+)[:=]([^\s,]+)', line))


def parse_meters(config, stats, features=''):
    if 'OFPST_METER_CONFIG' not in config or 'OFPST_METER' not in stats:
        raise ValueError('Risposta meter OVS non riconosciuta.')
    definitions = {}
    # ovs-ofctl può stampare ogni banda su una riga successiva al meter.
    normalized = re.sub(r'\n\s*(type=)', r' \1', config)
    for line in normalized.splitlines():
        if not re.search(r'\bmeter=', line):
            continue
        header, *bands = re.split(r'\btype=', line)
        settings = fields(header)
        meter_id = int(settings['meter'], 0)
        if meter_id in definitions:
            raise ValueError('ID meter duplicato.')
        parsed = []
        for band in bands:
            values = fields(band)
            parsed.append((re.split(r'[\s,]', band)[0], int(values['rate']),
                           int(values.get('burst_size', '0'))))
        if not parsed:
            raise ValueError(f'Bande mancanti per meter {meter_id}.')
        flags = tuple(sorted(set(re.findall(r'\b(kbps|pktps|burst|stats)\b', header))))
        definitions[meter_id] = Meter(meter_id, flags, tuple(parsed))
    counters, current = {}, None
    for line in stats.splitlines():
        values = fields(line)
        if 'meter' in values:
            current = int(values['meter'], 0)
            if current in counters:
                raise ValueError('Statistiche meter duplicate.')
            counters[current] = dict(
                flow_count=parse_counter(values.get('flow_count')),
                packets=parse_counter(values.get('packet_in_count')),
                byte_count=parse_counter(values.get('byte_in_count')),
                duration=float(values['duration'].rstrip('s')) if 'duration' in values else None)
        elif re.match(r'\s*\d+:', line) and current is not None:
            # Questi meter hanno una sola banda DROP; altre configurazioni restano visibili.
            if len(definitions.get(current, Meter(0, (), ())).bands) == 1:
                counters[current].update(drop_packets=parse_counter(values.get('packet_count')),
                                         drop_bytes=parse_counter(values.get('byte_count')))
    if set(counters) != set(definitions):
        raise ValueError('Meter cambiati tra descrizione e statistiche: aggiornare la lettura.')
    return MeterSnapshot(tuple(replace(meter, **counters[key])
                               for key, meter in sorted(definitions.items())), features,
                         raw_config=config, raw_stats=stats)


def read_meters(switch):
    features = config = stats = ''
    try:
        command = ['ovs-ofctl', '-O', 'OpenFlow13']
        features = run_ovs(command + ['meter-features', switch])
        config = run_ovs(command + ['dump-meters', switch])
        stats = run_ovs(command + ['meter-stats', switch])
        return parse_meters(config, stats, features)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return MeterSnapshot(features=features, error=str(error), raw_config=config, raw_stats=stats)


def meter_delta(after, before):
    names = ('packets', 'byte_count', 'drop_packets', 'drop_bytes')
    if before is None:
        return None, 'riferimento assente'
    if after.definition != before.definition:
        return None, 'configurazione cambiata'
    if any(getattr(meter, name) is None for meter in (after, before)
           for name in (*names, 'duration')):
        return None, 'contatori indisponibili'
    if after.duration < before.duration or any(getattr(after, n) < getattr(before, n) for n in names):
        return None, 'reset'
    result = {name: getattr(after, name) - getattr(before, name) for name in names}
    if result['drop_packets'] > result['packets'] or result['drop_bytes'] > result['byte_count']:
        return None, 'contatori incoerenti'
    return result, None


def meter_rows(snapshot, baseline=None):
    if not snapshot.meters or snapshot.meters.error:
        return []
    previous = ({m.meter_id: m for m in baseline.meters.meters}
                if baseline and baseline.meters and not baseline.meters.error else {})
    rows = []
    for meter in snapshot.meters.meters:
        changes, error = meter_delta(meter, previous.get(meter.meter_id))
        refs = [flow for flow in snapshot.flows
                if re.search(rf'(?:^|,)meter:{meter.meter_id}(?:,|$)', flow.actions)]
        rows.append({'Meter': meter.meter_id, 'Flag': ', '.join(meter.flags),
                     'Bande (tipo, kbit/s, kbit)': str(meter.bands),
                     'Flow osservati': len(refs), 'Flow dichiarati': meter.flow_count,
                     'Ingressi pkt': meter.packets, 'Ingressi byte': meter.byte_count,
                     'DROP pkt': meter.drop_packets, 'DROP byte': meter.drop_bytes,
                     'Delta ingressi pkt': changes['packets'] if changes else None,
                     'Delta DROP pkt': changes['drop_packets'] if changes else None,
                     'Confronto': error or 'valido'})
    return rows
