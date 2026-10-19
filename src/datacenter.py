import argparse
from contextlib import suppress
import fcntl
import os
import pwd
import select
import signal
import subprocess
import sys
import termios
import threading
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI

from network_models import NetworkModel
from runtime_status import write_status

class SpineLeafTopo(Topo):
    def build(self):
        self.net_model = NetworkModel(config_dir='config')
        
        uplink_bw = self.net_model.topology.get('uplink_bw_mbps')
        host_bw = self.net_model.topology.get('host_bw_mbps')
        link_delay = f"{self.net_model.topology.get('link_delay_ms', 1)}ms"

        spines = [] # Liste di switch
        leaves = []

        for i in range(1, self.net_model.spines + 1):
            spine_name = f'spine{i}'
            dpid = f"{100 + i:016x}"
            spines.append(self.addSwitch(spine_name, dpid=dpid))

        for i in range(1, self.net_model.leaves + 1):
            leaf_name = f'leaf{i}'
            dpid = f"{i:016x}"
            leaf = self.addSwitch(leaf_name, dpid=dpid)
            leaves.append(leaf)

            for s_idx, spine in enumerate(spines):
                spine_port = self.net_model.get_spine_port_to_leaf(i)
                leaf_port = self.net_model.hosts_per_leaf + s_idx + 1
                
                self.addLink(leaf, spine, port1=leaf_port, port2=spine_port, 
                             bw=uplink_bw, delay=link_delay)

        all_hosts = self.net_model.get_all_hosts_dict()
        
        for host_name, host_obj in all_hosts.items():
            params = {'mac': host_obj.mac}
            if host_obj.ip:
                params['ip'] = f"{host_obj.ip}/24"
                
            h_node = self.addHost(host_obj.name, **params)
            
            self.addLink(host_obj.leaf, h_node, port1=host_obj.port, port2=0, 
                         bw=host_bw, delay=link_delay)

def create_network(controller_mode, controller_ip, controller_port):
    topo = SpineLeafTopo()
    
    ctrl = None
    if controller_mode == 'remote':
        ctrl = RemoteController('c0', ip=controller_ip, port=controller_port)

    # Factory per forzare i parametri di sicurezza richiesti su tutti gli switch
    def SecureOVSSwitch(name, **kwargs):
        kwargs.update(protocols='OpenFlow13', failMode='secure')
        return OVSSwitch(name, **kwargs)

    net = Mininet(
        topo=topo,
        link=TCLink,
        switch=SecureOVSSwitch,
        controller=ctrl,
        autoSetMacs=False,
        autoStaticArp=False
    )
    return net


def open_local_cli(net, stopped):
    """Collega la CLI della rete esistente a una finestra xterm."""
    class ConsoleClosed(BaseException):
        pass

    def close_console(signum, frame):
        if signum == signal.SIGTERM:
            stopped.set()
        raise ConsoleClosed()

    master, slave = os.openpty()
    saved = [os.dup(fd) for fd in range(3)]
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    desktop = pwd.getpwuid(int(os.environ.get('SUDO_UID', os.getuid())))
    environment = dict(os.environ, HOME=desktop.pw_dir, USER=desktop.pw_name, LOGNAME=desktop.pw_name)
    credentials = ({'user': desktop.pw_uid, 'group': desktop.pw_gid, 'extra_groups': []}
                   if os.geteuid() == 0 else {})
    terminal = None
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, close_console)
        signal.signal(signal.SIGHUP, close_console)
        terminal = subprocess.Popen(
            ['xterm', '-title', 'Good-Fences · Mininet', '-geometry', '110x32',
             f'-S{os.ttyname(slave)}/{master}'],
            pass_fds=(master,), start_new_session=True, env=environment, **credentials)
        if not select.select([slave], [], [], 5)[0]:
            raise RuntimeError('xterm non apre la finestra: controllare DISPLAY e i permessi del desktop.')
        os.read(slave, 80)  # ID della finestra inviato da xterm prima del prompt.
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in range(3):
            os.dup2(slave, fd)
        os.close(master)
        master = None
        CLI(net, stdin=sys.stdin)
    except (ConsoleClosed, KeyboardInterrupt):
        pass
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        for stream in (sys.stdout, sys.stderr):
            with suppress(OSError):
                stream.flush()
        for fd, original in enumerate(saved):
            os.dup2(original, fd)
            os.close(original)
        with suppress(OSError):
            fcntl.ioctl(slave, termios.TIOCNOTTY, 0)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if terminal is not None:
            if terminal.poll() is None:
                terminal.terminate()
            try:
                terminal.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminal.kill()
                terminal.wait()
        if master is not None:
            os.close(master)
        os.close(slave)
        for node in net.values():
            if node.waiting:
                node.sendInt()


