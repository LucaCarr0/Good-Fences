"""Default deny e ammissione dello sprint 1.2; nessun forwarding dati."""

from ryu.lib.packet import arp, ether_types

BASE_COOKIE = 0x47460010
TAG_DROP_COOKIE = 0x47460020
ARP_DROP_COOKIE = 0x47460021
ARP_COOKIE = 0x47460030
HOST_IPV4_COOKIE = 0x47460040
UPLINK_IPV4_COOKIE = 0x47460041
TRANSIT_COOKIE = 0x47460042


def base_tables(model, dpid):
    if model.is_leaf(dpid):
        return (0, 1, 2)
    if model.is_spine(dpid):
        return (0,)
    raise ValueError(f'DPID sconosciuto: {dpid:016x}')


def _add_flow(datapath, table, priority, cookie, match, instructions=()):
    ofp, parser = datapath.ofproto, datapath.ofproto_parser
    datapath.send_msg(parser.OFPFlowMod(
        datapath=datapath, cookie=cookie, command=ofp.OFPFC_ADD,
        table_id=table, priority=priority, idle_timeout=0, hard_timeout=0,
        buffer_id=ofp.OFP_NO_BUFFER, match=parser.OFPMatch(**match),
        instructions=list(instructions)))


def install_base_pipeline(datapath, model):
    """Rimuove i flow precedenti e installa soltanto i table-miss DROP."""
    tables = base_tables(model, datapath.id)
    ofp, parser = datapath.ofproto, datapath.ofproto_parser
    datapath.send_msg(parser.OFPFlowMod(
        datapath=datapath, command=ofp.OFPFC_DELETE,
        table_id=ofp.OFPTT_ALL, out_port=ofp.OFPP_ANY,
        out_group=ofp.OFPG_ANY, cookie=0, cookie_mask=0,
        match=parser.OFPMatch()))
    for table in tables:
        _add_flow(datapath, table, 0, BASE_COOKIE, {})


def install_entry_tables(datapath, model):
    """Installa ARP in tabella 0 e classificazione IPv4 fino al DROP finale."""
    # Validare prima di inviare qualsiasi regola permissiva.
    model.validate_configuration()
    access_hosts, _ = model.get_arp_bindings()
    install_base_pipeline(datapath, model)
    if not model.is_leaf(datapath.id):
        return

    ofp, parser = datapath.ofproto, datapath.ofproto_parser
    for port in range(1, model.hosts_per_leaf + 1):
        _add_flow(datapath, 0, 400, TAG_DROP_COOKIE, dict(
            in_port=port, vlan_vid=(ofp.OFPVID_PRESENT, ofp.OFPVID_PRESENT)))
        # Rifiuta anche TPID residui che OVS non interpreta come VLAN.
        for eth_type in (0x8100, 0x88a8, 0x9100):
            _add_flow(datapath, 0, 400, TAG_DROP_COOKIE,
                      dict(in_port=port, eth_type=eth_type))

    for (leaf_id, port), (tag, host) in access_hosts.items():
        if leaf_id != datapath.id:
            continue
        arp_match = dict(in_port=port, vlan_vid=ofp.OFPVID_NONE,
                         eth_type=ether_types.ETH_TYPE_ARP)
        _add_flow(datapath, 0, 350, ARP_DROP_COOKIE,
                  dict(arp_match, arp_spa='0.0.0.0'))
        _add_flow(datapath, 0, 350, ARP_DROP_COOKIE,
                  dict(arp_match, arp_tpa=host.ip))
        _add_flow(datapath, 0, 300, ARP_COOKIE, dict(
            arp_match, arp_op=arp.ARP_REQUEST, eth_src=host.mac,
            arp_sha=host.mac, arp_spa=host.ip), [
                parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [
                    parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                           ofp.OFPCML_NO_BUFFER)])])
        vlan_tag_match = dict(
            in_port=port,
            vlan_vid=ofp.OFPVID_NONE,
            eth_type=ether_types.ETH_TYPE_IP,
            eth_src=host.mac,
            ipv4_src=host.ip)
        _add_flow(datapath, 0, 200, HOST_IPV4_COOKIE, vlan_tag_match, [
                parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [
                    parser.OFPActionPushVlan(0x8100),
                    parser.OFPActionSetField(vlan_vid=ofp.OFPVID_PRESENT | tag)]),
                parser.OFPInstructionGotoTable(1)])

    _add_flow(datapath, 1, 300, ARP_DROP_COOKIE,
              dict(eth_type=ether_types.ETH_TYPE_ARP))
    for tenant in model.tenants:
        tagged_ipv4 = dict(eth_type=ether_types.ETH_TYPE_IP,
                           vlan_vid=ofp.OFPVID_PRESENT | tenant.tag)
        for port in model.get_leaf_uplink_ports():
            _add_flow(datapath, 0, 200, UPLINK_IPV4_COOKIE,
                      dict(tagged_ipv4, in_port=port),
                      [parser.OFPInstructionGotoTable(1)])
        _add_flow(datapath, 1, 200, TRANSIT_COOKIE, tagged_ipv4,
                  [parser.OFPInstructionGotoTable(2)])
