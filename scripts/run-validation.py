"""Esegue i casi T1-T26 della validazione sulla rete attiva e stampa l'esito di ogni assert.

Con controller e rete avviati (ALL SWITCHES READY), da un terminale Linux:
    sudo .venv/bin/python scripts/run-validation.py
    sudo .venv/bin/python scripts/run-validation.py --only T1 T11 T16
    sudo .venv/bin/python scripts/run-validation.py --only T1 --pause
Con Docker:
    docker compose exec good-fences .venv/bin/python scripts/run-validation.py

Gli invii e le catture usano tests/network/packet_io.py nel namespace degli host
(mnexec), i contatori tests/network/flow_visualizer.py, i casi "trace"
ovs-appctl ofproto/trace. T26 chiede di riavviare a mano il solo controller.
Uscita 0 se tutti i casi eseguiti sono superati, 1 altrimenti.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from scapy.all import ARP, IP, Dot1Q, Ether, conf, sniff

from controller.pipeline import (
    ARP_COOKIE, ARP_DROP_COOKIE, BASE_COOKIE, ECMP_GROUP_ID, HOST_IPV4_COOKIE, LOCAL_COOKIE,
    SPINE_COOKIE, TAG_DROP_COOKIE, TRANSIT_COOKIE, UPLINK_COOKIE, UPLINK_IPV4_COOKIE,
)
from network_models import NetworkModel
from tests.network import packet_io
from tests.network.flow_visualizer import (
    available_switches, bucket_deltas, delta, read_state, run_ovs,
)

SETTLE = 2.0  # OVS aggiorna i contatori dal datapath circa ogni 0,5 s.
ECMP_FLOWS, ECMP_PACKETS = 100, 10
ECMP_DESCRIPTION = (f'{ECMP_FLOWS} flussi UDP (porte sorgente {packet_io.ECMP_FIRST_SOURCE_PORT}-'
                    f'{packet_io.ECMP_FIRST_SOURCE_PORT + ECMP_FLOWS - 1}, destinazione '
                    f'{packet_io.ECMP_DESTINATION_PORT}), {ECMP_PACKETS} pacchetti ciascuno')
SPARE_SOURCE_IP = '10.0.0.4'  # Lo spare non ha binding: l'IP sorgente è indifferente.
RESTART_TIMEOUT = 90
TRACE_RULE = re.compile(r'^\s*(\d+)\.\s.*?\bcookie (0x[0-9a-f]+)', re.M)
LEGACY_TRACE_RULE = re.compile(r'Rule: table=(\d+) cookie=(0x[0-9a-f]+)')


class MeasureError(Exception):
    """Misura non eseguibile: il caso fallisce senza fermare gli altri."""


def paint(text, code):
    return f'\033[{code}m{text}\033[0m' if sys.stdout.isatty() else text


class Report:
    """Assert di un caso, stampati appena valutati."""

    def __init__(self, ident, requirement, route, description):
        self.ident, self.requirement = ident, requirement
        self.checks = []
        print(f'\n{paint(ident, "1")} [{requirement}] {route}: {description}', flush=True)

    def check(self, label, ok, detail=''):
        self.checks.append(bool(ok))
        status = paint('PASS', '1;32') if ok else paint('FAIL', '1;31')
        print(f'    {status}  {label}' + (f'  [{detail}]' if detail else ''), flush=True)

    def equal(self, label, value, expected):
        self.check(f'{label} = {expected}', value == expected, f'osservato {value}')

    def at_least(self, label, value, minimum=1):
        self.check(f'{label} ≥ {minimum}', value >= minimum, f'osservato {value}')

    @property
    def passed(self):
        return bool(self.checks) and all(self.checks)

    def close(self):
        verdict = paint('SUPERATO', '1;32') if self.passed else paint('NON SUPERATO', '1;31')
        print(f'    → {self.ident} {verdict} ({sum(self.checks)}/{len(self.checks)} assert)')


@dataclass
class Case:
    ident: str
    requirement: str
    route: str
    description: str
    run: object


CASES = []


def case(ident, requirement, route, description):
    def register(function):
        CASES.append(Case(ident, requirement, route, description, function))
        return function
    return register


def run_case(lab, entry):
    report = Report(entry.ident, entry.requirement, entry.route, entry.description)
    try:
        entry.run(lab, report)
    except Exception as error:  # Un errore di misura non deve fermare gli altri casi.
        report.check('misura completata', False, f'{type(error).__name__}: {error}')
    report.close()
    return report


def wait_enter(message):
    try:
        input(f'    {message} ')
    except EOFError:
        pass


def run_case_paused(lab, entry):
    """Attende Invio prima e dopo il caso, per gli screenshot della dashboard."""
    print(f'\n{entry.ident}: salvare il riferimento in "Statistiche" e fare lo screenshot "prima".')
    wait_enter(f'Premere Invio per eseguire {entry.ident}...')
    report = run_case(lab, entry)
    print(f'{entry.ident} eseguito: premere "Aggiorna statistiche" e fare lo screenshot "dopo".')
    wait_enter('Premere Invio per proseguire...')
    return report


# --- Lettura di porte, regole, frame e trace ---------------------------------

def port_number(text):
    """Porta OpenFlow da 5, "leaf1-eth5" oppure output:5."""
    found = re.search(r'(\d+)"?$', text.strip())
    return int(found[1]) if found else None


def leaf_number(name):
    return int(re.search(r'\d+', name)[0])


def matches(flow, criteria):
    fields = dict(token.partition('=')[::2] for token in flow.match)
    for key, expected in criteria.items():
        if key in ('table', 'priority'):
            value = getattr(flow, key)
        elif key == 'in_port':
            value = port_number(fields.get(key, ''))
        else:
            value, expected = fields.get(key, '').strip('"'), str(expected)
        if value != expected:
            return False
    return True


def frames_detail(frames):
    if not frames:
        return 'nessun frame'
    shown = []
    for frame in frames[:3]:
        vlan = 'assente' if frame['vlan'] is None else frame['vlan']
        text = f"{frame['ether_src']} → {frame['ether_dst']}, VLAN {vlan}"
        if frame.get('protocol') == 'ARP':
            text += f", ARP op {frame['operation']} {frame['source_ip']} ({frame['source_mac']})"
        shown.append(text)
    return f'{len(frames)} frame: ' + '; '.join(shown) + (' ...' if len(frames) > 3 else '')


def with_tags(frame, vlans, tpid=0x8100):
    """Aggiunge tag VLAN (il primo è l'esterno) a un frame IPv4 untagged."""
    tagged = Ether(src=frame[Ether].src, dst=frame[Ether].dst, type=tpid)
    for index, vlan in enumerate(vlans):
        tagged /= Dot1Q(vlan=vlan, type=0x8100 if index < len(vlans) - 1 else 0x0800)
    return tagged / frame[IP]


def ipv4_fields(source, target, vlan):
    """Campi di un trace IPv4 taggato, nell'ordine accettato da ofproto/trace."""
    return dict(dl_vlan=vlan, dl_src=source.mac, dl_dst=target.mac, dl_type='0x0800',
                nw_src=source.ip, nw_dst=target.ip)


@dataclass
class Trace:
    rules: list  # (tabella, cookie) nell'ordine attraversato
    outputs: list
    pop_vlan: bool
    controller: bool
    datapath: str

    @property
    def outcome(self):
        return tuple(self.rules), tuple(self.outputs), self.pop_vlan

    def __str__(self):
        parts = [' → '.join(f'tab.{table} cookie {cookie & 0xff:x}' for table, cookie in self.rules)
                 or 'nessuna regola']
        if self.pop_vlan:
            parts.append('pop VLAN')
        parts.append(f'output {",".join(map(str, self.outputs))}' if self.outputs else 'nessun output')
        parts.append(f'datapath: {self.datapath}')
        return '; '.join(parts)


def trace(switch, **fields):
    """ofproto/trace simula ingressi non generabili dagli host, senza toccare i contatori."""
    text = run_ovs(['ovs-appctl', 'ofproto/trace', switch,
                    ','.join(f'{key}={value}' for key, value in fields.items())])
    body = text.split('\nFinal flow:')[0]
    rules = TRACE_RULE.findall(body) or LEGACY_TRACE_RULE.findall(body)
    datapath = re.search(r'^Datapath actions: (.*)$', text, re.M)
    return Trace([(int(table), int(cookie, 16)) for table, cookie in rules],
                 [port_number(port) for port in re.findall(r'\boutput:("?[\w.-]+"?)', body)],
                 'pop_vlan' in body, 'controller' in text.lower(),
                 datapath[1].strip() if datapath else 'n/d')


def controller_connected(switch):
    uuids = re.findall(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                       run_ovs(['ovs-vsctl', 'get', 'bridge', switch, 'controller']))
    return bool(uuids) and all(
        run_ovs(['ovs-vsctl', 'get', 'controller', uuid, 'is_connected']).strip() == 'true'
        for uuid in uuids)


def host_pids():
    """PID delle shell Mininet degli host, riconosciute come in cleanup-network.py."""
    pids = {}
    for line in subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True).splitlines():
        pid, _, command = line.strip().partition(' ')
        args = command.split()
        if len(args) == 5 and args[:4] == ['bash', '--norc', '--noediting', '-is']:
            name = args[4].partition('mininet:')[2]
            if re.fullmatch(r'h[1-9][0-9]*', name):
                pids.setdefault(name, []).append(int(pid))
    duplicated = sorted(name for name, found in pids.items() if len(found) > 1)
    if duplicated:
        raise SystemExit(f'Più shell Mininet per {", ".join(duplicated)}: chiudere le run precedenti.')
    return {name: found[0] for name, found in pids.items()}


