"""Classificazione e forwarding unicast per tenant sul primo uplink."""

from ryu.lib.packet import arp, ether_types

BASE_COOKIE = 0x47460010
TAG_DROP_COOKIE = 0x47460020
ARP_DROP_COOKIE = 0x47460021
ARP_COOKIE = 0x47460030
HOST_IPV4_COOKIE = 0x47460040
UPLINK_IPV4_COOKIE = 0x47460041
TRANSIT_COOKIE = 0x47460042
LOCAL_COOKIE = 0x47460050
UPLINK_COOKIE = 0x47460051
SPINE_COOKIE = 0x47460052


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
    """Installa classificazione, proxy ARP e forwarding statico per tenant."""
    # Validare prima di inviare qualsiasi regola permissiva.
    model.validate_configuration()
    access_hosts, _ = model.get_arp_bindings()
    install_base_pipeline(datapath, model)
    if not model.is_leaf(datapath.id):
        _install_spine_forwarding(datapath, model, access_hosts)
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
        for port in model.get_leaf_uplink_ports()[:1]:
            _add_flow(datapath, 0, 200, UPLINK_IPV4_COOKIE,
                      dict(tagged_ipv4, in_port=port),
                      [parser.OFPInstructionGotoTable(1)])
    _install_leaf_forwarding(datapath, model, access_hosts)


def _install_leaf_forwarding(datapath, model, access_hosts):
    ofp, parser = datapath.ofproto, datapath.ofproto_parser
    for (leaf_id, port), (tag, host) in access_hosts.items():
        destination = dict(vlan_vid=ofp.OFPVID_PRESENT | tag, eth_dst=host.mac)
        # Verifica la coppia MAC/IP nel tenant una sola volta, prima dell'output.
        _add_flow(datapath, 1, 200, TRANSIT_COOKIE, dict(
            destination, eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=host.ip),
            [parser.OFPInstructionGotoTable(2)])
        if leaf_id == datapath.id:
            _add_flow(datapath, 2, 200, LOCAL_COOKIE, destination, [
                parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [
                    parser.OFPActionPopVlan(), parser.OFPActionOutput(port)])])
        elif model.spines:
            # Nessuna regola uplink -> uplink: soltanto gli host originano transito.
            for (source_leaf, source_port), (source_tag, _) in access_hosts.items():
                if source_leaf == datapath.id and source_tag == tag:
                    _add_flow(datapath, 2, 200, UPLINK_COOKIE,
                              dict(destination, in_port=source_port), [
                        parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [
                            parser.OFPActionOutput(model.hosts_per_leaf + 1)])])


def _install_spine_forwarding(datapath, model, access_hosts):

    if datapath.id != 101:
        return
    ofp, parser = datapath.ofproto, datapath.ofproto_parser
    for (leaf_id, _), (tag, host) in access_hosts.items(): # scorre tutti gli host della rete (9 hosts)
        out_port = model.get_spine_port_to_leaf(leaf_id)
        for source_leaf in range(1, model.leaves + 1):
            if source_leaf == leaf_id: # 2 regole per leaf: 18 regole totali, 
                continue
            _add_flow(datapath, 0, 200, SPINE_COOKIE, dict(
                in_port=model.get_spine_port_to_leaf(source_leaf),
                eth_type=ether_types.ETH_TYPE_IP,
                vlan_vid=ofp.OFPVID_PRESENT | tag,
                eth_dst=host.mac, ipv4_dst=host.ip), [
                    parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [
                        parser.OFPActionOutput(out_port)])])
