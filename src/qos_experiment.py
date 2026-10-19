"""Esperimenti locali QoS condivisi da CLI e Streamlit, senza servizi HTTP."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import csv
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid

from src.qos_stats import meter_delta
from src.visualizer_support import ROOT, ManagedLab, load_model, read_switch
from tests.network.flow_visualizer import run_ovs


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=keys or ['status'])
        writer.writeheader()
        writer.writerows(rows)


def summarize_stream(sent, received):
    rows = []
    if len(sent['intervals']) != len(received['intervals']):
        raise ValueError('Finestre sender/receiver diverse.')
    for tx, rx in zip(sent['intervals'], received['intervals']):
        if tx['second'] != rx['second'] or rx['packets'] > tx['packets']:
            raise ValueError('Contatori UDP incoerenti.')
        rows.append({'second': tx['second'], 'sent_packets': tx['packets'],
                     'received_packets': rx['packets'], 'goodput_mbps': rx['bytes'] * 8 / 1e6,
                     'sent_mbps': tx['bytes'] * 8 / 1e6,
                     'loss_pct': 100 * (tx['packets'] - rx['packets']) / tx['packets']
                                 if tx['packets'] else None,
                     'jitter_ms': rx['jitter_ms']})
    return rows


def congestion_evidence(samples, interfaces, capacity):
    """Richiede utilizzo >=90% e drop, oppure tre letture consecutive con backlog.

    Valuta ciascuna qdisc separatamente: parent e child non sono sommati.
    """
    evidence = []
    for interface in interfaces:
        streaks = {}
        for before, after in zip(samples, samples[1:]):
            a, b = before['links'].get(interface), after['links'].get(interface)
            if not a or not b:
                continue
            elapsed = b['at'] - a['at']
            tx = b['tx_bytes'] - a['tx_bytes']
            if elapsed <= 0 or tx < 0:
                continue
            utilization = tx * 8 / elapsed / (capacity * 1e6)
            old = {(q['handle'], q['kind']): q for q in a['qdiscs']}
            for queue in b['qdiscs']:
                key = queue['handle'], queue['kind']
                previous = old.get(key)
                if previous is None or queue.get('drops') is None or previous.get('drops') is None:
                    streaks[key] = 0
                    continue
                drops = queue['drops'] - previous['drops']
                if drops < 0 or queue.get('bytes', 0) < previous.get('bytes', 0):
                    streaks[key] = 0
                    continue
                busy = utilization >= .9
                streaks[key] = streaks.get(key, 0) + 1 if busy and queue.get('backlog', 0) > 0 else 0
                if busy and (drops > 0 or streaks[key] >= 3):
                    evidence.append({'interface': interface, 'at': b['at'],
                                     'utilization': utilization, 'qdisc': key,
                                     'drops': drops, 'backlog': queue.get('backlog')})
    return evidence


def limit_check(before, after, elapsed, rate_mbps, payload=1200):
    changes, error = meter_delta(after, before)
    if error or elapsed <= 0:
        return {'limit_status': 'incompleto', 'limit_reason': error or 'durata nulla'}
    if after.flags != ('burst', 'kbps', 'stats') or after.bands != (('drop', rate_mbps * 1000,
                                                                           rate_mbps * 100),):
        return {'limit_status': 'fallito', 'limit_reason': 'configurazione meter diversa dal modello'}
    admitted = (changes['byte_count'] - changes['drop_bytes']) * 8 / elapsed / 1e6
    upper = rate_mbps * 1.05 + after.bands[0][2] / (1000 * elapsed) + (payload + 42) * 8 / elapsed / 1e6
    valid = (changes['packets'] > 0 and changes['drop_packets'] > 0
             and after.flow_count is not None and after.flow_count > 0
             and rate_mbps * .95 <= admitted <= upper)
    return {'limit_status': 'verificato' if valid else 'fallito',
            'admitted_meter_mbps': admitted, 'limit_upper_mbps': upper,
            'meter_input_packets': changes['packets'], 'meter_drop_packets': changes['drop_packets'],
            'meter_elapsed_s': elapsed}


def aggregate_trials(trials):
    grouped = {}
    for trial in trials:
        key = tuple(trial[k] for k in ('scenario', 'mode', 'source', 'target', 'offered_mbps'))
        grouped.setdefault(key, []).append(trial)
    rows = []
    for key, group in grouped.items():
        row = dict(zip(('scenario', 'mode', 'source', 'target', 'offered_mbps'), key))
        valid = [t for t in group if t['validity'] == 'valida' and t.get('pair_validity', 'valida') == 'valida']
        row.update(n_total=len(group), n_valid=len(valid))
        for metric in ('goodput_mbps', 'loss_pct', 'rtt_mean_ms', 'jitter_mean_ms', 'throughput_sd_mbps'):
            values = [t[metric] for t in valid if t.get(metric) is not None]
            row[metric + '_mean'] = statistics.mean(values) if values else None
            row[metric + '_between_trial_sd'] = statistics.stdev(values) if len(values) > 1 else None
        rows.append(row)
    return rows


class Experiment:
    def __init__(self, lab, scenario='isolated', source='h1', target='h5', matrix=False,
                 duration=30, repetitions=6, seed=42, noise_mbps=90, output=None):
        if scenario not in ('isolated', 'noisy-neighbor'):
            raise ValueError('Esperimento sconosciuto.')
        if (type(duration) is not int or not 3 <= duration <= 300
                or type(repetitions) is not int or not 2 <= repetitions <= 20
                or repetitions % 2 or not 1 <= noise_mbps <= 95):
            raise ValueError('Durata 3–300 s, ripetizioni pari 2–20, rumore 1–95 Mbps.')
        self.lab, self.model = lab, load_model()
        self.scenario, self.source, self.target, self.matrix = scenario, source, target, matrix
        self.duration, self.repetitions, self.seed = duration, repetitions, seed
        self.noise_mbps, self.warmup = noise_mbps, 3
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self.run, name='good-fences-qos', daemon=True)
        self._lock = threading.Lock()
        self._status = {'state': 'pending', 'message': 'In attesa', 'completed': 0}
        self.processes, self.rows, self.trials, self.pairs, self.meter_checks = [], [], [], [], []
        self.run_id = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        self.path = Path(output).resolve() if output else ROOT / 'results/qos' / self.run_id
        if self.path.exists():
            raise ValueError('La directory dei risultati esiste già: scegliere un nuovo percorso.')
        self.initial_mode = lab.qos_mode
        self.hosts = self.model.get_all_hosts_dict()
        if scenario == 'isolated':
            self._validate_pair(source, target)
        else:
            for a, b in (('h2', 'h6'), ('h1', 'h5'), ('h3', 'h7')):
                self._validate_pair(a, b)
            if self.model.spines != 2:
                raise ValueError('Lo scenario noisy neighbor richiede i due spine configurati.')

    @property
    def running(self):
        return self.thread.is_alive()

    def start(self):
        self.thread.start()

    def status(self):
        with self._lock:
            return dict(self._status, output=str(self.path))

    def update(self, **values):
        with self._lock:
            self._status.update(values)

    def _validate_pair(self, source, target):
        a, b = self.hosts.get(source), self.hosts.get(target)
        tenant = (self.model.get_tenant_by_host_port(a.leaf, a.port) if a else None)
        if not a or not b or not tenant or b not in tenant.hosts or a.leaf == b.leaf:
            raise ValueError('Servono due host assegnati allo stesso tenant su leaf diversi.')

    def check_cancel(self):
        if self.cancel.is_set():
            raise RuntimeError('Esperimento annullato.')

    def wait_until(self, deadline):
        while time.monotonic() < deadline:
            if self.cancel.wait(min(.1, max(0, deadline - time.monotonic()))):
                self.check_cancel()

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.processes.clear()

    def spawn(self, command, log):
        self.check_cancel()
        with log.open('wb') as stream:
            process = subprocess.Popen(command, cwd=str(ROOT), stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def host_run(self, host, arguments):
        self.check_cancel()
        result = subprocess.run(self.lab.host_command(host) + arguments, capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'Comando fallito su {host}')
        return result.stdout

    def flow(self, source, target, rate, sport, port, role):
        return dict(source=source, target=target, offered_mbps=rate,
                    source_port=sport, port=port, role=role)

    def bucket_counts(self, leaf):
        snapshot = read_switch(self.lab.switch_prefix + leaf)
        if snapshot.groups is None or snapshot.groups.error:
            raise RuntimeError('Contatori ECMP non disponibili.')
        group = next((g for g in snapshot.groups.groups if g.group_id == 1), None)
        if group is None or any(b.packets is None for b in group.buckets):
            raise RuntimeError('Bucket ECMP privi di contatori.')
        return [b.packets for b in group.buckets]

    def probe(self, flow, ping=False):
        source, target = self.hosts[flow['source']], self.hosts[flow['target']]
        before = self.bucket_counts(source.leaf)
        if ping:
            self.host_run(source.name, ['ping', '-n', '-c', '3', '-i', '.1', '-W', '1', target.ip])
        else:
            program = ('import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); '
                       's.bind((%r,%d)); '
                       '[(s.sendto(bytes(1200),(%r,%d)),time.sleep(.002)) for _ in range(60)]')
            self.host_run(source.name, [sys.executable, '-c', program % (
                source.ip, flow['source_port'], target.ip, flow['port'])])
        # OVS aggiorna i contatori dal datapath in modo differito. Prima della
        # probe successiva attendere che tutti gli invii siano contabilizzati.
        deadline = time.monotonic() + 4
        expected = 3 if ping else 60
        while True:
            changes = [b - a for a, b in zip(before, self.bucket_counts(source.leaf))]
            if sum(changes) >= expected or time.monotonic() >= deadline:
                break
            self.cancel.wait(.15)
            self.check_cancel()
        with (self.path / 'calibration.jsonl').open('a') as log:
            log.write(json.dumps(dict(flow=flow, ping=ping, bucket_deltas=changes, expected=expected)) + '\n')
        if any(n < 0 for n in changes) or sum(changes) < expected:
            raise RuntimeError(f'Probe ECMP {source.name}: delta {changes}, attesi almeno {expected}.')
        ports = self.model.get_leaf_uplink_ports()
        return [ports[i] for i, count in enumerate(changes) if count > 0]

    def calibrate(self):
        self.update(message='Calibrazione tuple ECMP senza meter')
        self.lab.set_qos('off', self.cancel)
        victim = self.flow('h2', 'h6', 15, 40000, 5201, 'victim')
        victim['paths'] = self.probe(victim)
        victim['ping_paths'] = self.probe(victim, ping=True)
        flows = [victim]
        uplinks = self.model.get_leaf_uplink_ports()
        for source, target, base, port_base in (('h1', 'h5', 41000, 5300), ('h3', 'h7', 42000, 5400)):
            counts = {p: 0 for p in uplinks}
            selected = []
            for offset in range(64):
                self.check_cancel()
                flow = self.flow(source, target, self.noise_mbps / 8, base + offset,
                                 port_base + len(selected), 'noise')
                paths = self.probe(flow)
                if len(paths) == 1 and counts[paths[0]] < 4:
                    flow['paths'] = paths
                    selected.append(flow)
                    counts[paths[0]] += 1
                if len(selected) == 8:
                    break
            if len(selected) != 8:
                raise RuntimeError('Impossibile distribuire quattro tuple per uplink: riprogettare lo scenario.')
            flows.extend(selected)
        return flows

    def capture(self, leaf, target_leaf):
        snapshot = read_switch(self.lab.switch_prefix + leaf)
        captured_at = time.monotonic()
        links, errors = {}, []
        try:
            ports = run_ovs(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-ports', self.lab.switch_prefix + leaf])
        except (RuntimeError, OSError) as error:
            ports = None
            errors.append(f'Contatori porte: {error}')
        leaf_id = int(target_leaf[4:])
        interfaces = [f'{leaf}-eth{p}' for p in self.model.get_leaf_uplink_ports()]
        interfaces += [f'spine{i}-eth{self.model.get_spine_port_to_leaf(leaf_id)}'
                       for i in range(1, self.model.spines + 1)]
        for interface in interfaces:
            try:
                physical = self.lab.switch_prefix + interface
                qdiscs = json.loads(run_ovs(['tc', '-j', '-s', 'qdisc', 'show', 'dev', physical]))
                tx = int(Path(f'/sys/class/net/{physical}/statistics/tx_bytes').read_text())
                if not qdiscs:
                    raise ValueError('Qdisc assenti')
                links[interface] = dict(at=time.monotonic(), tx_bytes=tx, qdiscs=qdiscs, physical=physical)
            except (OSError, RuntimeError, ValueError) as error:
                errors.append(f'{interface}: {error}')
        return snapshot, dict(at=captured_at, links=links, errors=errors,
                              openflow=asdict(snapshot), ports=ports)

    def trial(self, flows, mode, pair, label):
        self.check_cancel()
        self.lab.set_qos(mode, self.cancel)
        first = flows[0]
        source, target = self.hosts[first['source']], self.hosts[first['target']]
        # Una reinstallazione non deve cambiare il percorso rispetto alla calibrazione.
        if 'paths' in first and self.probe(first) != first['paths']:
            raise RuntimeError('Percorso vittima cambiato: ripetere la coppia con nuova calibrazione.')
        folder = self.path / f'{len(self.trials) + 1:03d}-{label}-{mode}'
        folder.mkdir()
        for name in ('controller', 'network'):
            (folder / f'{name}.log').write_text(self.lab.logs(name))
        (folder / 'controller-status.json').write_text(json.dumps(self.lab._read('controller.json'), indent=2))
        self.update(message=f'{label} · QoS {mode} · misura {self.duration}s')
        start = time.monotonic() + 5
        begin = start + self.warmup
        clock_offset = time.time() - time.monotonic()
        workers, receivers = [], []
        for index, flow in enumerate(flows):
            receiver = self.hosts[flow['target']]
            sender = self.hosts[flow['source']]
            ready = folder / f'{index}-ready'
            common = ['--port', str(flow['port']), '--source-port', str(flow['source_port']),
                      '--bitrate', str(round(flow['offered_mbps'] * 1e6)), '--payload', '1200',
                      '--start-at', str(start), '--warmup', str(self.warmup), '--duration', str(self.duration)]
            rx = self.lab.host_command(receiver.name) + [sys.executable, str(ROOT / 'src/qos_traffic.py'),
                 'receive', '--bind', receiver.ip, '--peer', sender.ip,
                 '--ready-file', str(ready), '--output', str(folder / f'{index}-rx.json')] + common
            process = self.spawn(rx, folder / f'{index}-rx.log')
            receivers.append(process)
            workers.append((index, flow, ready, common))
        deadline = start - 1
        while not all(item[2].exists() for item in workers):
            self.check_cancel()
            if time.monotonic() >= deadline or any(p.poll() is not None for p in receivers):
                raise RuntimeError('Ricevitori non pronti prima della finestra comune; vedere i log.')
            self.cancel.wait(.05)
        for index, flow, ready, common in workers:
            sender, receiver = self.hosts[flow['source']], self.hosts[flow['target']]
            tx = self.lab.host_command(sender.name) + [sys.executable, str(ROOT / 'src/qos_traffic.py'),
                 'send', '--bind', sender.ip, '--peer', receiver.ip,
                 '--output', str(folder / f'{index}-tx.json')] + common
            self.spawn(tx, folder / f'{index}-tx.log')
        if time.monotonic() >= start:
            raise RuntimeError('Avvio sender tardivo: prova scartata.')
        ping_path = folder / 'ping.log'
        self.spawn(self.lab.host_command(source.name) + ['ping', '-n', '-D', '-i', '.1', '-w',
                   str(math.ceil(begin + self.duration + 3 - time.monotonic())), target.ip], ping_path)
        samples, snapshots = [], []
        for second in range(self.duration + 1):
            self.wait_until(begin + second)
            if not self.lab.status()['ready']:
                raise RuntimeError('Pipeline non più READY durante la misura.')
            snapshot, sample = self.capture(source.leaf, target.leaf)
            sample['second'] = second
            sample['lateness_s'] = sample['at'] - (begin + second)
            snapshots.append(snapshot)
            samples.append(sample)
            (folder / 'samples.json').write_text(json.dumps(samples))
        self.wait_until(begin + self.duration + 2.5)
        for process in self.processes[:-1]:
            if process.poll() is None:
                process.wait(timeout=3)
            if process.returncode:
                raise RuntimeError('Generatore/ricevitore fallito; vedere i log della prova.')
        self.cleanup()
        metadata = dict(run_id=self.run_id, trial=folder.name, pair=pair, scenario=self.scenario,
                        mode=mode, duration_s=self.duration, warmup_s=self.warmup, payload_bytes=1200,
                        burst_ms=100, uplink_mbps=self.model.topology['uplink_bw_mbps'],
                        host_mbps=self.model.topology['host_bw_mbps'],
                        delay_ms=self.model.topology['link_delay_ms'], noise_mbps=self.noise_mbps,
                        seed=self.seed, repetitions=self.repetitions)
        local = []
        for index, flow, _, _ in workers:
            tx = json.loads((folder / f'{index}-tx.json').read_text())
            rx = json.loads((folder / f'{index}-rx.json').read_text())
            host = self.hosts[flow['source']]
            tenant = self.model.get_tenant_by_host_port(host.leaf, host.port)
            for row in summarize_stream(tx, rx):
                local.append(dict(metadata, **flow, meter_id=tenant.tag, rate_mbps=tenant.rate_mbps,
                                  **row))
        ping = []
        for line in ping_path.read_text().splitlines():
            match = re.search(r'^\[([\d.]+)\].*icmp_seq=(\d+).*time[=<]([\d.]+) ms', line)
            if match:
                at = float(match[1]) - clock_offset
                if begin <= at < begin + self.duration:
                    ping.append({'second': int(at - begin), 'rtt_ms': float(match[3])})
        for row in local:
            rtts = [r['rtt_ms'] for r in ping if r['second'] == row['second']]
            row['rtt_mean_ms'] = statistics.mean(rtts) if rtts and row['role'] != 'noise' else None
        victim_rows = [r for r in local if r['role'] != 'noise']
        sent = sum(r['sent_packets'] for r in victim_rows)
        received = sum(r['received_packets'] for r in victim_rows)
        goodputs = [r['goodput_mbps'] for r in victim_rows]
        summary = dict(metadata, source=source.name, target=target.name,
                       offered_mbps=first['offered_mbps'],
                       goodput_mbps=statistics.mean(goodputs),
                       throughput_sd_mbps=statistics.stdev(goodputs),
                       loss_pct=100 * (sent - received) / sent if sent else None,
                       jitter_mean_ms=statistics.mean(r['jitter_ms'] for r in victim_rows
                           if r['jitter_ms'] is not None) if received else None,
                       rtt_mean_ms=statistics.mean(r['rtt_ms'] for r in ping) if ping else None,
                       rtt_sd_ms=statistics.stdev(r['rtt_ms'] for r in ping) if len(ping) > 1 else None,
                       rtt_p95_ms=sorted(r['rtt_ms'] for r in ping)[math.ceil(.95 * len(ping)) - 1]
                           if ping else None)
        observed = []
        for index, flow, _, _ in workers:
            rows = [r for r in local if r['source_port'] == flow['source_port']]
            observed.append(statistics.mean(r['sent_mbps'] for r in rows) >= flow['offered_mbps'] * .95)
        summary['generator_valid'] = all(observed)
        paths = first.get('paths', self.model.get_leaf_uplink_ports())
        interfaces = [f'{source.leaf}-eth{p}' for p in paths]
        interfaces += [f'spine{p - self.model.hosts_per_leaf}-eth{int(target.leaf[4:])}' for p in paths]
        evidence = congestion_evidence(samples, interfaces, self.model.topology['uplink_bw_mbps'])
        summary['congested'] = bool(evidence)
        summary['measurement_complete'] = not any(s['errors'] or s['lateness_s'] > .75
            or not snap.meters or snap.meters.error for s, snap in zip(samples, snapshots))
        summary['victim_goal_met'] = ((summary['goodput_mbps'] >= 14 and summary['loss_pct'] is not None
                                      and summary['loss_pct'] <= 2) if self.scenario == 'noisy-neighbor' else None)
        summary['validity'] = ('valida' if summary['generator_valid'] and summary['measurement_complete']
                               else 'incompleta')
        tenant = self.model.get_tenant_by_host_port(source.leaf, source.port)
        summary.update(meter_id=tenant.tag, rate_mbps=tenant.rate_mbps)
        if mode == 'on':
            try:
                if any(s.meters is None or s.meters.error for s in (snapshots[0], snapshots[-1])):
                    raise ValueError('Meter non leggibili')
                before, after = [next(m for m in s.meters.meters if m.meter_id == tenant.tag)
                                 for s in (snapshots[0], snapshots[-1])]
                expected = sum(h.leaf == source.leaf for h in tenant.hosts)
                actual = sum(bool(re.search(rf'(?:^|,)meter:{tenant.tag}(?:,|$)', f.actions))
                             for f in snapshots[-1].flows)
                if actual != expected or after.flow_count != expected:
                    raise ValueError('Riferimenti flow/meter incoerenti')
                if first['offered_mbps'] > tenant.rate_mbps:
                    summary.update(limit_check(before, after, samples[-1]['at'] - samples[0]['at'],
                                               tenant.rate_mbps))
                else:
                    summary['limit_status'] = 'sotto soglia'
                changes, error = meter_delta(after, before)
                summary['meter_drop_packets'] = changes['drop_packets'] if changes else None
                summary['meter_error'] = error
                if first['offered_mbps'] < tenant.rate_mbps:
                    summary['under_limit_ok'] = bool(changes and changes['packets'] > 0
                                                     and changes['drop_packets'] == 0)
                for tag in {self.model.get_tenant_by_host_port(self.hosts[f['source']].leaf,
                            self.hosts[f['source']].port).tag for f in flows}:
                    old, current = [next(m for m in s.meters.meters if m.meter_id == tag)
                                    for s in (snapshots[0], snapshots[-1])]
                    changes, error = meter_delta(current, old)
                    model_tenant = self.model.get_tenant_by_tag(tag)
                    offered = sum(f['offered_mbps'] for f in flows
                                  if self.hosts[f['source']] in model_tenant.hosts)
                    check = dict(metadata, meter_id=tag, rate_mbps=model_tenant.rate_mbps,
                                 offered_mbps=offered, meter_input_packets=changes['packets'] if changes else None,
                                 meter_drop_packets=changes['drop_packets'] if changes else None,
                                 meter_error=error)
                    references = sum(bool(re.search(rf'(?:^|,)meter:{tag}(?:,|$)', f.actions))
                                     for f in snapshots[-1].flows)
                    expected_references = sum(h.leaf == source.leaf for h in model_tenant.hosts)
                    configured = (current.flags == ('burst', 'kbps', 'stats')
                        and current.bands == (('drop', model_tenant.rate_mbps * 1000, model_tenant.rate_mbps * 100),)
                        and references == current.flow_count == expected_references)
                    check['configuration_valid'] = configured
                    if error or not configured or (offered > model_tenant.rate_mbps and changes['drop_packets'] == 0):
                        summary['validity'] = 'incompleta'
                    self.meter_checks.append(check)
            except (StopIteration, ValueError) as error:
                summary.update(limit_status='incompleto', meter_error=str(error), validity='incompleta')
        elif snapshots[-1].meters is None or snapshots[-1].meters.error:
            summary['validity'] = 'incompleta'
        elif (any(m.meter_id == tenant.tag for m in snapshots[-1].meters.meters)
              or any(re.search(r'\bmeter:', f.actions) for f in snapshots[-1].flows)):
            summary['validity'] = 'incompleta'
        for row in local:
            row['validity'] = summary['validity']
        self.rows.extend(local)
        self.trials.append(summary)
        (folder / 'summary.json').write_text(json.dumps(summary, indent=2))
        (folder / 'congestion.json').write_text(json.dumps(evidence, indent=2))
        write_csv(folder / 'intervals.csv', local)
        self.update(completed=len(self.trials))
        self.export()
        return summary

    def export(self):
        write_csv(self.path / 'intervals.csv', self.rows)
        write_csv(self.path / 'summary.csv', self.trials)
        write_csv(self.path / 'pairs.csv', self.pairs)
        write_csv(self.path / 'meters.csv', self.meter_checks)
        write_csv(self.path / 'aggregate.csv', aggregate_trials(self.trials))
        (self.path / 'status.json').write_text(json.dumps(self.status(), indent=2))

    def figure(self):
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        figure = make_subplots(rows=2, cols=2, subplot_titles=(
            'Goodput ricevuto (Mbps)', 'Perdita UDP (%)', 'RTT medio (ms)', 'Variabilità throughput (SD Mbps)'))
        metrics = ['goodput_mbps', 'loss_pct', 'rtt_mean_ms', 'throughput_sd_mbps']
        for index, metric in enumerate(metrics):
            for mode in ('off', 'on'):
                rows = [r for r in self.trials if r['mode'] == mode]
                positions = [r['pair'] if self.scenario == 'noisy-neighbor'
                             else f'{r["source"]} · {r["offered_mbps"]}Mbps' for r in rows]
                color = '#e87924' if mode == 'off' else '#2563eb'
                figure.add_trace(go.Scatter(x=positions, y=[r.get(metric) for r in rows],
                                           mode='markers+lines', name=f'QoS {mode}', connectgaps=False,
                                           line={'color': color}, marker={'color': color}, legendgroup=mode,
                                           text=[r['validity'] + ' / ' + r.get('pair_validity', 'coppia in corso') for r in rows],
                                           hovertemplate='%{x}<br>%{y}<br>%{text}<extra>%{fullData.name}</extra>',
                                           showlegend=index == 0), row=index // 2 + 1, col=index % 2 + 1)
        figure.update_layout(height=900, title={'text': f'{self.run_id} · {self.scenario}',
                             'x': .07, 'y': .98, 'yanchor': 'top', 'font': {'size': 18}}, margin={'t': 180})
        figure.update_xaxes(title_text='Coppia' if self.scenario == 'noisy-neighbor' else 'Sorgente / carico')
        figure.update_yaxes(rangemode='tozero')
        if self.scenario == 'noisy-neighbor':
            figure.add_hline(y=14, row=1, col=1, line_dash='dot', line_color='#64748b')
            figure.add_hline(y=2, row=1, col=2, line_dash='dot', line_color='#64748b')
        captions = [f'{self.duration}s misura + {self.warmup}s warmup · payload 1200B · burst 100ms · '
                    f'limiti tenant {[t.rate_mbps for t in self.model.tenants]}Mbps',
                    f'Uplink {self.model.topology["uplink_bw_mbps"]}Mbps · host {self.model.topology["host_bw_mbps"]}Mbps · '
                    f'delay {self.model.topology["link_delay_ms"]}ms · validità in pairs.csv',
                    (f'Vittima 15Mbps · rumore {self.noise_mbps}Mbps/tenant · {self.repetitions} coppie · seed {self.seed}'
                     if self.scenario == 'noisy-neighbor' else 'Isolato: baseline 40Mbps, sotto soglia 15Mbps, sovraccarico 40Mbps')]
        for index, caption in enumerate(captions):
            figure.add_annotation(text=caption, x=0, y=1.18 - index * .05, xref='paper', yref='paper',
                                  xanchor='left', showarrow=False, font={'size': 11})
        return figure

    def charts(self):
        self.figure().write_html(str(self.path / 'charts.html'), include_plotlyjs=True,
            config={'toImageButtonOptions': {'format': 'png', 'width': 1500, 'height': 1000,
                                             'scale': 1.5, 'filename': f'qos-{self.run_id}'}})

    def run(self):
        self.update(state='running')
        failure = None
        try:
            self.path.mkdir(parents=True)
            self.check_cancel()
            try:
                import plotly.graph_objects  # Verificare prima di avviare prove lunghe.
            except ImportError as error:
                raise RuntimeError('Installare requirements-visualizer.txt per esportare i grafici QoS.') from error
            versions = {name: subprocess.run(command, capture_output=True, text=True, timeout=5).stdout
                        for name, command in {'ovs': ['ovs-ofctl', '--version'],
                                              'iperf3': ['iperf3', '--version'], 'kernel': ['uname', '-r']}.items()}
            config = dict(scenario=self.scenario, duration=self.duration, warmup=self.warmup,
                          repetitions=self.repetitions, seed=self.seed, burst_ms=100, payload_bytes=1200,
                          noise_mbps=self.noise_mbps, initial_mode=self.initial_mode, versions=versions,
                          switch_prefix=self.lab.switch_prefix,
                          topology=self.model.topology, tenants=[asdict(t) for t in self.model.tenants],
                          traffic_engine='qos_traffic.py; sequenze e CLOCK_MONOTONIC condiviso')
            (self.path / 'config.json').write_text(json.dumps(config, indent=2))
            if self.scenario == 'isolated':
                pairs = [(self.source, self.target)]
                if self.matrix:
                    pairs = [(h.name, next(dst.name for dst in tenant.hosts if dst.leaf != h.leaf))
                             for tenant in self.model.tenants for h in tenant.hosts]
                for index, (source, target) in enumerate(pairs, 1):
                    outcomes = []
                    for mode, rate, label in (('off', 40, 'baseline'), ('on', 15, 'under'), ('on', 40, 'over')):
                        outcomes.append(self.trial([self.flow(source, target, rate, 40000, 5201, 'isolated')],
                                                   mode, index, f'{source}-{label}'))
                    baseline, under, over = outcomes
                    validated = (baseline['validity'] == 'valida' and baseline['goodput_mbps'] >= 38
                                 and baseline['loss_pct'] is not None and baseline['loss_pct'] <= 2
                                 and not baseline['congested'] and under.get('under_limit_ok')
                                 and under['validity'] == over['validity'] == 'valida'
                                 and over.get('limit_status') == 'verificato')
                    self.pairs.append(dict(pair=index, source=source, target=target,
                                           validity='limite verificato' if validated else 'limite non verificato'))
                    for trial in outcomes:
                        trial['pair_validity'] = 'valida' if validated else 'non verificata'
            else:
                flows = self.calibrate()
                (self.path / 'tuples.json').write_text(json.dumps(flows, indent=2))
                orders = [('off', 'on')] * (self.repetitions // 2) + [('on', 'off')] * (self.repetitions // 2)
                random.Random(self.seed).shuffle(orders)
                for pair, order in enumerate(orders, 1):
                    runs = {mode: self.trial(flows, mode, pair, f'pair-{pair}') for mode in order}
                    a, b = runs['off'], runs['on']
                    valid = a['validity'] == b['validity'] == 'valida' and a['congested']
                    for trial in runs.values():
                        trial['pair_validity'] = 'valida' if valid else 'non dimostrativa'
                    for row in self.rows:
                        if row['pair'] == pair:
                            row['pair_validity'] = 'valida' if valid else 'non dimostrativa'
                    self.pairs.append(dict(pair=pair, order='/'.join(order),
                        validity='confronto valido' if valid else 'non dimostrativo: riprogettare e ripetere entrambe le condizioni',
                        goodput_change_mbps=b['goodput_mbps'] - a['goodput_mbps'],
                        loss_change_pp=b['loss_pct'] - a['loss_pct'] if a['loss_pct'] is not None and b['loss_pct'] is not None else None,
                        victim_goal_met=b['victim_goal_met']))
            self.charts()
            self.update(criteria_verified=bool(self.pairs) and all(p['validity'] in
                        ('limite verificato', 'confronto valido') for p in self.pairs))
        except Exception as error:
            failure = str(error)
            if self.path.exists():
                for name in ('controller', 'network'):
                    log = self.lab.logs(name)
                    if isinstance(log, str):
                        (self.path / f'failure-{name}.log').write_text(log)
        finally:
            self.cleanup()
            self.update(message='Ripristino della modalità QoS iniziale')
            try:
                if self.lab.status()['network_alive']:
                    self.lab.set_qos(self.initial_mode)
            except Exception as error:
                failure = (failure + '; ' if failure else '') + f'Ripristino fallito: {error}'
            self.update(state='cancelled' if self.cancel.is_set() else 'failed' if failure else 'complete',
                        message=failure or 'Misure completate: consultare la validità delle singole prove')
            if self.path.exists():
                self.export()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['isolated', 'noisy-neighbor'], default='isolated')
    parser.add_argument('--source', default='h1')
    parser.add_argument('--target', default='h5')
    parser.add_argument('--matrix', action='store_true')
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--repetitions', type=int, default=6)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--noise-mbps', type=float, default=90)
    parser.add_argument('--output', type=Path)
    args = vars(parser.parse_args(argv))
    if os.geteuid() != 0:
        parser.error('Eseguire con sudo usando la .venv del progetto.')
    lab = ManagedLab()
    job = None
    try:
        lab.start()
        lab.wait_ready()
        job = lab.start_experiment(**args)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda signum, frame: job.cancel.set())
        previous = None
        while job.running:
            state = job.status()
            if state != previous:
                print(json.dumps(state), flush=True)
                previous = state
            job.thread.join(timeout=.5)
        print(json.dumps(job.status()), flush=True)
        return 0 if job.status()['state'] == 'complete' else 1
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if job:
            job.cancel.set()
        lab.close()


if __name__ == '__main__':
    sys.exit(main())
