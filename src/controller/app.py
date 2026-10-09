"""App Ryu minimale; avviare dalla radice con PYTHONPATH=src."""
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls,
)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
from network_models import NetworkModel
from controller.arp_proxy import build_arp_reply
from controller.pipeline import base_tables, install_base_pipeline


class GoodFencesApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = NetworkModel(config_dir='config')
        self.access_hosts, self.tenant_hosts = self.model.get_arp_bindings()
        self.expected_dpids = (
            set(range(1, self.model.leaves + 1))
            | set(range(101, 101 + self.model.spines))
        )
        self.datapaths = {}
        self.ready = set()
        self.pending = {}
        self.logger.info('DPID attesi: %s',
                         ', '.join(f'{d:016x}' for d in sorted(self.expected_dpids)))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        
        if dp.id not in self.expected_dpids:
            self.logger.error('DPID non previsto: %016x; connessione rifiutata', dp.id)
            dp.close()
            return
            
        if dp.ofproto.OFP_VERSION != ofproto_v1_3.OFP_VERSION:
            self.logger.error('OpenFlow non supportato per %016x', dp.id)
            dp.close()
            return
            
        if msg.n_tables <= max(base_tables(self.model, dp.id)):
            self.logger.error('Tabelle insufficienti per %016x: %s', dp.id, msg.n_tables)
            dp.close()
            return
            
        old = self.datapaths.get(dp.id)
        
        if old is not None and old is not dp:
            old.close()
            
        self.datapaths[dp.id] = dp
        self.ready.discard(dp.id)
        install_base_pipeline(dp, self.model)
        barrier = dp.ofproto_parser.OFPBarrierRequest(dp)
        dp.set_xid(barrier)
        self.pending[dp.id] = (dp, barrier.xid)
        dp.send_msg(barrier)
        hub.spawn_after(10, self._installation_timeout, dp, barrier.xid)
        self.logger.info('Connesso DPID=%016x OpenFlow=1.3; installazione inviata', dp.id)

    def _installation_timeout(self, dp, xid):
        if self.pending.get(dp.id) == (dp, xid):
            self.pending.pop(dp.id, None)
            self.logger.error('Timeout installazione DPID=%016x', dp.id)
            dp.close()

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if self.datapaths.get(dp.id) is not dp or dp.id not in self.ready:
            return
        in_port = msg.match.get('in_port')
        binding = self.access_hosts.get((dp.id, in_port))
        if binding is None or msg.table_id != 0: #solo le tabelle 0 hanno regole che contattano direttamente il controller
            return
        if len(msg.data) < msg.total_len: #controllo integrità del messaggio
            return

        reply_data = build_arp_reply(msg.data, binding, self.tenant_hosts)
        if reply_data is None:
            return
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(in_port)], data=reply_data))

    @set_ev_cls(ofp_event.EventOFPBarrierReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def barrier_reply_handler(self, ev):
        dp = ev.msg.datapath
        if self.pending.get(dp.id) != (dp, ev.msg.xid):
            return
        self.pending.pop(dp.id)
        self.ready.add(dp.id)
        self.logger.info('BASE READY DPID=%016x tabelle=%s (%d/%d)',
                         dp.id, base_tables(self.model, dp.id),
                         len(self.ready), len(self.expected_dpids))
        if self.ready == self.expected_dpids:
            self.logger.info('ALL SWITCHES READY: %d/%d',
                             len(self.ready), len(self.expected_dpids))

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        self.logger.error('OpenFlow error DPID=%016x xid=%s type=%s code=%s data=%s',
                          dp.id or 0, msg.xid, msg.type, msg.code, msg.data.hex())
        if self.datapaths.get(dp.id) is dp:
            self.pending.pop(dp.id, None)
            self.ready.discard(dp.id)
        dp.close()

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def disconnect_handler(self, ev):
        dp = ev.datapath
        if self.datapaths.get(dp.id) is dp:
            self.datapaths.pop(dp.id)
            self.pending.pop(dp.id, None)
            self.ready.discard(dp.id)
            self.logger.warning('Disconnesso DPID=%016x (%d/%d pronti)',
                                dp.id, len(self.ready), len(self.expected_dpids))
