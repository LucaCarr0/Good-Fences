"""App Ryu minimale; avviare dalla radice con PYTHONPATH=src."""
import os
import uuid
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls,
)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
from network_models import NetworkModel
from controller.arp_proxy import build_arp_reply
from controller.pipeline import (base_tables, install_entry_tables, prepare_pipeline,
                                 prepare_resources, leaf_tenants)
from runtime_status import write_status


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
        mode = os.environ.get('GOOD_FENCES_QOS', 'on')
        if mode not in ('on', 'off'):
            raise ValueError('GOOD_FENCES_QOS deve essere on oppure off')
        self.qos_enabled = mode == 'on'
        self.generation = uuid.uuid4().hex
        self.meter_features = {}
        self.status_file = os.environ.get('GOOD_FENCES_STATUS_FILE')
        self._publish_status()
        self.logger.info('DPID attesi: %s',
                         ', '.join(f'{d:016x}' for d in sorted(self.expected_dpids)))

    def _publish_status(self, error=None):
        path = getattr(self, 'status_file', None)
        if path:
            try:
                write_status(path, {'ready': sorted(self.ready),
                                    'expected': sorted(self.expected_dpids),
                                    'qos_enabled': self.qos_enabled,
                                    'generation': self.generation,
                                    'meter_features': self.meter_features,
                                    'error': error})
            except OSError as failure:
                self.logger.error('Impossibile pubblicare lo stato GUI: %s', failure)

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
        self._publish_status()
        prepare_pipeline(dp, self.model, self.qos_enabled, resources=False)
        if self.model.is_leaf(dp.id):
            request = dp.ofproto_parser.OFPMeterFeaturesStatsRequest(dp)
            dp.set_xid(request)
            self.pending[dp.id] = (dp, request.xid, 'meter-features')
            dp.send_msg(request)
            hub.spawn_after(10, self._installation_timeout, dp, request.xid, 'meter-features')
        else:
            install_entry_tables(dp, self.model)
            self._request_barrier(dp, 'flows')
        self.logger.info('Connesso DPID=%016x OpenFlow=1.3; installazione inviata', dp.id)

    @set_ev_cls(ofp_event.EventOFPMeterFeaturesStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def meter_features_reply_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if self.pending.get(dp.id) != (dp, msg.xid, 'meter-features'):
            return
        required = dp.ofproto.OFPMF_KBPS | dp.ofproto.OFPMF_BURST | dp.ofproto.OFPMF_STATS
        features = msg.body[0] if len(msg.body) == 1 else None
        supported = bool(features and features.max_meter)
        if self.qos_enabled and (not supported or features.max_bands < 1
                or not features.band_types & (1 << dp.ofproto.OFPMBT_DROP)
                or features.capabilities & required != required
                or features.max_meter < len(leaf_tenants(self.model, dp.id))):
            self.pending.pop(dp.id, None)
            error = f'Meter non supportati o capacità insufficienti DPID={dp.id}'
            self.logger.error(error)
            self._publish_status(error)
            dp.close()
            return
        self.meter_features[dp.id] = ({name: getattr(features, name) for name in
                                      ('max_meter', 'max_bands', 'band_types', 'capabilities')}
                                     if features else {})
        self.pending.pop(dp.id, None)
        prepare_resources(dp, self.model, self.qos_enabled, supported)
        self._request_barrier(dp, 'groups')

    def _request_barrier(self, dp, phase):
        """Attende la fase corrente, con lo stesso timeout per gruppi e flow."""
        barrier = dp.ofproto_parser.OFPBarrierRequest(dp)
        dp.set_xid(barrier)
        self.pending[dp.id] = (dp, barrier.xid, phase)
        dp.send_msg(barrier)
        hub.spawn_after(10, self._installation_timeout, dp, barrier.xid, phase)

    def _installation_timeout(self, dp, xid, phase):
        if self.pending.get(dp.id) == (dp, xid, phase):
            self.pending.pop(dp.id, None)
            self.ready.discard(dp.id)
            self._publish_status(f'Timeout DPID={dp.id} fase={phase}')
            self.logger.error('Timeout installazione DPID=%016x fase=%s', dp.id, phase)
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
        pending = self.pending.get(dp.id)
        if (pending is None or pending[:2] != (dp, ev.msg.xid)
                or pending[2] not in ('groups', 'flows')):
            return
        self.pending.pop(dp.id)
        if pending[2] == 'groups':
            install_entry_tables(dp, self.model, self.qos_enabled)
            self._request_barrier(dp, 'flows')
            return
        self.ready.add(dp.id)
        self._publish_status()
        self.logger.info('PIPELINE READY DPID=%016x tabelle=%s (%d/%d)',
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
            self._publish_status(f'Errore OpenFlow DPID={dp.id}: tipo={msg.type}, codice={msg.code}')
        dp.close()

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def disconnect_handler(self, ev):
        dp = ev.datapath
        if self.datapaths.get(dp.id) is dp:
            self.datapaths.pop(dp.id)
            self.pending.pop(dp.id, None)
            self.ready.discard(dp.id)
            self._publish_status(f'Switch disconnesso: DPID={dp.id}')
            self.logger.warning('Disconnesso DPID=%016x (%d/%d pronti)',
                                dp.id, len(self.ready), len(self.expected_dpids))
