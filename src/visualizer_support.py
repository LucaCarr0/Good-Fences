"""Modello, statistiche e processi del visualizzatore, senza dipendenze GUI."""
from __future__ import annotations

import atexit
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from src.network_models import NetworkModel
from tests.network.flow_visualizer import (
    available_switches, bucket_deltas, delta, read_state, readable_actions, readable_match,
)

ROOT = Path(__file__).resolve().parents[1]


def load_model():
    return NetworkModel(config_dir=str(ROOT / 'config'))


def topology_data(model):
    """Nodi e link configurati, comprese porte e host spare."""
    nodes, links = [], []
    for index in range(1, model.spines + 1):
        nodes.append({'name': f'spine{index}', 'kind': 'Spine', 'dpid': 100 + index})
    for index in range(1, model.leaves + 1):
        leaf = f'leaf{index}'
        nodes.append({'name': leaf, 'kind': 'Leaf', 'dpid': index})
        for spine in range(1, model.spines + 1):
            links.append({'source': leaf, 'target': f'spine{spine}',
                          'source_port': model.hosts_per_leaf + spine,
                          'target_port': model.get_spine_port_to_leaf(index)})
    tenants = {host.name: tenant for tenant in model.tenants for host in tenant.hosts}
    for host in model.get_all_hosts_dict().values():
        tenant = tenants.get(host.name)
        nodes.append({'name': host.name, 'kind': 'Host' if tenant else 'Spare',
                      'leaf': host.leaf, 'port': host.port, 'ip': host.ip,
                      'mac': host.mac, 'tenant': tenant.name if tenant else None,
                      'vlan': tenant.tag if tenant else None})
        links.append({'source': host.leaf, 'target': host.name,
                      'source_port': host.port, 'target_port': 0})
    return nodes, links


@dataclass
class SwitchSnapshot:
    flows: list
    groups: object
    captured_at: str


def read_switch(switch):
    flows, groups = read_state(switch)
    return SwitchSnapshot(flows, groups, datetime.now().astimezone().isoformat(timespec='seconds'))


def flow_rows(snapshot, baseline=None):
    reference = {flow.key: flow for flow in baseline.flows} if baseline else {}
    rows = []
    for index, flow in enumerate(snapshot.flows, 1):
        packets, byte_count = delta(flow, reference)
        rows.append({'ID': index, 'Tabella': flow.table, 'Priorità': flow.priority,
                     'Regola': flow.category, 'Cookie': hex(flow.cookie),
                     'Pacchetti': flow.packets, 'Byte': flow.byte_count,
                     'Delta pkt': packets if isinstance(packets, int) else None,
                     'Delta byte': byte_count if isinstance(byte_count, int) else None,
                     'Confronto': 'valido' if isinstance(packets, int) else packets,
                     'Match': readable_match(flow), 'Azioni': readable_actions(flow.actions),
                     'OVS originale': flow.raw})
    return rows


def group_rows(snapshot, baseline=None):
    """Non converte contatori indisponibili o riferimenti invalidi in zeri."""
    if snapshot.groups is None or snapshot.groups.error:
        return [], []
    old = ({group.group_id: group for group in baseline.groups.groups}
           if baseline and baseline.groups and not baseline.groups.error else {})
    rows, notices = [], []
    for group in snapshot.groups.groups:
        before = old.get(group.group_id)
        changes, error = bucket_deltas(group, before)
        total = sum(packets for packets, _ in changes) if changes is not None else 0
        if error:
            notices.append(f'Gruppo {group.group_id}: {error.replace("con b", "nella GUI")}.')
        elif not total:
            notices.append(f'Gruppo {group.group_id}: nessun incremento, quote non calcolate.')
        for index, bucket in enumerate(group.buckets):
            previous = before.buckets[index] if before and index < len(before.buckets) else None
            dp, db = changes[index] if changes is not None else (None, None)
            rows.append({'Gruppo': group.group_id, 'Tipo': group.kind, 'Bucket': index,
                         'Peso': bucket.weight, 'Azioni': readable_actions(bucket.actions),
                         'Pkt prima': previous.packets if previous else None,
                         'Pkt dopo': bucket.packets, 'Delta pkt': dp,
                         'Byte prima': previous.byte_count if previous else None,
                         'Byte dopo': bucket.byte_count, 'Delta byte': db,
                         'Quota pkt (%)': 100 * dp / total if total else None})
    return rows, notices


def summarize_flows(rows):
    summary = defaultdict(lambda: {'Regole': 0, 'Pacchetti': 0, 'Byte': 0})
    for row in rows:
        values = summary[row['Tabella'], row['Regola']]
        values['Regole'] += 1
        values['Pacchetti'] += row['Pacchetti']
        values['Byte'] += row['Byte']
    return [{'Tabella': table, 'Regola': category, **values}
            for (table, category), values in sorted(summary.items())]


