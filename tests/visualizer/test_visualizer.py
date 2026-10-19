"""Contratti GUI/core, ciclo di vita e adattamento dei contatori."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.runtime_status import write_status
from src.visualizer_support import (
    ROOT, ManagedLab, SwitchSnapshot, flow_rows, group_rows, load_model, topology_data,
)
from tests.network.flow_visualizer import Bucket, Flow, Group, GroupSnapshot


class DataTests(unittest.TestCase):
    def test_graph_contains_spares_and_exact_mininet_ports(self):
        model = load_model()
        nodes, links = topology_data(model)
        self.assertEqual(len(nodes), model.leaves + model.spines
                         + model.leaves * model.hosts_per_leaf)
        self.assertEqual(len(links), model.leaves * model.spines
                         + model.leaves * model.hosts_per_leaf)
        spare = next(node for node in nodes if node['name'] == 'h4')
        self.assertEqual((spare['kind'], spare['vlan'], spare['ip']), ('Spare', None, None))
        self.assertIn({'source': 'leaf1', 'target': 'spine2',
                       'source_port': 6, 'target_port': 1}, links)
        overlapping = [node for node in nodes if node.get('ip') == '10.0.0.1']
        self.assertEqual({node['vlan'] for node in overlapping}, {101, 102})

    def test_flow_reset_is_unavailable_instead_of_zero(self):
        flow = Flow(0x47460040, 0, 200, 10, 200, 20, ('ip',), 'drop', 'raw')
        before = SwitchSnapshot([flow], GroupSnapshot(), 'before')
        after = SwitchSnapshot([replace(flow, packets=1)], GroupSnapshot(), 'after')
        row = flow_rows(after, before)[0]
        self.assertEqual(row['Confronto'], 'reset')
        self.assertIsNone(row['Delta pkt'])
        self.assertEqual(flow_rows(before, before)[0]['Delta pkt'], 0)

    def test_bucket_shares_use_deltas_and_preserve_missing_values(self):
        group = Group(1, 'select', (Bucket(1, 'output:5', 10, 100),
                                    Bucket(1, 'output:6', 20, 200)), 10, 30, 300)
        after = replace(group, duration=20, packets=50, byte_count=500,
                        buckets=(Bucket(1, 'output:5', 25, 250), Bucket(1, 'output:6', 25, 250)))
        baseline = SwitchSnapshot([], GroupSnapshot((group,)), 'before')
        current = SwitchSnapshot([], GroupSnapshot((after,)), 'after')
        rows, notices = group_rows(current, baseline)
        self.assertEqual([row['Quota pkt (%)'] for row in rows], [75, 25])
        self.assertEqual(notices, [])
        rows, notices = group_rows(baseline, baseline)
        self.assertTrue(all(row['Quota pkt (%)'] is None for row in rows))
        self.assertIn('nessun incremento', notices[0])
        unavailable = replace(after, buckets=(Bucket(1, 'output:5'), after.buckets[1]))
        rows, notices = group_rows(SwitchSnapshot([], GroupSnapshot((unavailable,)), ''), baseline)
        self.assertTrue(all(row['Delta pkt'] is None for row in rows))
        self.assertTrue(all(row['Quota pkt (%)'] is None for row in rows))

    def test_status_replaces_snapshot_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            write_status(path, {'ready': [1]})
            write_status(path, {'ready': [1, 2]})
            self.assertEqual(json.loads(path.read_text()), {'ready': [1, 2]})
            self.assertFalse(path.with_suffix('.tmp').exists())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lab = ManagedLab()
        self.addCleanup(self.lab.close)

    def ready_lab(self):
        self.lab.controller = Mock()
        self.lab.network = Mock()
        self.lab.controller.poll.return_value = self.lab.network.poll.return_value = None
        write_status(self.lab.path / 'controller.json', {'expected': [1, 101], 'ready': [1, 101]})
        write_status(self.lab.path / 'network.json', {'state': 'running', 'hosts': {'h2': 456}})

    def test_tests_require_both_processes_and_all_barrier_confirmations(self):
        self.ready_lab()
        self.assertTrue(self.lab.status()['ready'])
        write_status(self.lab.path / 'controller.json', {'expected': [1, 101], 'ready': [1]})
        with self.assertRaisesRegex(RuntimeError, 'READY'):
            self.lab.run_test('h2', 'arp-request')
        self.lab.controller.poll.return_value = 1
        self.assertFalse(self.lab.status()['ready'])
        self.assertEqual(self.lab.status()['ready_count'], 0)

    @patch('src.visualizer_support.subprocess.run')
    @patch('src.visualizer_support.Path.read_bytes', return_value=b'bash\0mininet:h2\0')
    def test_test_command_enters_selected_host_namespace_and_reads_json(self, read, run):
        self.ready_lab()
        run.return_value = SimpleNamespace(returncode=0, stdout='{"sent_packets": 1}', stderr='')
        self.assertEqual(self.lab.run_test('h2', 'arp-request', target_mac='00:00:00:00:00:05'),
                         {'sent_packets': 1})
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ['mnexec', '-a', '456'])
        self.assertIn('--json', command)
        self.assertEqual(command[command.index('--iface') + 1], 'h2-eth0')
        self.assertEqual(command[command.index('--target-mac') + 1], '00:00:00:00:00:05')
        self.assertNotIn('shell', run.call_args.kwargs)

    @patch('src.visualizer_support.subprocess.run')
    @patch('src.visualizer_support.Path.read_bytes', return_value=b'bash\0mininet:h6\0')
    def test_recycled_pid_never_sends_traffic(self, read, run):
        self.ready_lab()
        with self.assertRaisesRegex(RuntimeError, 'PID'):
            self.lab.run_test('h2', 'arp-request')
        run.assert_not_called()

    @patch('src.visualizer_support.os.geteuid', return_value=0)
    @patch('src.visualizer_support.available_switches', return_value=['leaf1'])
    @patch('src.visualizer_support.subprocess.Popen')
    def test_start_refuses_to_replace_external_network(self, spawn, switches, uid):
        with self.assertRaisesRegex(RuntimeError, 'Esistono già bridge'):
            self.lab.start()
        spawn.assert_not_called()

    def test_stop_waits_for_mininet_cleanup_before_stopping_controller(self):
        self.ready_lab()
        network, controller = self.lab.network, self.lab.controller
        order = []
        network.wait.side_effect = lambda **kwargs: order.append('mininet stopped')
        controller.terminate.side_effect = lambda: order.append('controller stopped')
        self.lab.stop()
        self.assertEqual(order, ['mininet stopped', 'controller stopped'])
        self.assertFalse(self.lab.status()['running'])

    def test_qos_switch_restarts_only_controller_and_waits_for_ready(self):
        self.ready_lab()
        controller, network = self.lab.controller, self.lab.network
        with patch.object(self.lab, '_start_controller') as start, patch.object(self.lab, 'wait_ready') as wait:
            self.lab.set_qos('off')
        controller.terminate.assert_called_once_with()
        network.terminate.assert_not_called()
        self.assertEqual(self.lab.qos_mode, 'off')
        start.assert_called_once_with()
        wait.assert_called_once_with(None)

    def test_only_one_background_experiment_is_allowed(self):
        self.ready_lab()
        self.lab.experiment = SimpleNamespace(running=True)
        with self.assertRaisesRegex(RuntimeError, 'già attivo'):
            self.lab.start_experiment()
        self.lab.experiment = None


class HeadlessTests(unittest.TestCase):
    def test_headless_publishes_host_pids_and_waits_without_cli(self):
        from datacenter import run_network
        host = Mock(name='host')
        host.name, host.pid = 'h2', 456
        net = Mock(hosts=[host])
        stopped = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.json'
            args = SimpleNamespace(headless=True, state_file=str(path))
            run_network(net, args, stopped)
            self.assertEqual(json.loads(path.read_text()),
                             {'state': 'running', 'hosts': {'h2': 456}})
        stopped.wait.assert_called_once_with()
        self.assertEqual(host.cmd.call_count, 3)

    @patch('datacenter.create_network')
    @patch('sys.argv', ['datacenter.py', '--headless'])
    def test_startup_failure_still_stops_mininet(self, create):
        from datacenter import main
        create.return_value.start.side_effect = RuntimeError('startup failed')
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            main()
        create.return_value.stop.assert_called_once_with()


class DashboardTests(unittest.TestCase):
    def test_navigation_and_model_defaults_without_running_simulation(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest('Dipendenze GUI opzionali non installate')
        # La navigazione non richiede socket Scapy né un namespace di rete reale.
        packet_api = SimpleNamespace(CASES=('arp-request', 'ecmp-udp'))
        with patch('src.visualizer_support.available_switches', return_value=[]), \
                patch.dict('sys.modules', {'tests.network.packet_io': packet_api}):
            app = AppTest.from_file(str(ROOT / 'visualizer.py')).run(timeout=15)
            self.assertEqual(list(app.exception), [])
            app.sidebar.radio[0].set_value('Test').run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.selectbox[0].value, 'h2')
            self.assertEqual(app.selectbox[1].value, 'h6')
            self.assertTrue(next(button for button in app.button if button.label == 'Avvia test').disabled)
            app.sidebar.radio[0].set_value('Statistiche').run()
            self.assertEqual(list(app.exception), [])
            app.sidebar.radio[0].set_value('QoS').run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(next(button for button in app.button if button.label == 'Avvia esperimento').disabled)


if __name__ == '__main__':
    unittest.main()
