from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.qos_stats import Meter, meter_delta, parse_meters
from src.qos_experiment import Experiment, aggregate_trials, congestion_evidence, limit_check, summarize_stream, write_csv

CONFIG = 'OFPST_METER_CONFIG reply (OF1.3):\nmeter=101 kbps burst stats bands=type=drop rate=20000 burst_size=2000\n'
STATS = 'OFPST_METER reply (OF1.3):\nmeter:101 flow_count:1 packet_in_count:100 byte_in_count:120000 duration:3.2s bands:\n0: packet_count:20 byte_count:24000\n'


class MeterTests(unittest.TestCase):
    def test_parse_empty_and_populated_meter_snapshots(self):
        meter = parse_meters(CONFIG, STATS).meters[0]
        self.assertEqual((meter.meter_id, meter.flags, meter.bands, meter.drop_packets),
                         (101, ('burst', 'kbps', 'stats'), (('drop', 20000, 2000),), 20))
        self.assertFalse(parse_meters('OFPST_METER_CONFIG reply', 'OFPST_METER reply').meters)
        self.assertEqual(parse_meters(CONFIG.replace('bands=type=', 'bands=\ntype='), STATS).meters[0], meter)

    def test_missing_counters_reset_and_configuration_change_are_not_zero(self):
        meter = parse_meters(CONFIG, STATS).meters[0]
        for current, reason in ((replace(meter, duration=1), 'reset'),
                                (replace(meter, packets=1), 'reset'),
                                (replace(meter, flags=()), 'configurazione cambiata'),
                                (replace(meter, drop_bytes=None), 'contatori indisponibili')):
            self.assertEqual(meter_delta(current, meter), (None, reason))
        self.assertIsNone(parse_meters(CONFIG, STATS.replace('byte_count:24000', 'byte_count:?')).meters[0].drop_bytes)
        self.assertEqual(meter_delta(meter, meter)[0]['drop_packets'], 0)

    def test_racing_or_malformed_meter_dump_is_rejected(self):
        for config, stats in ((CONFIG, 'garbage'), (CONFIG, STATS.replace('meter:101', 'meter:102')),
                              (CONFIG + CONFIG.split('\n')[1], STATS)):
            with self.assertRaises(ValueError):
                parse_meters(config, stats)

    def test_limit_uses_observed_accepted_bytes_duration_and_burst(self):
        before = Meter(101, ('burst', 'kbps', 'stats'), (('drop', 20000, 2000),), 1, 0, 0, 0, 0, 1.)
        after = replace(before, packets=150000, byte_count=150000000,
                        drop_packets=75000, drop_bytes=75000000, duration=31.)
        result = limit_check(before, after, 30, 20)
        self.assertEqual(result['limit_status'], 'verificato')
        self.assertEqual(result['admitted_meter_mbps'], 20)
        self.assertGreater(result['limit_upper_mbps'], 21)
        self.assertEqual(limit_check(before, replace(after, drop_bytes=None), 30, 20)['limit_status'], 'incompleto')


class ExperimentMetricsTests(unittest.TestCase):
    def test_loss_uses_actual_sends_and_missing_jitter_stays_missing(self):
        sent = {'intervals': [dict(second=0, packets=10, bytes=12000)]}
        received = {'intervals': [dict(second=0, packets=8, bytes=9600, jitter_ms=None)]}
        result = summarize_stream(sent, received)[0]
        self.assertEqual(result['loss_pct'], 20)
        self.assertEqual(result['goodput_mbps'], .0768)
        self.assertIsNone(result['jitter_ms'])
        with self.assertRaises(ValueError):
            summarize_stream(sent, {'intervals': [dict(second=0, packets=11, bytes=13200, jitter_ms=0)]})

    def sample(self, second, drops=0, backlog=0, tx=None):
        return {'links': {'leaf1-eth5': dict(at=second, tx_bytes=tx if tx is not None else second * 7500000,
                  qdiscs=[dict(handle='5:', kind='htb', bytes=second * 7500000, drops=drops, backlog=backlog)])}}

    def test_load_alone_does_not_demonstrate_congestion(self):
        samples = [self.sample(i) for i in range(4)]
        self.assertEqual(congestion_evidence(samples, ['leaf1-eth5'], 60), [])
        self.assertTrue(congestion_evidence([self.sample(0), self.sample(1, drops=1)], ['leaf1-eth5'], 60))
        self.assertTrue(congestion_evidence([self.sample(i, backlog=100) for i in range(4)], ['leaf1-eth5'], 60))
        self.assertFalse(congestion_evidence([self.sample(0), self.sample(1, drops=1, tx=100)], ['leaf1-eth5'], 60))
        self.assertFalse(congestion_evidence([self.sample(0), self.sample(1, backlog=100)], ['leaf1-eth5'], 60))

    def test_csv_has_empty_missing_values_and_keeps_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.csv'
            write_csv(path, [dict(mode='on', rate_mbps=20, rtt_ms=None)])
            self.assertEqual(path.read_text().splitlines(), ['mode,rate_mbps,rtt_ms', 'on,20,'])

    def test_between_trial_variability_excludes_invalid_pairs_and_keeps_missing_rtt(self):
        common = dict(scenario='noisy-neighbor', mode='on', source='h2', target='h6', offered_mbps=15,
                      validity='valida', pair_validity='valida', rtt_mean_ms=None)
        trials = [dict(common, goodput_mbps=14), dict(common, goodput_mbps=15),
                  dict(common, goodput_mbps=1, pair_validity='non dimostrativa')]
        row = aggregate_trials(trials)[0]
        self.assertEqual((row['n_total'], row['n_valid']), (3, 2))
        self.assertEqual(row['goodput_mbps_mean'], 14.5)
        self.assertAlmostEqual(row['goodput_mbps_between_trial_sd'], 2 ** -.5)
        self.assertIsNone(row['rtt_mean_ms_mean'])

    def test_cancelled_job_restores_mode_and_exports_status(self):
        lab = Mock(qos_mode='off')
        lab.status.return_value = {'network_alive': True}
        with tempfile.TemporaryDirectory() as directory:
            job = Experiment(lab, output=Path(directory) / 'run')
            job.cancel.set()
            job.run()
            lab.set_qos.assert_called_once_with('off')
            self.assertEqual(job.status()['state'], 'cancelled')
            self.assertTrue((job.path / 'status.json').exists())

    def test_ecmp_probe_waits_for_delayed_datapath_counters(self):
        lab = Mock(qos_mode='off')
        with tempfile.TemporaryDirectory() as directory:
            job = Experiment(lab, output=Path(directory) / 'run')
            job.path.mkdir()
            flow = job.flow('h2', 'h6', 15, 40000, 5201, 'victim')
            with patch.object(job, 'host_run'), patch.object(job.cancel, 'wait', return_value=False), \
                    patch.object(job, 'bucket_counts', side_effect=[[0, 0], [0, 0], [0, 60]]) as counters:
                self.assertEqual(job.probe(flow), [6])
            self.assertEqual(counters.call_count, 3)
            self.assertEqual(json.loads((job.path / 'calibration.jsonl').read_text())['bucket_deltas'], [0, 60])

    def test_concurrent_packet_injection_is_blocked(self):
        from src.visualizer_support import ManagedLab
        lab = ManagedLab()
        lab.experiment = SimpleNamespace(running=True)
        with self.assertRaisesRegex(RuntimeError, 'esperimento'):
            lab.run_test('h1', 'arp-request')
        lab.experiment = None
        lab.close()


if __name__ == '__main__':
    unittest.main()
