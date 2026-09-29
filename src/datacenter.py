from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI

from model import NetworkModel

class SpineLeafTopo(Topo):
    def build(self):
        # Inizializza il modello leggendo i file da config/
        self.net_model = NetworkModel(config_dir='config')
        
        uplink_bw = self.net_model.topology.get('uplink_bw_mbps', 60)
        host_bw = self.net_model.topology.get('host_bw_mbps', 100)
        link_delay = f"{self.net_model.topology.get('link_delay_ms', 1)}ms"

        spines = []
        leaves = []

        # Creazione degli switch Spine
        for i in range(1, self.net_model.spines + 1):
            spine_name = f'spine{i}'
            dpid = str(100 + i)
            spines.append(self.addSwitch(spine_name, dpid=dpid, cls=OVSKernelSwitch))

        # Creazione degli switch Leaf e cablaggio Uplink
        for i in range(1, self.net_model.leaves + 1):
            leaf_name = f'leaf{i}'
            dpid = str(i)
            leaf = self.addSwitch(leaf_name, dpid=dpid, cls=OVSKernelSwitch)
            leaves.append(leaf)

            for s_idx, spine in enumerate(spines):
                spine_port = self.net_model.get_spine_port_to_leaf(i)
                leaf_port = self.net_model.hosts_per_leaf + s_idx + 1
                
                self.addLink(leaf, spine, port1=leaf_port, port2=spine_port, 
                             bw=uplink_bw, delay=link_delay)

        # Creazione degli Host (Assegnati e Spare)
        all_hosts = self.net_model.get_all_hosts_dict()
        
        for host_name, host_obj in all_hosts.items():
            params = {'mac': host_obj.mac}
            if host_obj.ip:
                params['ip'] = f"{host_obj.ip}/24"
                
            h_node = self.addHost(host_obj.name, **params)
            
            # Collegamento host-leaf rigoroso sulle porte previste
            self.addLink(h_node, host_obj.leaf, port1=0, port2=host_obj.port, 
                         bw=host_bw, delay=link_delay)

def run():
    setLogLevel('info')
    info('*** Costruzione della Topologia Spine-Leaf...\n')
    topo = SpineLeafTopo()
    
    info('*** Avvio di Mininet con RemoteController SDN...\n')
    net = Mininet(topo=topo, link=TCLink, switch=OVSKernelSwitch, controller=RemoteController)
    net.start()
    
    info('*** Avvio della Mininet CLI...\n')
    CLI(net)
    
    info('*** Chiusura di Mininet...\n')
    net.stop()

if __name__ == '__main__':
    run()