def main():
    parser = argparse.ArgumentParser(description="Good Fences - Spine-Leaf Topology")
    parser.add_argument('--controller', choices=['remote', 'none'], default='none', help='Tipo di controller (remote o none)')
    parser.add_argument('--controller-ip', default='127.0.0.1', help='IP del RemoteController')
    parser.add_argument('--controller-port', type=int, default=6653, help='Porta del RemoteController')
    parser.add_argument('--cli', action='store_true', help='Avvia la CLI di Mininet al termine del setup')
    parser.add_argument('--test-none', action='store_true', help='Esegue il test di packet loss in assenza di regole')
    parser.add_argument('--headless', action='store_true',
                        help='Mantiene la rete attiva senza CLI fino a SIGTERM/SIGINT')
    parser.add_argument('--state-file', help='Snapshot JSON degli host per la GUI')
    parser.add_argument('--local-cli', action='store_true', help='Abilita la CLI locale su richiesta della GUI')
    args = parser.parse_args()
    if args.headless and (args.cli or args.test_none):
        parser.error('--headless non è compatibile con --cli o --test-none')
    if args.state_file and not args.headless:
        parser.error('--state-file richiede --headless')
    if args.local_cli and not args.headless:
        parser.error('--local-cli richiede --headless')

    setLogLevel('info')
    info('*** Inizializzazione della rete...\n')
    
    net = create_network(args.controller, args.controller_ip, args.controller_port)
    stopped = threading.Event()
    if args.headless:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda signum, frame: stopped.set())
    if args.local_cli:
        console_open = False

        def open_console(signum, frame):
            nonlocal console_open
            if console_open:
                return
            console_open = True
            try:
                open_local_cli(net, stopped)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                info(f'*** Shell Mininet: {error}\n')
            finally:
                console_open = False

        signal.signal(signal.SIGUSR1, open_console)
    try:
        run_network(net, args, stopped)
    finally:
        info('*** Chiusura di Mininet...\n')
        try:
            net.stop()
        finally:
            write_status(args.state_file, {'state': 'stopped', 'hosts': {}})


def run_network(net, args, stopped):
    net.start()

    info('*** Disabilitazione IPv6 su tutti gli host...\n')
    for host in net.hosts:
        host.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
        host.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
        host.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")

    if args.headless:
        write_status(args.state_file, {
            'state': 'running',
            'hosts': {host.name: host.pid for host in net.hosts},
        })
        stopped.wait()
        return

    if args.test_none:
        info('*** Esecuzione test Phase F1: Ping da h1 a h5...\n')
        h1 = net.get('h1')
        h5 = net.get('h5')
        
        # Esegue un ping con 3 pacchetti
        ping_output = h1.cmd(f'ping -c 3 {h5.IP()}')
        info(ping_output + '\n')
        
        # Verifica della modalità failMode='secure'
        if "100% packet loss" in ping_output:
            info('*** SUCCESS: Traffico bloccato correttamente (100% packet loss).\n')
        else:
            info('*** FAILURE: Il traffico sta passando (FailMode non secure o switch in learning mode).\n')

    if args.cli:
        info('*** Avvio della Mininet CLI...\n')
        CLI(net)

if __name__ == '__main__':
    main()