# --- Processi helper nei namespace degli host --------------------------------

def agent(request):
    """Lato helper: READY, attesa del via dal processo principale, poi cattura o invio."""
    iface = request['iface']

    def ready():
        print('READY', flush=True)
        sys.stdin.readline()

    if request['op'] == 'listen':
        if request['kind'] == 'ipv4':
            packets = packet_io.capture(iface, request['timeout'], on_ready=ready)
        else:
            with conf.L2listen(iface=iface, promisc=True) as receiver:
                ready()
                packets = sniff(opened_socket=receiver, timeout=request['timeout'],
                                lfilter=lambda packet: ARP in packet)
        return [packet_io.packet_details(packet) for packet in packets]
    ready()
    if request['op'] == 'ecmp':
        return packet_io.execute_case(iface, 'ecmp-udp', **request['options'])
    frame = Ether(bytes.fromhex(request['frame']))
    if request['replies']:
        return [packet_io.packet_details(packet) for packet in packet_io.exchange(iface, frame)]
    with conf.L2socket(iface=iface) as sender:
        sender.send(frame)
    return []


class Agent:
    """Helper in esecuzione: nel namespace di un host, oppure nel root per le porte switch."""

    def __init__(self, lab, host, request):
        self.label = request['iface']
        self.process = subprocess.Popen(
            lab.agent_command(host, request), cwd=str(ROOT), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_ready(self, timeout=30):
        if (select.select([self.process.stdout], [], [], timeout)[0]
                and self.process.stdout.readline().strip() == 'READY'):
            return
        self.stop()
        raise MeasureError(f'helper su {self.label} non pronto: {self.errors()}')

    def go(self):
        self.process.stdin.write('GO\n')
        self.process.stdin.flush()

    def result(self, timeout):
        try:
            output, errors = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            raise MeasureError(f'helper su {self.label} senza risposta entro {timeout:g}s')
        if self.process.returncode:
            raise MeasureError(f'helper su {self.label} fallito: {errors.strip()[-300:]}')
        try:
            return json.loads(output.strip().splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise MeasureError(f'helper su {self.label}: risultato non valido') from error

    def errors(self):
        return (self.process.stderr.read() or '').strip()[-300:] if self.process.stderr else ''

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


@dataclass
class Observation:
    before: dict
    after: dict
    frames: dict
    result: object

    def delta(self, switches, cookie, **criteria):
        """Somma dei delta pacchetti delle regole selezionate; nessuna regola è un errore."""
        total = matched = 0
        for switch in [switches] if isinstance(switches, str) else switches:
            baseline = {flow.key: flow for flow in self.before[switch][0]}
            for flow in self.after[switch][0]:
                if flow.cookie != cookie or not matches(flow, criteria):
                    continue
                matched += 1
                packets = delta(flow, baseline)[0]
                if not isinstance(packets, int):
                    raise MeasureError(f'{switch}: regola {packets} durante la misura')
                total += packets
        if not matched:
            raise MeasureError(f'nessuna regola cookie {cookie:#x} con {criteria}')
        return total

    def buckets(self, switch, group_id=ECMP_GROUP_ID):
        """(porta di uscita, delta pacchetti) per ogni bucket del gruppo."""
        groups = []
        for state in (self.before, self.after):
            snapshot = state[switch][1]
            if snapshot.error:
                raise MeasureError(f'{switch}: {snapshot.error}')
            group = next((g for g in snapshot.groups if g.group_id == group_id), None)
            if group is None:
                raise MeasureError(f'{switch}: gruppo {group_id} assente')
            groups.append(group)
        before, after = groups
        changes, error = bucket_deltas(after, before)
        if error:
            raise MeasureError(f'{switch}: {error}')
        return [(port_number(bucket.actions), packets)
                for bucket, (packets, _) in zip(after.buckets, changes)]


def host_capture(host):
    return host, f'{host}-eth0', 'ipv4'


def port_capture(iface):
    return None, iface, 'arp'


class Lab:
    """Rete attiva: host dal modello, PID Mininet, invii con misura dei contatori."""

    def __init__(self, model, timeout):
        self.model, self.timeout = model, timeout
        self.hosts = model.get_all_hosts_dict()
        self.leaves = [f'leaf{index}' for index in range(1, model.leaves + 1)]
        self.spines = [f'spine{index}' for index in range(1, model.spines + 1)]
        self.switches = self.leaves + self.spines
        missing = sorted(set(self.switches) - set(available_switches()))
        if missing:
            raise SystemExit(f'Switch assenti: {", ".join(missing)}. Avviare controller e rete.')
        offline = [switch for switch in self.switches if not controller_connected(switch)]
        if offline:
            raise SystemExit(f'Switch non connessi al controller: {", ".join(offline)}. '
                             'Attendere ALL SWITCHES READY.')
        self.pids = host_pids()
        missing = sorted(set(self.hosts) - set(self.pids), key=leaf_number)
        if missing:
            raise SystemExit(f'Host Mininet assenti: {", ".join(missing)}.')

    def tag(self, name):
        host = self.hosts[name]
        return self.model.get_tenant_by_host_port(host.leaf, host.port).tag

    def ipv4(self, source, target, mac=None, ip=None, target_mac=None, target_ip=None):
        """Frame IPv4 protocollo 253 di packet_io, con eventuali campi sostituiti."""
        sender, receiver = self.hosts[source], self.hosts[target]
        return packet_io.build_packet('ipv4-untagged', mac or sender.mac, ip or sender.ip,
                                      target_ip or receiver.ip, target_mac or receiver.mac)

    def arp(self, source, target_ip, kind='arp-request', ether_src=None, **fields):
        """Frame ARP di packet_io, con eventuali campi Ethernet/ARP falsificati."""
        host = self.hosts[source]
        frame = packet_io.build_packet(kind, host.mac, host.ip, target_ip, host.mac)
        if ether_src:
            frame[Ether].src = ether_src
        for key, value in fields.items():
            setattr(frame[ARP], key, value)
        return frame

    def uplink_ifaces(self, leaf):
        return [f'{leaf}-eth{port}' for port in self.model.get_leaf_uplink_ports()]

    def other_host_ifaces(self, name):
        host = self.hosts[name]
        return [f'{host.leaf}-eth{port}' for port in range(1, self.model.hosts_per_leaf + 1)
                if port != host.port]

    def agent_command(self, host, request):
        prefix = ['mnexec', '-a', str(self.pids[host])] if host else []
        return prefix + [sys.executable, str(Path(__file__).resolve()),
                         '--agent', json.dumps(request)]

    def run_in(self, host, *command):
        return subprocess.run(['mnexec', '-a', str(self.pids[host]), *command],
                              capture_output=True, text=True, timeout=15)

    def observe(self, sender, request, switches=(), captures=()):
        """Contatori prima/dopo l'invio, con le catture aperte prima del via."""
        agents = [Agent(self, host, {'op': 'listen', 'iface': iface, 'kind': kind,
                                     'timeout': self.timeout})
                  for host, iface, kind in captures]
        agents.append(Agent(self, sender, request))
        try:
            for helper in agents:
                helper.wait_ready()
            before = {switch: read_state(switch) for switch in switches}
            for helper in agents:  # Prima le catture, per ultimo l'invio.
                helper.go()
            result = agents[-1].result(timeout=120)
            sent = time.monotonic()
            frames = {helper.label: helper.result(self.timeout + 30) for helper in agents[:-1]}
        finally:
            for helper in agents:
                helper.stop()
        time.sleep(max(0.0, SETTLE - (time.monotonic() - sent)))
        after = {switch: read_state(switch) for switch in switches}
        return Observation(before, after, frames, result)

    def send(self, host, frame, switches=(), captures=(), replies=False):
        request = {'op': 'send', 'iface': f'{host}-eth0', 'frame': bytes(frame).hex(),
                   'replies': replies}
        seen = self.observe(host, request, switches, captures)
        if replies:
            # La cattura dell'invio vede anche il frame uscente: restano le reply di altri.
            seen.result = [reply for reply in seen.result
                           if reply['ether_src'] != frame[Ether].src]
        return seen

    def neighbour_states(self, host, ip, timeout=10):
        """Stati della cache ARP del kernel mentre un ping tenta di risolvere l'IP."""
        iface = f'{host}-eth0'
        self.run_in(host, 'ip', 'neigh', 'flush', 'to', ip, 'dev', iface)
        ping = subprocess.Popen(['mnexec', '-a', str(self.pids[host]),
                                 'ping', '-n', '-c', '1', '-W', '1', ip],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        states, deadline = [], time.monotonic() + timeout
        try:
            while states[-1:] != ['FAILED'] and time.monotonic() < deadline:
                words = self.run_in(host, 'ip', 'neigh', 'show', ip, 'dev', iface).stdout.split()
                state = words[-1] if words else 'assente'
                if states[-1:] != [state]:
                    states.append(state)
                time.sleep(0.1)
        finally:
            if ping.poll() is None:
                ping.kill()
            ping.wait()
        return states


# --- Assert condivisi --------------------------------------------------------

def no_frames(report, seen, *hosts):
    for host in hosts:
        frames = seen.frames[f'{host}-eth0']
        report.check(f'nessun frame su {host}', not frames, frames_detail(frames))


def spoofed_source(lab, report, **changes):
    h2 = lab.hosts['h2']
    seen = lab.send('h2', lab.ipv4('h2', 'h6', **changes), switches=(h2.leaf,),
                    captures=[host_capture('h6')])
    report.at_least(f'{h2.leaf} miss tab.0: Δpkt', seen.delta(h2.leaf, BASE_COOKIE, table=0))
    report.equal(f'{h2.leaf} cookie 40 di h2 (invariato): Δpkt',
                 seen.delta(h2.leaf, HOST_IPV4_COOKIE, in_port=h2.port), 0)
    no_frames(report, seen, 'h6')


def tagged_frames(lab, report, variants):
    h2 = lab.hosts['h2']
    for label, vlans, tpid in variants:
        seen = lab.send('h2', with_tags(lab.ipv4('h2', 'h6'), vlans, tpid), switches=(h2.leaf,))
        report.equal(f'{label}: DROP prio 400 (cookie 20) Δpkt',
                     seen.delta(h2.leaf, TAG_DROP_COOKIE, priority=400, in_port=h2.port), 1)
        report.equal(f'{label}: cookie 40 di h2 Δpkt',
                     seen.delta(h2.leaf, HOST_IPV4_COOKIE, in_port=h2.port), 0)


def proxy_reply(lab, report, seen, requester, owner, others):
    """Una sola reply untagged con il MAC del proprietario, solo verso il richiedente."""
    asking, owning = lab.hosts[requester], lab.hosts[owner]
    replies = seen.result
    reply = replies[0] if len(replies) == 1 else None
    report.equal('reply ARP ricevute', len(replies), 1)
    report.check(f'reply untagged con MAC di {owner} ({owning.mac})',
                 reply is not None and reply['vlan'] is None
                 and reply['source_mac'] == owning.mac and reply['source_ip'] == owning.ip,
                 frames_detail(replies))
    stray = [frame for iface in others for frame in seen.frames[iface]
             if frame.get('operation') == 2]
    report.check(f'reply solo verso {requester} (nessuna su {", ".join(others)})',
                 reply is not None and reply['ether_dst'] == asking.mac and not stray,
                 frames_detail(stray) if stray else frames_detail(replies))


def arp_drop(lab, report, kind, **rule):
    h2 = lab.hosts['h2']
    seen = lab.send('h2', lab.arp('h2', lab.hosts['h6'].ip, kind), switches=(h2.leaf,),
                    replies=True)
    report.equal(f'{h2.leaf} DROP prio 350 (cookie 21): Δpkt',
                 seen.delta(h2.leaf, ARP_DROP_COOKIE, table=0, priority=350,
                            in_port=h2.port, **rule), 1)
    report.equal('reply ARP ricevute', len(seen.result), 0)


def ecmp(lab, report, source, target):
    """Delta positivo su ogni bucket del gruppo SELECT del leaf sorgente."""
    sender, receiver = lab.hosts[source], lab.hosts[target]
    options = dict(mac=sender.mac, ip=sender.ip, target_mac=receiver.mac,
                   target_ip=receiver.ip, flows=ECMP_FLOWS, packets_per_flow=ECMP_PACKETS)
    seen = lab.observe(source, {'op': 'ecmp', 'iface': f'{source}-eth0', 'options': options},
                       switches=(sender.leaf,))
    report.equal('pacchetti inviati', seen.result['sent_packets'], ECMP_FLOWS * ECMP_PACKETS)
    buckets = seen.buckets(sender.leaf)
    total = sum(packets for _, packets in buckets)
    report.equal(f'{sender.leaf} gruppo {ECMP_GROUP_ID}: numero di bucket',
                 len(buckets), lab.model.spines)
    for index, (port, packets) in enumerate(buckets):
        share = f', quota {100 * packets / total:.1f}%' if total else ''
        report.check(f'{sender.leaf} gruppo {ECMP_GROUP_ID} bucket {index} (porta {port}): Δpkt > 0',
                     packets > 0, f'osservato {packets}{share}')


# --- R1: isolamento ----------------------------------------------------------

@case('T1', 'R1', 'h2 → h6', 'IPv4 untagged, sorgente e destinazione coerenti (tenant B, leaf diversi)')
def t1(lab, report):
    h2, h6 = lab.hosts['h2'], lab.hosts['h6']
    tag = lab.tag('h6')
    seen = lab.send('h2', lab.ipv4('h2', 'h6'), switches=(h2.leaf, h6.leaf, *lab.spines),
                    captures=[host_capture('h6')])
    frames = seen.frames['h6-eth0']
    report.check('h6 riceve 1 frame untagged da h2',
                 len(frames) == 1 and frames[0]['vlan'] is None
                 and frames[0]['ether_src'] == h2.mac and frames[0]['ether_dst'] == h6.mac,
                 frames_detail(frames))
    report.equal(f'{h2.leaf} tab.0 cookie 40 (porta {h2.port}): Δpkt',
                 seen.delta(h2.leaf, HOST_IPV4_COOKIE, in_port=h2.port), 1)
    report.equal(f'{h2.leaf} tab.1 cookie 42 (verso h6): Δpkt',
                 seen.delta(h2.leaf, TRANSIT_COOKIE, dl_vlan=tag, dl_dst=h6.mac), 1)
    report.equal(f'{h2.leaf} tab.2 cookie 51 (gruppo ECMP): Δpkt',
                 seen.delta(h2.leaf, UPLINK_COOKIE, in_port=h2.port, dl_dst=h6.mac), 1)
    report.equal(f'{"+".join(lab.spines)} cookie 52 (verso h6): Δpkt',
                 seen.delta(lab.spines, SPINE_COOKIE, dl_dst=h6.mac,
                            in_port=lab.model.get_spine_port_to_leaf(leaf_number(h2.leaf))), 1)
    report.equal(f'{h6.leaf} tab.0 cookie 41 (da uplink, VLAN {tag}): Δpkt',
                 seen.delta(h6.leaf, UPLINK_IPV4_COOKIE, dl_vlan=tag), 1)
    report.equal(f'{h6.leaf} tab.1 cookie 42 (verso h6): Δpkt',
                 seen.delta(h6.leaf, TRANSIT_COOKIE, dl_vlan=tag, dl_dst=h6.mac), 1)
    report.equal(f'{h6.leaf} tab.2 cookie 50 (consegna a h6): Δpkt',
                 seen.delta(h6.leaf, LOCAL_COOKIE, dl_dst=h6.mac), 1)


@case('T2', 'R1', 'h1 → h6', 'IPv4 da h1 (A) con MAC e IP di h6 (B)')
def t2(lab, report):
    leaf = lab.hosts['h1'].leaf
    seen = lab.send('h1', lab.ipv4('h1', 'h6'), switches=(leaf,),
                    captures=[host_capture('h5'), host_capture('h6')])
    report.at_least(f'{leaf} miss tab.1: Δpkt', seen.delta(leaf, BASE_COOKIE, table=1))
    no_frames(report, seen, 'h5', 'h6')


@case('T3', 'R1, R3', 'h2 → h5', 'IPv4 da h2 (B) con MAC di h5 (A) e IP sovrapposto 10.0.0.2')
def t3(lab, report):
    leaf = lab.hosts['h2'].leaf
    seen = lab.send('h2', lab.ipv4('h2', 'h5'), switches=(leaf,), captures=[host_capture('h5')])
    report.at_least(f'{leaf} miss tab.1: Δpkt', seen.delta(leaf, BASE_COOKIE, table=1))
    no_frames(report, seen, 'h5')


@case('T4', 'R1', 'h2 → h6', 'IPv4 con MAC di h6 e IP di h10 (10.0.0.13)')
def t4(lab, report):
    leaf = lab.hosts['h2'].leaf
    frame = lab.ipv4('h2', 'h6', target_ip=lab.hosts['h10'].ip)
    seen = lab.send('h2', frame, switches=(leaf,), captures=[host_capture('h6')])
    report.at_least(f'{leaf} miss tab.1 (coppia MAC/IP incoerente): Δpkt',
                    seen.delta(leaf, BASE_COOKIE, table=1))
    no_frames(report, seen, 'h6')


@case('T5', 'R1', 'spine1 (trace)', 'IPv4 con VLAN 101 e MAC/IP di h6, ingresso da leaf1')
def t5(lab, report):
    h1, h6 = lab.hosts['h1'], lab.hosts['h6']
    spine = lab.spines[0]
    result = trace(spine, in_port=lab.model.get_spine_port_to_leaf(leaf_number(h1.leaf)),
                   **ipv4_fields(h1, h6, lab.tag('h1')))
    report.check(f'{spine}: table-miss in tab.0 (cookie 10)',
                 result.rules == [(0, BASE_COOKIE)], str(result))
    report.check(f'nessun output verso {h6.leaf}',
                 not result.outputs and result.datapath == 'drop', str(result))


# --- R2: anti-spoofing -------------------------------------------------------

@case('T6', 'R2', 'h2 → h6', 'IPv4 con MAC sorgente di h1')
def t6(lab, report):
    spoofed_source(lab, report, mac=lab.hosts['h1'].mac)


@case('T7', 'R2', 'h2 → h6', 'IPv4 con IP sorgente 10.0.0.99')
def t7(lab, report):
    spoofed_source(lab, report, ip='10.0.0.99')


@case('T8', 'R2', 'h2 → h6', 'IPv4 con tag VLAN 102, 101 oppure VID 0')
def t8(lab, report):
    own, other = lab.tag('h2'), lab.tag('h1')
    tagged_frames(lab, report, ((f'VLAN {own}', (own,), 0x8100),
                                (f'VLAN {other}', (other,), 0x8100),
                                ('VID 0', (0,), 0x8100)))


@case('T9', 'R2', 'h2 → h6', 'IPv4 con doppio tag 101/102 oppure TPID 0x88a8, 0x9100')
def t9(lab, report):
    own, other = lab.tag('h2'), lab.tag('h1')
    tagged_frames(lab, report, ((f'doppio tag {other}/{own}', (other, own), 0x8100),
                                ('TPID 0x88a8', (own,), 0x88a8),
                                ('TPID 0x9100', (own,), 0x9100)))


@case('T10', 'R2', 'h2 → bcast', 'ARP request con MAC Ethernet, SHA oppure SPA falsificati')
def t10(lab, report):
    h1, h2, h6 = (lab.hosts[name] for name in ('h1', 'h2', 'h6'))
    variants = (('MAC Ethernet falso', dict(ether_src=h1.mac)),
                ('SHA falso', dict(hwsrc=h1.mac)),
                ('SPA falso', dict(psrc='10.0.0.99')))
    for label, changes in variants:
        seen = lab.send('h2', lab.arp('h2', h6.ip, **changes), switches=(h2.leaf,), replies=True)
        report.at_least(f'{label}: {h2.leaf} miss tab.0 Δpkt',
                        seen.delta(h2.leaf, BASE_COOKIE, table=0))
        report.equal(f'{label}: cookie 30 (nessun PacketIn) Δpkt',
                     seen.delta(h2.leaf, ARP_COOKIE, in_port=h2.port), 0)
        report.equal(f'{label}: reply ARP ricevute', len(seen.result), 0)


# --- R3: indirizzi sovrapposti e proxy ARP -----------------------------------

@case('T11', 'R3', 'h2 → bcast', 'ARP request per 10.0.0.2')
def t11(lab, report):
    h2, h6 = lab.hosts['h2'], lab.hosts['h6']
    uplinks, others = lab.uplink_ifaces(h2.leaf), lab.other_host_ifaces('h2')
    seen = lab.send('h2', lab.arp('h2', h6.ip), switches=(h2.leaf,), replies=True,
                    captures=[port_capture(iface) for iface in uplinks + others])
    proxy_reply(lab, report, seen, 'h2', 'h6', others)
    report.equal(f'{h2.leaf} cookie 30 (ARP al controller): Δpkt',
                 seen.delta(h2.leaf, ARP_COOKIE, in_port=h2.port), 1)
    leaked = [frame for iface in uplinks for frame in seen.frames[iface]]
    report.check(f'nessun ARP sugli uplink {", ".join(uplinks)}', not leaked, frames_detail(leaked))


@case('T12', 'R3', 'h1 → bcast', 'ARP request per 10.0.0.2')
def t12(lab, report):
    others = lab.other_host_ifaces('h1')
    seen = lab.send('h1', lab.arp('h1', lab.hosts['h5'].ip), replies=True,
                    captures=[port_capture(iface) for iface in others])
    proxy_reply(lab, report, seen, 'h1', 'h5', others)


@case('T13', 'R3', 'h1 → bcast', 'ARP request per 10.0.0.13 (presente solo in B)')
def t13(lab, report):
    target = lab.hosts['h10'].ip
    seen = lab.send('h1', lab.arp('h1', target), replies=True)
    report.equal('reply ARP ricevute', len(seen.result), 0)
    states = lab.neighbour_states('h1', target)
    report.check('cache di h1: INCOMPLETE, poi FAILED',
                 'INCOMPLETE' in states and states[-1] == 'FAILED'
                 and states.index('INCOMPLETE') < len(states) - 1,
                 ' → '.join(states) or 'nessuno stato')


@case('T14', 'R3', 'h2 → bcast', 'ARP probe (SPA 0.0.0.0)')
def t14(lab, report):
    arp_drop(lab, report, 'arp-probe', arp_spa='0.0.0.0')


@case('T15', 'R3', 'h2 → bcast', 'Gratuitous ARP (SPA = TPA = 10.0.0.1)')
def t15(lab, report):
    arp_drop(lab, report, 'gratuitous-arp', arp_tpa=lab.hosts['h2'].ip)


# --- R4: multipath -----------------------------------------------------------

@case('T16', 'R4', 'h2 → h6', ECMP_DESCRIPTION)
def t16(lab, report):
    ecmp(lab, report, 'h2', 'h6')


@case('T17', 'R4', 'h6 → h10', ECMP_DESCRIPTION)
def t17(lab, report):
    ecmp(lab, report, 'h6', 'h10')


@case('T18', 'R4', 'h10 → h2', ECMP_DESCRIPTION)
def t18(lab, report):
    ecmp(lab, report, 'h10', 'h2')


@case('T19', 'R4', 'spine1/2, leaf2 (trace)', 'IPv4 con VLAN 102 verso h6, attraverso ciascuno spine')
def t19(lab, report):
    h2, h6 = lab.hosts['h2'], lab.hosts['h6']
    fields = ipv4_fields(h2, h6, lab.tag('h6'))
    to_leaf = lab.model.get_spine_port_to_leaf(leaf_number(h6.leaf))
    spines = []
    for spine in lab.spines:
        result = trace(spine, in_port=lab.model.get_spine_port_to_leaf(leaf_number(h2.leaf)),
                       **fields)
        spines.append(result.outcome)
        report.check(f'{spine}: cookie 52, VLAN mantenuta, output porta {to_leaf} verso {h6.leaf}',
                     result.rules == [(0, SPINE_COOKIE)] and result.outputs == [to_leaf]
                     and not result.pop_vlan, str(result))
    leaves = []
    for port in lab.model.get_leaf_uplink_ports():
        result = trace(h6.leaf, in_port=port, **fields)
        leaves.append(result.outcome)
        report.check(f'{h6.leaf} da uplink {port}: cookie 41 → 42 → 50, '
                     f'rimozione VLAN e output porta {h6.port}',
                     result.rules == [(0, UPLINK_IPV4_COOKIE), (1, TRANSIT_COOKIE), (2, LOCAL_COOKIE)]
                     and result.pop_vlan and result.outputs == [h6.port], str(result))
    report.check('esito identico sui due percorsi', len(set(spines)) == 1 and len(set(leaves)) == 1,
                 f'{len(set(spines))} esiti sugli spine, {len(set(leaves))} su {h6.leaf}')


# --- R5: deny-by-default -----------------------------------------------------

@case('T20', 'R5', 'h4 → h6', 'IPv4 untagged dallo spare h4')
def t20(lab, report):
    leaf = lab.hosts['h4'].leaf
    seen = lab.send('h4', lab.ipv4('h4', 'h6', ip=SPARE_SOURCE_IP), switches=(leaf,),
                    captures=[host_capture('h6')])
    report.at_least(f'{leaf} miss tab.0: Δpkt', seen.delta(leaf, BASE_COOKIE, table=0))
    no_frames(report, seen, 'h6')


@case('T21', 'R5', 'h2 → bcast', 'RARP request (EtherType 0x8035)')
def t21(lab, report):
    h2 = lab.hosts['h2']
    seen = lab.send('h2', lab.arp('h2', lab.hosts['h6'].ip, 'rarp'), switches=(h2.leaf,),
                    replies=True)
    report.at_least(f'{h2.leaf} miss tab.0: Δpkt', seen.delta(h2.leaf, BASE_COOKIE, table=0))
    report.equal('reply ARP/RARP ricevute', len(seen.result), 0)


@case('T22', 'R5', 'h2 → bcast', 'ARP reply spontanea (opcode 2)')
def t22(lab, report):
    h2 = lab.hosts['h2']
    seen = lab.send('h2', lab.arp('h2', lab.hosts['h6'].ip, op=2), switches=(h2.leaf,))
    report.at_least(f'{h2.leaf} miss tab.0: Δpkt', seen.delta(h2.leaf, BASE_COOKIE, table=0))
    report.equal(f'{h2.leaf} cookie 30 (nessun PacketIn): Δpkt',
                 seen.delta(h2.leaf, ARP_COOKIE, in_port=h2.port), 0)


@case('T23', 'R5', 'leaf1 (trace)', "IPv4 con VLAN 102 verso h6 in ingresso dall'uplink 5")
def t23(lab, report):
    h2, h6 = lab.hosts['h2'], lab.hosts['h6']
    port = lab.model.get_leaf_uplink_ports()[0]
    result = trace(h2.leaf, in_port=port, **ipv4_fields(h2, h6, lab.tag('h6')))
    report.check(f'{h2.leaf} da uplink {port}: cookie 41 → 42 → miss tab.2',
                 result.rules == [(0, UPLINK_IPV4_COOKIE), (1, TRANSIT_COOKIE), (2, BASE_COOKIE)],
                 str(result))
    report.check('nessun output: nessun transito uplink → uplink',
                 not result.outputs and result.datapath == 'drop', str(result))


@case('T24', 'R5', 'leaf1 (trace)', 'ARP con VLAN 102 in ingresso dagli uplink 5 e 6')
def t24(lab, report):
    h2, h6 = lab.hosts['h2'], lab.hosts['h6']
    for port in lab.model.get_leaf_uplink_ports():
        result = trace(h2.leaf, in_port=port, dl_vlan=lab.tag('h2'), dl_type='0x0806', arp_op=1,
                       arp_spa=h2.ip, arp_tpa=h6.ip, arp_sha=h2.mac,
                       arp_tha='00:00:00:00:00:00')
        report.check(f'{h2.leaf} ARP da uplink {port}: miss tab.0',
                     result.rules == [(0, BASE_COOKIE)], str(result))
        report.check(f'{h2.leaf} ARP da uplink {port}: nessun output verso il controller',
                     not result.controller and not result.outputs, str(result))


@case('T25', 'R5', 'tutti gli switch', 'Ispezione di flow e gruppi installati')
def t25(lab, report):
    uplinks = lab.model.get_leaf_uplink_ports()
    for switch in lab.switches:
        flows, groups = read_state(switch)
        flooding = [flow for flow in flows if re.search(r'\b(?:NORMAL|FLOOD)\b', flow.actions)]
        report.check(f'{switch}: nessuna azione NORMAL o FLOOD', not flooding,
                     f'{len(flows)} regole' + ''.join(f'; {flow.raw}' for flow in flooding[:2]))
        if groups.error:
            report.check(f'{switch}: gruppi leggibili', False, groups.error)
            continue
        found = '; '.join(f'gruppo {group.group_id} {group.kind} porte '
                          f'{[port_number(bucket.actions) for bucket in group.buckets]}'
                          for group in groups.groups) or 'nessun gruppo'
        if switch in lab.leaves:
            group = groups.groups[0] if len(groups.groups) == 1 else None
            report.check(f'{switch}: un solo gruppo SELECT {ECMP_GROUP_ID} con bucket verso {uplinks}',
                         group is not None and group.group_id == ECMP_GROUP_ID
                         and group.kind == 'select'
                         and [port_number(bucket.actions) for bucket in group.buckets] == uplinks,
                         found)
        else:
            report.check(f'{switch}: nessun gruppo', not groups.groups, found)


# --- T26: riavvio del controller ---------------------------------------------

def pipeline_definition(state):
    flows, groups = state
    return (sorted(flow.key for flow in flows),
            None if groups.error else [(group.group_id, group.definition) for group in groups.groups])


def restart_controller(lab, cases, previous):
    """T26: riavvio manuale del solo controller, verifica della pipeline e ripetizione dei casi."""
    report = Report('T26', 'R1–R5', 'controller', 'Riavvio del solo controller con la rete attiva')
    before = {switch: read_state(switch) for switch in lab.switches}
    asked = time.monotonic()
    count = len(lab.switches)
    print('    Riavviare ora SOLO il controller, lasciando attiva la rete:\n'
          '      1. Ctrl+C nel terminale del controller\n'
          '      2. bash scripts/start-controller.sh\n'
          '         (Docker: docker compose exec good-fences bash scripts/start-controller.sh)\n'
          f'      3. attendere ALL SWITCHES READY: {count}/{count}')
    try:
        answer = input('    Premere Invio dopo il riavvio ("s" + Invio per saltare T26): ')
    except EOFError:
        answer = 's'
    if answer.strip().lower() == 's':
        print('    T26 saltato.')
        return None
    deadline = time.monotonic() + RESTART_TIMEOUT
    connected, after, fresh, changed, elapsed = [], {}, False, list(lab.switches), 0.0
    while time.monotonic() < deadline:
        try:
            connected = [switch for switch in lab.switches if controller_connected(switch)]
            after = {switch: read_state(switch) for switch in lab.switches}
        except (RuntimeError, ValueError):
            time.sleep(1)  # Durante la reinstallazione le tabelle possono essere vuote.
            continue
        elapsed = time.monotonic() - asked
        # Una regola o un gruppo reinstallato ha durata inferiore al tempo dalla richiesta.
        fresh = all(flow.duration <= elapsed for flows, _ in after.values() for flow in flows) and all(
            group.duration is None or group.duration <= elapsed
            for _, groups in after.values() for group in groups.groups)
        changed = [switch for switch in lab.switches
                   if pipeline_definition(after[switch]) != pipeline_definition(before[switch])]
        if len(connected) == count and fresh and not changed:
            break
        time.sleep(1)
    report.equal('READY: switch connessi al controller', f'{len(connected)}/{count}', f'{count}/{count}')
    longest = max((flow.duration for flows, _ in after.values() for flow in flows), default=0)
    report.check('pipeline ricostruita: regole e gruppi reinstallati dopo il riavvio', fresh,
                 f'durata massima delle regole {longest:.1f}s, {elapsed:.1f}s dalla richiesta')
    report.check('pipeline identica a quella precedente il riavvio', not changed,
                 f'diversa su {", ".join(changed)}' if changed else f'{count} switch confrontati')
    if len(connected) != count:
        report.check('casi ripetuti dopo il riavvio', False, 'controller non connesso a tutti gli switch')
        report.close()
        return report
    time.sleep(1)  # La Barrier finale precede READY.
    print(f'\n{paint("Ripetizione dei casi dopo il riavvio del controller", "1")}')
    repeated = [run_case(lab, entry) for entry in cases]
    differences = [f'{old.ident} {"PASS" if old.passed else "FAIL"} → {"PASS" if new.passed else "FAIL"}'
                   for old, new in zip(previous, repeated) if old.passed != new.passed]
    span = f'{cases[0].ident}–{cases[-1].ident}' if len(cases) > 1 else cases[0].ident
    print(f'\n{paint("T26", "1")} confronto con la prima esecuzione')
    report.check(f'{span} con esito invariato', not differences,
                 '; '.join(differences) or f'{len(repeated)} casi con lo stesso esito')
    report.close()
    return report


# --- Avvio -------------------------------------------------------------------

def print_summary(reports, skipped_restart):
    print(f'\n{paint("RIEPILOGO", "1")}')
    for report in reports:
        verdict = paint('PASS', '1;32') if report.passed else paint('FAIL', '1;31')
        print(f'  {report.ident:<4} {report.requirement:<7} {verdict}  '
              f'{sum(report.checks)}/{len(report.checks)} assert')
    if skipped_restart:
        print(f'  {"T26":<4} {"R1–R5":<7} {paint("SALTATO", "1;33")}')
    first = next((report for report in reports if report.ident == 'T1'), None)
    if first is not None and not first.passed:
        print(paint('  Attenzione: T1 non superato, gli esiti negativi non dimostrano l\'isolamento.', '1;33'))
    passed = sum(report.passed for report in reports)
    print(f'Casi superati: {passed}/{len(reports)}')


def ovs_version():
    try:
        return subprocess.run(['ovs-vswitchd', '--version'], capture_output=True,
                              text=True, timeout=5).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return 'versione non disponibile'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', nargs='+', metavar='ID',
                        help='Casi da eseguire, per esempio --only T1 T11 T26')
    parser.add_argument('--no-restart', action='store_true',
                        help='Non eseguire T26 (riavvio interattivo del controller)')
    parser.add_argument('--timeout', type=float, default=2.0,
                        help='Secondi di cattura dopo ogni invio (default: 2)')
    parser.add_argument('--pause', action='store_true',
                        help='Attende Invio prima e dopo ogni caso, per gli screenshot')
    parser.add_argument('--agent', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.only:
        args.only = {item.strip().upper() for value in args.only
                     for item in value.split(',') if item.strip()}
        unknown = args.only - {entry.ident for entry in CASES} - {'T26'}
        if unknown:
            parser.error(f'casi sconosciuti: {", ".join(sorted(unknown))}')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout deve essere positivo e finito')
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.agent:
        print(json.dumps(agent(json.loads(args.agent))), flush=True)
        return 0
    if os.geteuid() != 0:
        raise SystemExit('Eseguire con sudo: mnexec e ovs-ofctl richiedono i privilegi di root.')
    for tool in ('mnexec', 'ovs-ofctl', 'ovs-appctl', 'ovs-vsctl'):
        if shutil.which(tool) is None:
            raise SystemExit(f'{tool} non trovato: installare Mininet e Open vSwitch.')
    lab = Lab(NetworkModel(config_dir=str(ROOT / 'config')), args.timeout)
    print(paint('Good-Fences: validazione T1–T26', '1'))
    print(f'{ovs_version()} | switch: {", ".join(lab.switches)} | cattura {lab.timeout:g}s per invio')
    cases = [entry for entry in CASES if not args.only or entry.ident in args.only]
    runner = run_case_paused if args.pause and sys.stdin.isatty() else run_case
    reports = [runner(lab, entry) for entry in cases]
    skipped_restart = False
    if not args.only or 'T26' in args.only:
        if args.no_restart or not sys.stdin.isatty():
            print('\nT26 saltato: --no-restart oppure terminale non interattivo.')
            skipped_restart = True
        else:
            if not cases:  # Con --only T26 serve prima un'esecuzione di riferimento.
                cases = list(CASES)
                reports = [run_case(lab, entry) for entry in cases]
            restart = restart_controller(lab, cases, reports)
            if restart is None:
                skipped_restart = True
            else:
                reports.append(restart)
    print_summary(reports, skipped_restart)
    return 0 if all(report.passed for report in reports) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nValidazione interrotta.')
        sys.exit(130)
