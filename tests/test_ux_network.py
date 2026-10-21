import contextlib
import io
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zs_support.cli import UI, finish_recording, watch_recording, execute, parser
from zs_support.domain import SupportError
from zs_support.transports.network import select_interface, prefer_direct_hosts, route_details


ADDRESSES = [
    {"ifname": "corp0", "flags": ["UP"], "addr_info": [{"family": "inet", "local": "192.168.30.2", "prefixlen": 24}]},
    {"ifname": "robot0", "flags": ["UP"], "addr_info": [{"family": "inet", "local": "192.168.234.2", "prefixlen": 24}]},
]


def network(*args):
    if args[:2] == ("address", "show"):
        return ADDRESSES
    if args[0] == "link":
        return []
    if "oif" in args:
        return [{"dev": "robot0", "gateway": "192.168.234.1"}]
    return [{"dev": "Meta", "gateway": "198.18.0.2"}]


class NetworkAndInteractionTests(unittest.TestCase):
    def test_finished_recording_renders_saved_result_with_scene_names(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            UI(SimpleNamespace()).emit({"id": "c" * 32, "path": "/home/robot/bag",
                                       "state": "completed", "board": "compute",
                                       "scenes": ["navigation"], "topics": ["/odom"],
                                       "elapsed_seconds": 2})
        self.assertIn("录制状态：已完成", output.getvalue())
        self.assertIn("已录制时长：2 s", output.getvalue())
        self.assertIn("zs-support pull", output.getvalue())

    @patch("zs_support.transports.network.ip_json", side_effect=network)
    def test_robot_nic_selected_over_proxy_and_direct_ap_preferred(self, _):
        self.assertEqual(select_interface("192.168.234.1"), "robot0")
        self.assertEqual(select_interface("192.168.168.100"), "robot0")
        self.assertEqual(prefer_direct_hosts(["192.168.168.168", "192.168.234.1"]),
                         ["192.168.234.1", "192.168.168.168"])
        details = route_details("192.168.168.100", "robot0")
        self.assertEqual(details["route_command"], "sudo ip route add 192.168.168.0/24 via 192.168.234.1 dev robot0")
        self.assertEqual(details["route"][0]["dev"], "Meta")

    @patch("zs_support.transports.network.ip_json", return_value=[])
    def test_unknown_interface_is_rejected(self, _):
        with self.assertRaises(SupportError) as error:
            select_interface("192.168.168.100", "absent0")
        self.assertEqual(error.exception.reason, "invalid_interface")
        self.assertIsNone(select_interface("192.168.168.100"))

    def test_foreground_ctrl_c_stops_exact_session_and_restores_handler(self):
        recording = Mock()
        recording.refresh.return_value = {"id": "a" * 32, "state": "completed"}
        session = {"id": "a" * 32, "state": "recording"}
        previous = signal.getsignal(signal.SIGINT)
        stderr = io.StringIO()
        with patch("zs_support.cli.time.sleep", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(stderr):
            result, code = watch_recording(recording, session, SimpleNamespace(duration=None), None)
        self.assertEqual(code, 0)
        self.assertIn("正在录制", stderr.getvalue())
        self.assertIn("已录制 0 s", stderr.getvalue())
        recording.refresh.assert_called_once_with("a" * 32, stop=True)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assertEqual(result["state"], "completed")

    def test_second_interrupt_cannot_break_stop_cleanup(self):
        recording = Mock()
        seen = []
        def stopping(*args, **kwargs):
            seen.append(signal.getsignal(signal.SIGINT))
            raise SupportError("lost connection", 4, "connection_failed")
        recording.refresh.side_effect = stopping
        previous = signal.getsignal(signal.SIGINT)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SupportError):
            finish_recording(recording, "b" * 32, None)
        self.assertEqual(seen, [signal.SIG_IGN])
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_repeated_stop_without_active_task_succeeds(self):
        args = parser().parse_args(["stop", "--json"])
        with patch("zs_support.cli.Devices") as devices, patch("zs_support.cli.Recording") as recording:
            devices.return_value.current.return_value = {"product": "zsm1", "device_id": "test", "boards": {}}
            recording.return_value.active.return_value = []
            with contextlib.redirect_stderr(io.StringIO()):
                result, code = execute(args, {}, Mock(), UI(args))
            self.assertEqual(code, 0)
            recording.return_value.refresh.assert_not_called()
            self.assertIn("没有正在录制", result["message"])


if __name__ == "__main__":
    unittest.main()
