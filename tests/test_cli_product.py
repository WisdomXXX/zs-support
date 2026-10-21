"""Regressions for the customer-facing command flow and supplied topic updates."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zs_support.cli import main, parser, UI, execute
from zs_support.config import load, topic_groups, topics
from zs_support.domain import SupportError


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name + "/config",
                                         "XDG_STATE_HOME": self.temp.name + "/state"})
        self.env.start()
        self.context = {"product": "zsm1", "device_id": "test-device", "boards": {
            "body": {"status": "connected", "host": "body-test"},
            "compute": {"status": "connected", "host": "compute-test"}}}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_no_command_matches_help_without_prompt_or_network(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("help must not prompt")), \
             patch("zs_support.cli.Devices", side_effect=AssertionError("help must not connect")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main([]), 0)
        self.assertEqual(output.getvalue(), parser().format_help())
        self.assertEqual(errors.getvalue(), "")
        command_lines = [line.split()[0] for line in output.getvalue().splitlines()
                         if line.startswith('    ') and line.split()[0] != '<命令>']
        self.assertEqual(command_lines, ['connect', 'info', 'bag', 'status', 'stop', 'pull', 'doctor', 'completion'])
        for alias in ('rec', 'log', 'map', 'collect'):
            self.assertEqual(parser().parse_args([alias]).command, alias)

    def test_q_returns_cleanly_without_error(self):
        with patch("builtins.input", return_value='q'), patch('sys.stdin.isatty', return_value=True), \
             patch('zs_support.cli.Devices.connect', side_effect=AssertionError('cancel must not connect')):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main(['connect']), 0)
            self.assertEqual(output.getvalue(), '')
            self.assertTrue(errors.getvalue().endswith('已取消。\n'))
            self.assertNotIn('错误', errors.getvalue())

    def run_map(self, argv, config=None, connected=True):
        args = parser().parse_args(argv)
        with patch("zs_support.cli.Devices") as devices, patch("zs_support.cli.Collection") as collection:
            if not connected:
                self.context['boards']['compute'] = {'status': 'unavailable'}
            devices.return_value.current.return_value = self.context
            collection.return_value.list.return_value = {'items': [], 'roots': [], 'errors': []}
            with contextlib.redirect_stderr(io.StringIO()):
                result, code = execute(args, config or {}, Mock(), UI(args))
            return result, code, collection.return_value

    def test_maps_always_use_compute_even_when_config_defaults_to_body(self):
        for argv in (['pull', 'map', '--list', '--json'], ['map', '--list', '--json']):
            _, code, collection = self.run_map(argv, {'board': 'body'})
            self.assertEqual(code, 0)
            collection.list.assert_called_once_with('compute', 'map', None)

    def test_map_only_collection_needs_no_board_selection(self):
        _, _, collection = self.run_map(['pull', 'all', '--kinds', 'map', '--json'], {'board': 'body'})
        collection.pull.assert_called_once_with('compute', 'map', None, None, False, False)

    def test_mixed_collection_uses_body_for_logs_and_compute_for_map(self):
        args = parser().parse_args(['pull', 'all', '--kinds', 'log', 'map', '--board', 'body', '--json'])
        with patch('zs_support.cli.Devices') as devices, patch('zs_support.cli.Collection') as collection:
            devices.return_value.current.return_value = self.context
            collection.return_value.logs.return_value = {'state': 'completed'}
            collection.return_value.pull.return_value = {'state': 'completed'}
            with contextlib.redirect_stderr(io.StringIO()):
                _, code = execute(args, {}, Mock(), UI(args))
            self.assertEqual(code, 0)
            collection.return_value.logs.assert_called_once_with('body', None, None, False, False)
            collection.return_value.pull.assert_called_once_with('compute', 'map', None, None, False, False)

    def test_maps_do_not_fall_back_to_body_if_compute_unavailable(self):
        with self.assertRaises(SupportError) as error:
            self.run_map(['pull', 'map', '--list', '--json'], connected=False)
        self.assertEqual(error.exception.reason, 'board_unavailable')
        self.assertIn('地图位于算力板', str(error.exception))

    def test_explicit_body_for_map_is_rejected(self):
        with self.assertRaises(SupportError) as error:
            self.run_map(['pull', 'map', '--board', 'body', '--list', '--json'])
        self.assertEqual(error.exception.code, 2)

    def test_pull_summary_hides_internal_evidence_but_json_preserves_it(self):
        data = {'kind': 'bag', 'state': 'completed', 'destination': '/downloads/one',
                'files': [{'name': 'bag.db3', 'size': 100}], 'bytes': 100,
                'verified': True, 'hashes': False, 'path_attempts': [{'requested_path': '/remote',
                'symlink': False, 'is_dir': True, 'size': 4096}], 'source_manifest': {'private': 'evidence'}}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            UI(parser().parse_args(['pull'])).emit(data)
        self.assertIn('录包拉取完成', output.getvalue())
        self.assertIn('文件清单与大小通过', output.getvalue())
        for internal in ('hashes', 'False', 'path_attempts', 'requested_path', 'symlink', 'is_dir', 'source_manifest'):
            self.assertNotIn(internal, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            UI(parser().parse_args(['pull', '--json'])).emit(data)
        self.assertEqual(json.loads(output.getvalue())['data'], data)

    def test_nested_partial_failure_and_scan_error_remain_visible(self):
        data = {'items': [{'kind': 'log', 'state': 'failed', 'source': '/log',
                          'error': {'message': '权限不足'}}],
                'scan_errors': [{'path': '/bags', 'error': {'message': '读取失败'}}]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            UI(parser().parse_args(['pull'])).emit(data, 1)
        self.assertIn('权限不足', output.getvalue())
        self.assertIn('读取失败', output.getvalue())

    def test_packaged_topics_match_current_supplied_product_files(self):
        supplied = Path(__file__).resolve().parents[1] / 'Add_data/topics'
        for product, filename in [('zsl1', 'record_topics_xg.json'), ('zsl2', 'record_topics_xgl2.json'),
                                  ('zsm1', 'record_topics_zg.json')]:
            self.assertEqual(topic_groups(product), json.loads((supplied / filename).read_text()))
        groups = topic_groups('zsm1')
        for scene, count in [('navigation', 183), ('slam', 18), ('localization', 18)]:
            self.assertEqual(len(groups[scene]['topics']), count)
            selected = topics('zsm1', [scene])
            for new in ['/odom/mujoco_odom', '/odom/slam_odom', '/tf_manager/odom_type']:
                self.assertIn(new, selected)
            self.assertEqual(len(selected), len(set(selected)))


if __name__ == '__main__':
    unittest.main()
