from dataclasses import dataclass
from typing import List

@dataclass
class HostConfig:
    name: str
    leaf: str
    port: int
    ip: str = None # Opzionale per i dispari (spare_hosts)

@dataclass
class TenantConfig:
    name: str
    tag: int
    rate_mbps: int
    subnet: str
    hosts: List[HostConfig]