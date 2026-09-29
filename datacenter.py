import yaml
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI

class SpineLeafTopo(Topo):
    def build(self):
        # 1. Caricamento dei file YAML
        with open('topology.yaml', 'r') as f:
            topo_cfg = yaml.safe_load(f)
        
        with open('tenants.yaml', 'r') as f:
            tenants_cfg = yaml.safe_load(f)

        # 2. Estrazione parametri fisici e QoS
        num_spines = topo_cfg['spines']
        num_leaves = topo_cfg['leaves']
        uplink_bw = topo_cfg['uplink_bw_mbps']
        host_bw = topo_cfg['host_bw_mbps']
        link_delay = f"{topo_cfg['link_delay_ms']}ms"

        spines = []
        leaves = []

        # 3. Creazione degli switch Spine
        for i in range(1, num_spines + 1):
            spine_name = f'spine{i}'
            spines.append(self.addSwitch(spine_name, cls=OVSKernelSwitch))

        # 4. Creazione degli switch Leaf e dei collegamenti Uplink (Full-Mesh)
        for i in range(1, num_leaves + 1):
            leaf_name = f'leaf{i}'
            leaf = self.addSwitch(leaf_name, cls=OVSKernelSwitch)
            leaves.append(leaf)

            for spine in spines:
                # Applichiamo la QoS (banda uplink e ritardo)
                self.addLink(leaf, spine, bw=uplink_bw, delay=link_delay)

        # 5. Creazione degli Host assegnati ai vari Tenant
        for tenant in tenants_cfg['tenants']:
            for host in tenant['hosts']:
                # Creiamo l'host assegnandogli l'IP specificato nel file YAML
                h_node = self.addHost(host['name'], ip=f"{host['ip']}/24")
                
                # Lo colleghiamo al leaf corretto, sulla porta indicata e con la QoS per l'host
                self.addLink(h_node, host['leaf'], port2=host['port'], bw=host_bw, delay=link_delay)

        # 6. Creazione degli Spare Hosts (non configurati in alcun tenant per ora)
        for spare in tenants_cfg.get('spare_hosts', []):
            h_node = self.addHost(spare['name'])
            self.addLink(h_node, spare['leaf'], port2=spare['port'], bw=host_bw, delay=link_delay)

def run():
    # Imposta il livello di log per vedere l'output nel terminale
    setLogLevel('info')
    
    info('*** Costruzione della Topologia Spine-Leaf...\n')
    topo = SpineLeafTopo()
    
    info('*** Avvio di Mininet...\n')
    # È cruciale passare link=TCLink per far funzionare i parametri bw e delay
    net = Mininet(topo=topo, link=TCLink, switch=OVSKernelSwitch)
    net.start()
    
    info('*** Avvio della Mininet CLI...\n')
    CLI(net)
    
    info('*** Chiusura di Mininet...\n')
    net.stop()

if __name__ == '__main__':
    run()