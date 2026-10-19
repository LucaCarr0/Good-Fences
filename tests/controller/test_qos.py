from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ryu.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser
from controller.pipeline import prepare_pipeline, install_entry_tables, HOST_IPV4_COOKIE
from network_models import ConfigValidationError, HostModel
import test_pipeline as support


class QosPipelineTests(unittest.TestCase):
    def setUp(self):
        self.helper = support.PipelineTests()
        self.helper.setUpClass()
        self.model = deepcopy(self.helper.model)

    def messages(self, enabled=True, dpid=1):
        messages = []
        dp = SimpleNamespace(id=dpid, ofproto=ofp, ofproto_parser=parser, send_msg=messages.append)
        prepare_pipeline(dp, self.model, enabled)
        install_entry_tables(dp, self.model, enabled)
        for message in messages:
            message.serialize()
        return messages

    def test_shared_meter_and_order_only_on_authorized_ipv4(self):
        self.model.tenants[0].hosts.append(HostModel('h4', 'leaf1', 4, '10.0.0.4'))
        messages = self.messages()
        meters = [m for m in messages if isinstance(m, parser.OFPMeterMod)]
        self.assertEqual([(m.command, m.meter_id) for m in meters],
                         [(op, tag) for tag in (101, 102, 103) for op in (ofp.OFPMC_DELETE, ofp.OFPMC_ADD)])
        for meter in meters[1::2]:
            self.assertEqual((meter.flags, meter.bands[0].rate, meter.bands[0].burst_size), (13, 20000, 2000))
        flows = [m for m in messages if isinstance(m, parser.OFPFlowMod)]
        access = [f for f in flows if f.cookie == HOST_IPV4_COOKIE]
        self.assertEqual([f.instructions[0].meter_id for f in access], [101, 101, 102, 103])
        for flow in access:
            self.assertEqual([i.type for i in flow.instructions],
                             [ofp.OFPIT_METER, ofp.OFPIT_APPLY_ACTIONS, ofp.OFPIT_GOTO_TABLE])
        self.assertFalse(any(isinstance(i, parser.OFPInstructionMeter)
                             for f in flows if f.cookie != HOST_IPV4_COOKIE for i in f.instructions))

    def test_off_deletes_only_configured_ids_and_spines_have_no_meters(self):
        messages = self.messages(False)
        self.assertEqual([(m.command, m.meter_id) for m in messages if isinstance(m, parser.OFPMeterMod)],
                         [(ofp.OFPMC_DELETE, tag) for tag in (101, 102, 103)])
        self.assertFalse(any(isinstance(i, parser.OFPInstructionMeter) for m in messages
                             if isinstance(m, parser.OFPFlowMod) for i in m.instructions))
        self.assertFalse(any(isinstance(m, parser.OFPMeterMod) for m in self.messages(dpid=101)))

    def test_invalid_rates_and_largest_valid_rate(self):
        for value in (True, False, 0, -1, 20., '20', None, 0xffffffff // 1000 + 1):
            with self.subTest(value=value), self.assertRaises(ConfigValidationError):
                self.model.tenants[0].rate_mbps = value
                self.model.validate_configuration()
        self.model.tenants[0].rate_mbps = 0xffffffff // 1000
        self.messages()

    def test_features_are_required_before_resources_and_stale_replies_are_ignored(self):
        app, dp, messages = self.helper.start_controller_installation(meter_reply=False)
        request = messages[-1]
        self.assertIsInstance(request, parser.OFPMeterFeaturesStatsRequest)
        self.assertFalse(any(isinstance(m, (parser.OFPGroupMod, parser.OFPMeterMod)) for m in messages))
        reply = SimpleNamespace(datapath=dp, xid=request.xid + 100,
                                body=[SimpleNamespace(max_meter=1000, max_bands=1, band_types=2, capabilities=13)])
        app.meter_features_reply_handler(SimpleNamespace(msg=reply))
        self.assertIs(messages[-1], request)
        with patch('controller.app.hub.spawn_after'):
            reply.xid = request.xid
            app.meter_features_reply_handler(SimpleNamespace(msg=reply))
        self.assertTrue(any(isinstance(m, parser.OFPMeterMod) for m in messages))
        self.assertIsInstance(messages[-1], parser.OFPBarrierRequest)
        self.assertFalse(app.ready)

    def test_each_missing_capability_and_timeout_prevents_ready(self):
        for change in ({'max_meter': 0}, {'max_meter': 2}, {'max_bands': 0}, {'band_types': 0},
                       {'capabilities': 12}, {'capabilities': 9}, {'capabilities': 5}, None):
            app, dp, messages = self.helper.start_controller_installation(meter_reply=False)
            xid = messages[-1].xid
            if change is None:
                app._installation_timeout(dp, xid, 'meter-features')
            else:
                values = dict(max_meter=1000, max_bands=1, band_types=2, capabilities=13)
                values.update(change)
                reply = SimpleNamespace(datapath=dp, xid=xid, body=[SimpleNamespace(**values)])
                app.meter_features_reply_handler(SimpleNamespace(msg=reply))
            self.assertFalse(app.ready)
            self.assertFalse(app.pending)
            dp.close.assert_called_once()

    def test_off_works_without_meter_support(self):
        app, dp, messages = self.helper.start_controller_installation(qos=False, meter_reply=False)
        reply = SimpleNamespace(datapath=dp, xid=messages[-1].xid,
                                body=[SimpleNamespace(max_meter=0, max_bands=0, band_types=0, capabilities=0)])
        with patch('controller.app.hub.spawn_after'):
            app.meter_features_reply_handler(SimpleNamespace(msg=reply))
        self.helper.acknowledge_barrier(app, dp, messages[-1].xid)
        self.helper.acknowledge_barrier(app, dp, messages[-1].xid)
        self.assertEqual(app.ready, {1})
        self.assertFalse(any(isinstance(m, parser.OFPMeterMod) for m in messages))


if __name__ == '__main__':
    unittest.main()
