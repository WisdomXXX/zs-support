import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from zs_support.cli import main, parser, complete
from zs_support.config import load, merge, topics, topic_groups, product_key
from zs_support.domain import SupportError, Endpoint
from zs_support.services.devices import Devices
from zs_support.services.versions import parse_version, classify, release_versions, numeric_version
from zs_support.storage import Store
from zs_support.transports.ssh import SSHTransport, ssh_error

FIXTURES = Path(__file__).parent / "fixtures"


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.cfg = load(system=self.root / "none")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_real_m1_samples(self):
        body = parse_version((FIXTURES / "m1_body.yaml").read_text())
        compute = parse_version((FIXTURES / "m1_compute.yaml").read_text())
        self.assertEqual(body.value, "0.3.3")
        self.assertEqual(compute.value, "Hermes_M_v0.3.1")
        self.assertEqual(classify({"status": "connected", "version": compute.to_dict()})["type"], "navigation")

    def test_real_release_listing(self):
        names = [line.split()[-1] for line in (FIXTURES / "zsl1_release.txt").read_text().splitlines() if line.startswith("-")]
        self.assertEqual(release_versions(names), ["622002862AIC"])
        self.assertEqual(release_versions(["602abcd", "622b.yaml", "rootfs_1.yaml"]), ["602abcd", "622b"])

    def test_version_failures_and_manual_evidence(self):
        for text in ("", "version: [bad]", "version: x\nversion: y", "modules:\n  version: Hermes_x", "version: !unsafe a"):
            self.assertEqual(parse_version(text).status, "invalid")
        for text in ("nx v0.2.3", "NX V0.12.0", "nxv0.1.2"):
            self.assertEqual(classify({"status": "connected", "version": parse_version("version: " + text).to_dict()})["type"], "standard")
        self.assertEqual(classify({"status": "unavailable"})["type"], "unknown")
        result = classify({"status": "unavailable"}, override="navigation")
        self.assertEqual(result["source"], "人工指定")
        self.assertEqual(result["detected"], "unknown")
        self.assertEqual(classify({"status": "declared_absent"}, body_only=True)["type"], "standard")
        self.assertEqual(classify({"status": "connected"}, body_only=True)["type"], "unknown")
        self.assertGreater(numeric_version("v0.10.0"), numeric_version("0.3.2"))

    def test_config_precedence_and_validation(self):
        system, user = self.root / "system.json", self.root / "user.json"
        system.write_text('{"min_free_bytes": 10, "scan_depth": 2}')
        user.write_text('{"min_free_bytes": 20}')
        with patch.dict(os.environ, {"ZS_SUPPORT_MIN_FREE_BYTES": "30"}):
            self.assertEqual(load(system=system, user=user)["min_free_bytes"], 30)
            cfg = load({"min_free_bytes": 40}, system, user)
            self.assertEqual(cfg["min_free_bytes"], 40)
            self.assertEqual(cfg["scan_depth"], 2)
        with self.assertRaises(SupportError):
            load({"min_free_bytes": -1}, system, user)
        user.write_text("[]")
        with self.assertRaises(SupportError):
            load(system=system, user=user)

    def test_products_credentials_and_scenes(self):
        for alias in ("xg", "L1W", "小狗", "zsl1w"):
            self.assertEqual(product_key(alias, self.cfg), "zsl1")
        devices = Devices(self.cfg, Store(self.root))
        small = devices.endpoint("zsl1", "body", "192.168.234.1")
        m1 = devices.endpoint("zsm1", "body", "192.168.234.1")
        self.assertEqual(small.user, "firefly")
        self.assertEqual(m1.user, "robot")
        self.assertEqual(m1.password, "bot")
        self.assertNotIn("password=", repr(m1))
        self.assertEqual(self.cfg["products"]["zsm1"]["boards"]["body"]["log_scripts"], ["/opt/runtime/bin/get_robot_log.sh"])
        for product in self.cfg["products"]:
            groups = topic_groups(product)
            scene = next(k for k, v in groups.items() if v["topics"])
            selected = topics(product, [scene, scene], ["/custom"], [groups[scene]["topics"][0]])
            self.assertEqual(len(selected), len(set(selected)))
            self.assertNotIn(groups[scene]["topics"][0], selected)
            self.assertIn("/custom", selected)

    def test_cli_json_and_no_prompt(self):
        for argv, expected in [(["--json"], 2), (["rec", "--bad", "--json"], 2),
                               (["rec", "--json"], 3)]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), expected)
            self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertTrue(parser().parse_args(["--json", "status", "--local"]).json)
        self.assertTrue(parser().parse_args(["status", "--json", "--local"]).json)

    def test_explicit_connect_is_lightweight(self):
        context = {"product": "zsm1", "boards": {}, "algorithm": {"type": "navigation"}}
        out = io.StringIO()
        with patch.object(Devices, "connect", return_value=context) as connect, \
             patch("zs_support.services.recording.Recording.preview", side_effect=AssertionError("No ROS on connect")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["connect", "--product", "m1", "--json"]), 0)
        self.assertEqual(connect.call_args.args[0], "m1")
        self.assertEqual(json.loads(out.getvalue())["data"]["product"], "zsm1")

    def test_bag_is_recording_and_pull_supports_data_types(self):
        bag = parser().parse_args(["bag", "--scene", "localization", "--duration", "3"])
        self.assertEqual(bag.scene, ["localization"])
        self.assertEqual(bag.duration, 3)
        pull = parser().parse_args(["pull", "log", "--board", "body"])
        self.assertEqual(pull.kind, "log")
        self.assertEqual(pull.board, "body")

    def test_noninteractive_connect_requires_product_without_reusing_stale_context(self):
        Store().write("context.json", {"product": "zsl1"})
        out = io.StringIO()
        with patch.object(Devices, "connect") as connect, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["connect", "--json"]), 2)
        connect.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"]["reason"], "selection_required")

    def test_completion_is_local(self):
        args = parser().parse_args(["__complete", "scenes", "--product", "M1"])
        with patch.object(SSHTransport, "call", side_effect=AssertionError("No SSH during completion")):
            self.assertIn("localization", complete(args, Store(self.root)))

    def test_host_validation_and_errors(self):
        for host in ("host;touch /tmp/bad", "-oProxyCommand=bad", "x\nother"):
            with self.assertRaises(SupportError):
                SSHTransport(Endpoint("zsm1", "body", host, "robot", "bot"), self.cfg)
        self.assertEqual(ssh_error(255, "Host key verification failed").reason, "host_key_changed")
        self.assertEqual(ssh_error(255, "Permission denied").code, 5)

    def test_fallback_entry_checks_identity(self):
        def factory(endpoint, config):
            mock = Mock()
            if endpoint.host.endswith("168.168"):
                mock.call.side_effect = SupportError("unreachable", 4, "connection_failed")
            else:
                mock.call.return_value = {"id": "other", "boot_id": "boot", "home": "/home/robot",
                                          "hardware": "unknown", "version_status": "ok", "version_text": "version: 0.3.3"}
            return mock
        devices = Devices(self.cfg, Store(self.root), factory)
        with self.assertRaises(SupportError) as error:
            devices.probe_board("zsm1", "body", expected="original")
        self.assertEqual(error.exception.reason, "identity_mismatch")

    def test_compute_failure_not_standard(self):
        devices = Devices(self.cfg, Store(self.root))
        def probe(product, board, *args):
            if board == "compute":
                raise SupportError("unreachable", 4, "connection_failed")
            return {"status": "connected", "id": "body", "host": "host", "version": {"value": "0.3.3"}}, None
        with patch.object(devices, "probe_board", side_effect=probe):
            self.assertEqual(devices.connect("m1")["algorithm"]["type"], "unknown")


if __name__ == "__main__":
    unittest.main()
