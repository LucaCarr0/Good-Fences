"""Validazione delle richieste e costruzione delle risposte ARP per tenant."""
from ryu.lib.packet import arp, ethernet, ether_types, packet


def build_arp_reply(data, binding, tenant_hosts):
    """Restituisce il frame di risposta, oppure None se la richiesta va scartata.

    binding contiene (tag tenant, host mittente), ricavato dalla porta di ingresso.
    tenant_hosts associa (tag tenant, IP) all'host destinatario configurato.
    La funzione non invia messaggi OpenFlow e non modifica i binding.
    """
    pkt = packet.Packet(data)
    eth = pkt.get_protocol(ethernet.ethernet)
    request = pkt.get_protocol(arp.arp)
    tenant_tag, source = binding
    if (eth is None or request is None
        or eth.ethertype != ether_types.ETH_TYPE_ARP
        or request.opcode != arp.ARP_REQUEST):
        return None
    target = tenant_hosts.get((tenant_tag, request.dst_ip))
    if target is None:
        return None

    reply = packet.Packet()
    reply.add_protocol(ethernet.ethernet(
        dst=source.mac, src=target.mac,
        ethertype=ether_types.ETH_TYPE_ARP))
    reply.add_protocol(arp.arp(
        opcode=arp.ARP_REPLY,
        src_mac=target.mac, src_ip=target.ip,
        dst_mac=source.mac, dst_ip=source.ip))
    reply.serialize()
    return reply.data
