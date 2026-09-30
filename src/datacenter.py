import argparse
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI

from network_models import NetworkModel

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


def main():
    parser = argparse.ArgumentParser(description="Good Fences - Spine-Leaf Topology")
    parser.add_argument('--controller', choices=['remote', 'none'], default='none', help='Tipo di controller (remote o none)')
    parser.add_argument('--controller-ip', default='127.0.0.1', help='IP del RemoteController')
    parser.add_argument('--controller-port', type=int, default=6653, help='Porta del RemoteController')
    parser.add_argument('--cli', action='store_true', help='Avvia la CLI di Mininet al termine del setup')
    parser.add_argument('--test-none', action='store_true', help='Esegue il test di packet loss in assenza di regole')
    args = parser.parse_args()

    setLogLevel('info')
    info('*** Inizializzazione della rete...\n')
    
    net = create_network(args.controller, args.controller_ip, args.controller_port)
    net.start()

    info('*** Disabilitazione IPv6 su tutti gli host...\n')
    for host in net.hosts:
        host.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
        host.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
        host.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")

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

    info('*** Chiusura di Mininet...\n')
    net.stop()

if __name__ == '__main__':
    main()