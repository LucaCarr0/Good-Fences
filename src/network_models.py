import re
import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional

class ConfigValidationError(Exception):
    pass

@dataclass
class HostConfig:
    name: str
    leaf: str
    port: int
    ip: Optional[str] = None
    # da calcolare poi
    host_id: int = field(init=False)
    mac: str = field(init=False)

    def __post_init__(self):
        match = re.search(r'\d+', self.name)
        if not match:
            raise ConfigValidationError(f"Impossibile estrarre host_id dal nome: {self.name}")
        self.host_id = int(match.group())
        self.mac = f"00:00:00:00:00:{self.host_id:02x}"


@dataclass
class TenantConfig:
    name: str
    tag: int
    rate_mbps: int
    subnet: str
    hosts: List[HostConfig]

    def __post_init__(self):
        if self.hosts and isinstance(self.hosts[0], dict):
            self.hosts = [HostConfig(**h) for h in self.hosts]


class NetworkModel:
    def __init__(self, config_dir="config"):
        self.config_dir = config_dir
        self.topology = {}
        self.tenants = []
        
        self.spines = 0
        self.leaves = 0
        self.hosts_per_leaf = 0

        self._load_and_validate()

    def _load_and_validate(self):
            topo_path = os.path.join(self.config_dir, "topology.yaml")
            tenants_path = os.path.join(self.config_dir, "tenants.yaml")
            
            try:
                with open(topo_path, 'r') as f:
                    self.topology = yaml.safe_load(f) or {}
                with open(tenants_path, 'r') as f:
                    tenants_data = yaml.safe_load(f) or {}
            except FileNotFoundError as e:
                raise ConfigValidationError(f"Errore nel caricamento dei file YAML: {e}")

            self.spines = self.topology.get('spines', 0)
            self.leaves = self.topology.get('leaves', 0)
            self.hosts_per_leaf = self.topology.get('hosts_per_leaf', 0)

            for t_data in tenants_data.get('tenants', []):
                self.tenants.append(TenantInfo(**t_data))

            self.validate_configuration()

    def validate_configuration(self):
        tags = set()
        assigned_hosts = set()

        for tenant in self.tenants:
            if tenant.tag is not None:
                if not (1 <= tenant.tag <= 4094):
                    raise ConfigValidationError(f"Tag VLAN {tenant.tag} non valido nel tenant {tenant.name}.")
                if tenant.tag in tags:
                    raise ConfigValidationError(f"Tag VLAN {tenant.tag} duplicato trovato nel tenant {tenant.name}.")
                tags.add(tenant.tag)

            ips = set()
            for host in tenant.hosts:
                if host.ip:
                    if host.ip in ips:
                        raise ConfigValidationError(f"IP {host.ip} duplicato nel tenant {tenant.name}.")
                    ips.add(host.ip)

                if host.name in assigned_hosts:
                    raise ConfigValidationError(f"Host {host.name} assegnato a più tenant.")
                assigned_hosts.add(host.name)

                leaf_match = re.search(r'\d+', host.leaf)
                if not leaf_match:
                    raise ConfigValidationError(f"Nome leaf malformato per l'host {host.name}: {host.leaf}")
                leaf_id = int(leaf_match.group())

                if not (1 <= leaf_id <= self.leaves):
                    raise ConfigValidationError(f"Leaf ID {leaf_id} inesistente per l'host {host.name}.")
                if not (1 <= host.port <= self.hosts_per_leaf):
                    raise ConfigValidationError(f"Porta {host.port} non valida sul leaf {leaf_id} per l'host {host.name}.")

                expected_host_id = (leaf_id - 1) * self.hosts_per_leaf + host.port
                if host.host_id != expected_host_id:
                    raise ConfigValidationError(f"host_id {host.host_id} errato per {host.name}. Atteso: {expected_host_id}.")



#tobechecked

def is_leaf(self, dpid):
        return 1 <= int(dpid) <= self.leaves

    def is_spine(self, dpid):
        return 101 <= int(dpid) <= (100 + self.spines)

    def get_leaf_uplink_ports(self):
        start = self.hosts_per_leaf + 1
        end = self.hosts_per_leaf + self.spines + 1
        return list(range(start, end))

    def get_spine_port_to_leaf(self, leaf_id):
        return int(leaf_id)

    def get_assigned_host_names(self):
        return {host.name for tenant in self.tenants for host in tenant.hosts}

    def get_spare_hosts(self):
        assigned = self.get_assigned_host_names()
        spare_hosts = []
        for leaf_id in range(1, self.leaves + 1):
            for port in range(1, self.hosts_per_leaf + 1):
                host_id = (leaf_id - 1) * self.hosts_per_leaf + port
                host_name = f"h{host_id}"
                if host_name not in assigned:
                    spare_hosts.append(HostInfo(name=host_name, leaf=f"leaf{leaf_id}", port=port))
        return spare_hosts

    def get_all_hosts_dict(self):
        hosts_dict = {}
        for tenant in self.tenants:
            for host in tenant.hosts:
                hosts_dict[host.name] = host
        for host in self.get_spare_hosts():
            hosts_dict[host.name] = host
        return hosts_dict

    def get_tenant_by_tag(self, tag):
        for tenant in self.tenants:
            if tenant.tag == tag:
                return tenant
        return None

    def get_tenant_by_host_port(self, leaf, port):
        for tenant in self.tenants:
            for host in tenant.hosts:
                if host.leaf == leaf and host.port == int(port):
                    return tenant
        return None

    def add_tenant(self, name, tag, rate_mbps, subnet, host_names):
        if any(t.name == name for t in self.tenants):
            raise ConfigValidationError(f"Impossibile creare il tenant: {name} esiste già.")

        if tag is None:
            existing_tags = {t.tag for t in self.tenants if t.tag is not None}
            tag = 101
            while tag in existing_tags:
                tag += 1
            if tag > 4094:
                raise ConfigValidationError("Spazio per tag VLAN esaurito.")

        spare_dict = {h.name: h for h in self.get_spare_hosts()}
        new_hosts = []
        
        network = ipaddress.IPv4Network(subnet)
        ip_generator = network.hosts()

        for h_name in host_names:
            if h_name not in spare_dict:
                raise ConfigValidationError(f"L'host {h_name} non è disponibile (non è spare).")
            
            base_host = spare_dict[h_name]
            try:
                assigned_ip = str(next(ip_generator))
            except StopIteration:
                raise ConfigValidationError(f"IP insufficienti nella subnet {subnet} per l'host {h_name}.")
            
            new_hosts.append(HostInfo(
                name=base_host.name,
                leaf=base_host.leaf,
                port=base_host.port,
                ip=assigned_ip
            ))

        new_tenant = TenantInfo(name=name, tag=tag, rate_mbps=rate_mbps, subnet=subnet, hosts=new_hosts)
        self.tenants.append(new_tenant)
        
        try:
            self.validate_configuration()
        except ConfigValidationError:
            self.tenants.remove(new_tenant)
            raise
            
        return new_tenant

    def delete_tenant(self, name):
        original_count = len(self.tenants)
        self.tenants = [t for t in self.tenants if t.name != name]
        if len(self.tenants) == original_count:
            raise ConfigValidationError(f"Impossibile eliminare: il tenant {name} non esiste.")