class ManagedLab:
    """Una sola simulazione per server Streamlit, indipendente dai rerun/browser."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='good-fences-visualizer-')
        self.path = Path(self.directory.name)
        self.network = self.controller = None
        self.generation = None
        self.started_at = None
        self._lock = threading.RLock()
        atexit.register(self.close)

    def _read(self, name):
        try:
            return json.loads((self.path / name).read_text())
        except (OSError, ValueError):
            return {}

    def status(self):
        with self._lock:
            network = self._read('network.json')
            controller = self._read('controller.json')
            network_alive = self.network is not None and self.network.poll() is None
            controller_alive = self.controller is not None and self.controller.poll() is None
            ready = set(controller.get('ready', []))
            expected = set(controller.get('expected', []))
            return {'network_alive': network_alive, 'controller_alive': controller_alive,
                    'running': network_alive or controller_alive,
                    'ready': bool(network_alive and controller_alive and expected
                                  and ready == expected and network.get('state') == 'running'),
                    'ready_count': len(ready) if controller_alive else 0,
                    'expected_count': len(expected), 'error': controller.get('error'),
                    'hosts': network.get('hosts', {}) if network_alive else {},
                    'started_at': self.started_at,
                    'network_exit': self.network.poll() if self.network else None,
                    'controller_exit': self.controller.poll() if self.controller else None}

    def _spawn(self, command, log_name, env=None):
        with (self.path / log_name).open('wb') as output:
            return subprocess.Popen(command, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

    def start(self):
        with self._lock:
            if os.geteuid() != 0:
                raise RuntimeError('Avviare con bash scripts/start-visualizer.sh per gestire Mininet.')
            if self.status()['running']:
                raise RuntimeError('La simulazione è già avviata: arrestarla prima di riavviare.')
            # Non sostituire una simulazione avviata da un altro terminale.
            if available_switches():
                raise RuntimeError('Esistono già bridge Good-Fences. Chiudere la rete nella CLI '
                                   'Mininet prima di avviarla dalla GUI.')
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(('127.0.0.1', 6653))
                except OSError as error:
                    raise RuntimeError('Porta 6653 occupata: arrestare il controller esterno.') from error
            load_model()  # Validare prima di creare processi o interfacce.
            for name in ('network.json', 'controller.json'):
                (self.path / name).unlink(missing_ok=True)
            self.generation = uuid.uuid4().hex
            self.started_at = time.monotonic()
            environment = dict(os.environ, PYTHONPATH=str(ROOT / 'src'),
                               GOOD_FENCES_STATUS_FILE=str(self.path / 'controller.json'))
            try:
                self.controller = self._spawn(
                    ['bash', str(ROOT / 'scripts/start-controller.sh')], 'controller.log', environment)
                deadline = time.monotonic() + 10
                while True:
                    if self.controller.poll() is not None:
                        raise RuntimeError('Ryu non si è avviato. Consultare il log del controller.')
                    try:
                        with socket.create_connection(('127.0.0.1', 6653), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Ryu non apre la porta 6653 entro 10 secondi.')
                        time.sleep(0.1)
                self.network = self._spawn(
                    [sys.executable, '-u', str(ROOT / 'src/datacenter.py'),
                     '--controller', 'remote', '--headless',
                     '--state-file', str(self.path / 'network.json')], 'network.log')
            except Exception:
                self.stop()
                raise

    def stop(self):
        with self._lock:
            errors = []
            for process in (self.network, self.controller):
                if process is None or process.poll() is not None:
                    continue
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        if process is self.network:
                            subprocess.run([sys.executable, str(ROOT / 'scripts/cleanup-network.py')],
                                           cwd=str(ROOT), capture_output=True, text=True, timeout=15,
                                           check=True)
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(str(error))
            if not errors:
                self.network = self.controller = None
            if errors:
                raise RuntimeError('Arresto incompleto: ' + '; '.join(errors))

    def close(self):
        self.stop()
        self.directory.cleanup()

    def logs(self, name):
        try:
            with (self.path / f'{name}.log').open('rb') as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 32000))
                return source.read().decode('utf-8', errors='replace')
        except FileNotFoundError:
            return 'Nessun log disponibile.'

    def run_test(self, host, case, **options):
        with self._lock:
            status = self.status()
            if not status['ready']:
                raise RuntimeError('Attendere che tutti gli switch siano READY prima del test.')
            pid = status['hosts'].get(host)
            if not isinstance(pid, int) or pid <= 0:
                raise ValueError('Host non presente nella simulazione gestita dalla GUI.')
            try:
                arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            except OSError as error:
                raise RuntimeError('Il namespace host non è più disponibile.') from error
            if f'mininet:{host}'.encode() not in arguments:
                raise RuntimeError('Il PID non identifica più l’host Mininet selezionato.')
            allowed = {'mac', 'ip', 'target_ip', 'target_mac', 'vlan', 'timeout',
                       'flows', 'packets_per_flow'}
            if set(options) - allowed:
                raise ValueError('Opzione di test non supportata.')
            if case == 'ecmp-udp' and options.get('flows', 100) * options.get('packets_per_flow', 10) > 100000:
                raise ValueError('La GUI permette al massimo 100000 pacchetti per invio ECMP.')
            command = ['mnexec', '-a', str(pid), sys.executable,
                       str(ROOT / 'tests/network/packet_io.py'), '--json',
                       '--iface', f'{host}-eth0', '--case', case]
            for key, value in options.items():
                command.extend(['--' + key.replace('_', '-'), str(value)])
            try:
                result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                                        timeout=max(30, float(options.get('timeout', 1)) + 10))
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Timeout del test: alcuni pacchetti potrebbero essere stati inviati.') from error
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or 'Esecuzione del test fallita.')
            try:
                return json.loads(result.stdout)
            except ValueError as error:
                raise RuntimeError('Il processo di test non ha restituito un risultato JSON valido.') from error
