"""Default drop e richieste ARP al controller dalle sole porte host assegnate."""

from ryu.lib.packet import arp, ether_types

BASE_COOKIE = 0x47460010


def base_tables(model, dpid):
    if model.is_leaf(dpid):
        return (0, 1, 2)
    if model.is_spine(dpid):
        return (0,)
    raise ValueError(f'DPID sconosciuto: {dpid:016x}')


def install_base_pipeline(datapath, model):
    '''
    datapath : astrazione dello switch openflow

    ''' 
    
    tables = base_tables(model,datapath.id) 
    '''
    ofp : serve a utilizzare costanti di libreria come OFPFC_DELETE, ADD ecc.
    parser : serve per costruire messaggi openflow
    '''
    
    ofp, parser = datapath.ofproto, datapath.ofproto_parser 
    
    #CLEAR DI TUTTE LE TABELLE (inizializzazione)
    
    datapath.send_msg(parser.OFPFlowMod(
        datapath=datapath, command=ofp.OFPFC_DELETE,
        table_id=ofp.OFPTT_ALL, out_port=ofp.OFPP_ANY,
        out_group=ofp.OFPG_ANY, cookie=0, cookie_mask=0,
        match=parser.OFPMatch()))
    
    #INIZIALIZZAZIONE TABELLE CON DEFAULT DROP
        
    for table_id in tables:
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, cookie=BASE_COOKIE,
            command=ofp.OFPFC_ADD, table_id=table_id,
            priority=0, idle_timeout=0, hard_timeout=0,
            buffer_id=ofp.OFP_NO_BUFFER,
            match=parser.OFPMatch(), instructions=[]))

    if model.is_leaf(datapath.id):
        access_hosts, _ = model.get_arp_bindings()
        for leaf_id, port in access_hosts:
            if leaf_id != datapath.id:
                continue
            datapath.send_msg(parser.OFPFlowMod(
                datapath=datapath, cookie=BASE_COOKIE,
                table_id=0, priority=100,
                match=parser.OFPMatch(
                    in_port=port, vlan_vid=ofp.OFPVID_NONE,
                    eth_type=ether_types.ETH_TYPE_ARP,
                    arp_op=arp.ARP_REQUEST),
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionOutput(
                        ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])]))
