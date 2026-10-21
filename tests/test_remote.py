"""Run the real wire protocol and process lifecycle against a local fake ROS CLI.

No network/robot is accessed. Only processes launched by these tests are signalled.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from zs_support.config import load
from zs_support.domain import CommandResult, Endpoint, SupportError, TaskControl
from zs_support.storage import Store
from zs_support.services.devices import Devices
from zs_support.services.recording import Recording
from zs_support.services.collection import Collection
from zs_support.transports.ssh import SSHTransport
from zs_support.transports import remote_agent as agent

FAKE_ROS = r'''#!/usr/bin/env python3
import os, sys, signal, time, sqlite3
from pathlib import Path
args = sys.argv[1:]
if args == ['bag', 'record', '--help']:
    print('--storage --qos-profile-overrides-path'); sys.exit(0)
if args == ['bag', 'list', 'storage']:
    print('sqlite3 mcap'); sys.exit(0)
if args in (['topic', 'list'], ['topic', 'list', '--no-daemon'], ['topic', 'list', '--no-daemon', '-t']):
    print('/visible'); sys.exit(0)
if args[:2] == ['bag', 'record']:
    path = Path(args[args.index('-o')+1]); path.mkdir()
    with sqlite3.connect(path / 'data.db3') as db:
        db.execute('create table messages(id integer primary key, data blob)')
        if not os.environ.get('TEST_ROS_EMPTY'):
            db.execute('insert into messages(data) values (?)', (b'fake ROS data',))
    def stop(*args):
        (path / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  version: 5\n')
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    print('Recording...', flush=True)
    while True: time.sleep(.1)
sys.exit(2)
'''


class LocalTransport(SSHTransport):
    def run(self, command, stdin=b"", timeout=30):
        result = subprocess.run(["bash", "-c", command], input=stdin, capture_output=True, timeout=timeout)
        return CommandResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    def download(self, source, destination, is_dir, control=None):
        if control:
            control.check()
        destination = Path(destination)
        destination.mkdir(exist_ok=True, parents=True)
        if is_dir:
            shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("link"))
        else:
            shutil.copy2(source, destination / Path(source).name)


class RemoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "机器人 HOME"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "ros2").write_text(FAKE_ROS)
        (self.bin / "ros2").chmod(0o755)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                         "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.cfg = load({"min_free_bytes": 0, "stop_timeout": 5}, system=self.root / "none")
        self.version_file = self.home / "version.yaml"
        self.version_file.write_text("version: 0.3.3\n")
        self.cfg["products"]["zsm1"]["boards"]["body"]["version_path"] = str(self.version_file)
        self.transport = LocalTransport(Endpoint("zsm1", "body", "local", "robot", "bot"), self.cfg)
        self.ident = self.transport.call("probe")
        self.store = Store(self.root / "store")
        board = {"status": "connected", "id": self.ident["id"], "host": "local", "home": str(self.home),
                 "version": {"status": "ok", "value": "0.3.3"}, "boot_id": self.ident["boot_id"]}
        self.store.write("context.json", {"product": "zsm1", "device_id": self.ident["id"], "algorithm": {"type": "unknown"},
                                          "boards": {"body": board, "compute": {"status": "unavailable"}}})
        self.devices = Devices(self.cfg, self.store, LocalTransport)
        self.recording = Recording(self.devices, self.store, self.cfg)
        self.collection = Collection(self.devices, self.store, self.cfg)
        self.sessions = []
        self.archives = []

    def tearDown(self):
        for sid in self.sessions:
            try:
                self.transport.call("record_stop", id=sid, stop_timeout=5, _timeout=10)
            except Exception:
                pass
        for archive in self.archives:
            if archive.exists():
                archive.unlink()
        self.env.stop()
        self.temp.cleanup()

    def test_full_record_restart_stop_and_precise_pull(self):
        session = self.recording.start("body", [], ["/visible", "/later"], remote_path=str(self.home / "空 格 ' ; $()"))
        self.sessions.append(session["id"])
        self.assertEqual(session["state"], "recording")
        self.assertEqual(session["missing_topics"], ["/later"])
        restarted = Recording(self.devices, Store(self.store.root), self.cfg)
        self.assertEqual(restarted.refresh(session["id"])["state"], "recording")
        with self.assertRaises(SupportError) as error:
            restarted.start("body", [], ["/visible"], remote_path=str(self.home / "bags"))
        self.assertEqual(error.exception.reason, "already_recording")
        failed = restarted.get(restarted.pending_session)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual([s["id"] for s in restarted.active()], [session["id"]])
        stopped = restarted.refresh(session["id"], stop=True)
        self.assertEqual(stopped["state"], "completed")
        self.assertEqual(restarted.refresh(session["id"], stop=True)["state"], "completed")
        self.assertEqual(restarted.active(), [])
        result = self.collection.pull("body", "bag", stopped["path"], str(self.root / "downloads"), True)
        self.assertTrue(result["verified"])
        self.assertTrue((Path(result["destination"]) / "data/data.db3").exists())
        manifest = json.loads((Path(result["destination"]) / "collection-manifest.json").read_text())
        self.assertEqual(manifest["path_attempts"], result["path_attempts"])
        self.assertTrue(Path(stopped["path"]).exists())
        again = self.collection.pull("body", "bag", stopped["path"], str(self.root / "downloads"), True)
        self.assertTrue(again["reused"])

    def test_optional_device_metadata_does_not_block_connection(self):
        device = self.home / "device.json"
        request = {"device_path": str(device), "version_path": str(self.version_file)}
        self.assertEqual(self.transport.call("probe", **request)["device"]["status"], "missing")
        device.write_text('{"sn":"04A0164F0100222","tag":"RoamerX","type":"ZSM-1","custom":"ignored"}')
        metadata = self.transport.call("probe", **request)["device"]
        self.assertEqual(metadata["sn"], "04A0164F0100222")
        self.assertEqual(metadata["tag"], "RoamerX")
        self.assertNotIn("custom", metadata)
        device.write_text('[]')
        result = self.transport.call("probe", **request)
        self.assertEqual(result["device"]["status"], "invalid")
        self.assertEqual(result["version_status"], "ok")

    def test_old_rejected_start_is_not_an_active_task(self):
        sid = uuid.uuid4().hex
        self.store.write("sessions/" + sid + ".json", {
            "id": sid, "device_id": self.ident["id"], "board_id": self.ident["id"],
            "product": "zsm1", "board": "body", "state": "unknown",
            "started_at": "2020-01-01T00:00:00+00:00", "path": str(self.home / "never-started")})
        self.assertEqual(self.recording.active(), [])
        self.assertEqual(self.recording.get(sid)["state"], "failed")

    def test_preview_is_reused_but_free_space_is_rechecked(self):
        self.recording.preview("body")
        original = LocalTransport.call
        calls = []
        def track(transport, action, **kwargs):
            calls.append((action, kwargs))
            return original(transport, action, **kwargs)
        with patch.object(LocalTransport, "call", track):
            session = self.recording.start("body", [], ["/visible"])
        self.sessions.append(session["id"])
        self.assertFalse(next(kw for action, kw in calls if action == "preflight")["check_ros"])
        self.assertIn("visible_topics", next(kw for action, kw in calls if action == "record_start")["prepared_ros"])

    def test_updated_m1_slam_scene_reaches_recorder(self):
        session = self.recording.start("body", ["slam"])
        self.sessions.append(session["id"])
        self.assertEqual(len(session["topics"]), 18)
        for topic in ("/odom/current_pose", "/odom/mujoco_odom", "/odom/slam_odom", "/tf_manager/odom_type"):
            self.assertIn(topic, session["remote"]["topics"])
        self.assertEqual(self.recording.refresh(session["id"], stop=True)["state"], "completed")

    def test_pid_reuse_and_boot_change_do_not_signal(self):
        session = self.recording.start("body", [], ["/visible"], remote_path=str(self.home / "bags"))
        self.sessions.append(session["id"])
        meta_file = self.home / ".local/state/zs-support/sessions" / session["id"] / "meta.json"
        original = json.loads(meta_file.read_text())
        altered = {**original, "start_ticks": "0"}
        meta_file.write_text(json.dumps(altered))
        status = self.transport.call("record_stop", id=session["id"], stop_timeout=1)
        self.assertEqual(status["state"], "abnormal")
        self.assertTrue(Path(f"/proc/{original['pid']}").exists())
        altered = {**original, "boot_id": "previous-boot"}
        meta_file.write_text(json.dumps(altered))
        self.assertEqual(self.transport.call("record_stop", id=session["id"])["state"], "abnormal")
        meta_file.write_text(json.dumps(original))

    def test_stop_of_empty_bag_explains_missing_messages(self):
        with patch.dict(os.environ, {"TEST_ROS_EMPTY": "1"}):
            session = self.recording.start("body", [], ["/not_published"])
        self.sessions.append(session["id"])
        stopped = self.recording.refresh(session["id"], stop=True)
        self.assertEqual(stopped["state"], "abnormal")
        self.assertEqual(stopped["remote"]["result"]["exit_code"], 0)
        self.assertIn("录制已停止，但未收到", stopped["remote"]["reason"])
        self.assertEqual(self.recording.active(), [])

    def test_wrong_device_cannot_probe_or_stop(self):
        with self.assertRaises(SupportError) as error:
            self.transport.call("record_stop", id=uuid.uuid4().hex, expected_id="another-device")
        self.assertEqual(error.exception.reason, "identity_mismatch")

    def test_disk_and_ros_preflight_failures(self):
        with self.assertRaises(SupportError) as error:
            self.transport.call("preflight", path=str(self.home / "bags"), min_free_bytes=10**30, ros_setups=[])
        self.assertEqual(error.exception.reason, "disk_full")
        with self.assertRaises(SupportError) as error:
            self.transport.call("preflight", path=str(self.home / "bags"), min_free_bytes=0, ros_setups=["/not/a/ros/setup"])
        self.assertEqual(error.exception.code, 8)

    def test_unsupported_storage_never_starts(self):
        with self.assertRaises(SupportError) as error:
            self.recording.start("body", [], ["/visible"], storage="not-a-plugin")
        self.assertEqual(error.exception.reason, "unsupported_storage")
        self.assertFalse(list((self.home / ".local/state/zs-support/sessions").glob("*/meta.json")))

    def test_scan_bags_by_content_and_depth(self):
        root = self.home / "history"
        (root / "bag_name_only").mkdir(parents=True)
        (root / "ordinary_name").mkdir()
        (root / "ordinary_name" / "sample.db3").write_bytes(b"data")
        (root / "ordinary_name" / "metadata.yaml").write_text("meta")
        (root / "broken").mkdir()
        (root / "broken" / "sample.mcap").write_bytes(b"data")
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "sub" / "deep" / "file.db3").write_bytes(b"deep")
        result = self.transport.call("scan", path=str(root), kind="bag", depth=1)
        self.assertEqual({i["name"] for i in result["items"]}, {"ordinary_name", "broken"})
        self.assertFalse(next(i for i in result["items"] if i["name"] == "broken")["complete"])

    def test_history_scan_preserves_known_abnormal_session(self):
        root = self.home / "history"
        bag = root / "failed-bag"
        bag.mkdir(parents=True)
        (bag / "data.db3").write_bytes(b"allocated but no valid messages")
        (bag / "metadata.yaml").write_text("empty bag metadata")
        sid = uuid.uuid4().hex
        self.store.write("sessions/" + sid + ".json", {"id": sid, "path": str(bag),
                         "device_id": self.ident["id"], "board_id": self.ident["id"],
                         "state": "abnormal", "started_at": "2026-10-21T00:00:00+00:00"})
        self.cfg["products"]["zsm1"]["boards"]["body"]["paths"]["bag"] = [str(root)]
        listing = self.collection.list("body", "bag")
        self.assertEqual(listing["items"][0]["state"], "abnormal")
        self.assertFalse(listing["items"][0]["complete"])

    def test_map_priority_and_history_maps(self):
        first, second = self.home / "userdata/map", self.home / "ota/map"
        (first / "history_map" / "地图 A").mkdir(parents=True)
        second.mkdir(parents=True)
        self.cfg["products"]["zsm1"]["boards"]["body"]["paths"]["map"] = [str(first), str(second)]
        result = self.collection.list("body", "map")
        self.assertEqual(result["roots"][0]["path"], str(first))
        self.assertEqual(result["items"][0]["name"], "地图 A")

    def test_missing_path_manual_and_saved_override(self):
        self.cfg["products"]["zsm1"]["boards"]["body"]["paths"]["map"] = ["/nonexistent/one", "/nonexistent/two"]
        with self.assertRaises(SupportError) as error:
            self.collection.paths("body", "map")
        self.assertEqual(error.exception.reason, "paths_missing")
        self.assertEqual(len(error.exception.details["paths"]), 2)
        source = self.home / "自定义 地图"
        source.mkdir()
        (source / "map.pcd").write_bytes(b"map")
        self.collection.pull("body", "map", str(source), str(self.root / "downloads"), save_path=True)
        self.assertEqual(self.collection.paths("body", "map")[3][0]["path"], str(source))

    def test_transfer_retry_cancel_and_no_remote_delete(self):
        source = self.home / "map"
        source.mkdir()
        (source / "map.pcd").write_bytes(b"map")
        original = LocalTransport.download
        count = [0]
        def flaky(transport, *args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                raise SupportError("network", 4, "connection_failed")
            return original(transport, *args, **kwargs)
        with patch.object(LocalTransport, "download", flaky):
            result = self.collection.pull("body", "map", str(source), str(self.root / "downloads"))
        self.assertEqual(count[0], 2)
        self.assertTrue(result["verified"])
        control = TaskControl()
        control.cancelled.set()
        with self.assertRaises(SupportError) as error:
            Collection(self.devices, self.store, self.cfg, control).pull("body", "map", str(source))
        self.assertEqual(error.exception.reason, "cancelled")
        self.assertTrue((source / "map.pcd").exists())

    def test_manifest_skips_links_and_static_change_fails(self):
        source = self.home / "map"
        source.mkdir()
        (source / "file").write_bytes(b"data")
        (source / "link").symlink_to("/etc/passwd")
        result = self.transport.call("manifest", path=str(source), hashes=True)
        self.assertEqual(result["skipped"], ["link"])
        original = LocalTransport.download
        def changing(transport, *args, **kwargs):
            original(transport, *args, **kwargs)
            (source / "file").write_bytes(b"new data")
        with patch.object(LocalTransport, "download", changing):
            with self.assertRaises(SupportError) as error:
                self.collection.pull("body", "map", str(source), str(self.root / "downloads"))
        self.assertEqual(error.exception.reason, "verification_failed")

    def test_explicit_log_symlink_resolves_but_nested_links_are_skipped(self):
        source = self.home / "dated-log"
        source.mkdir()
        (source / "launch.log").write_text("running")
        (source / "link").symlink_to("/etc/passwd")
        alias = self.home / "robot_launch_log"
        alias.symlink_to(source)
        info = self.transport.call("paths", paths=[str(alias)])["paths"][0]
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["path"], str(source))
        self.assertEqual(info["requested_path"], str(alias))
        result = self.collection.pull("body", "log", str(alias), str(self.root / "downloads"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["skipped"], ["link"])

    def test_runtime_environment_is_loaded_for_topic_probe(self):
        runtime = self.home / "runtime.bash"
        runtime.write_text("export ROS_DOMAIN_ID=77\nexport RMW_IMPLEMENTATION=rmw_zenoh_cpp\n")
        result = self.transport.call("ros", runtime_env=str(runtime), ros_setups=[], ros_overlays=[])
        self.assertEqual(result["environment"]["ROS_DOMAIN_ID"], "77")
        self.assertEqual(result["environment"]["RMW_IMPLEMENTATION"], "rmw_zenoh_cpp")

    def test_known_missing_type_is_reported_and_filtered_before_recording(self):
        original = LocalTransport.call
        unsupported = {"/bad_type": {"types": ["absent/msg/Missing"], "reason": "library missing"}}
        def probe(transport, action, **kwargs):
            result = original(transport, action, **kwargs)
            if action in ("ros", "preflight"):
                result["unsupported_topics"] = unsupported
            return result
        with patch.object(LocalTransport, "call", probe), \
             patch("zs_support.services.recording.topics", return_value=["/visible", "/bad_type"]):
            session = self.recording.start("body", ["navigation"])
            self.sessions.append(session["id"])
        self.assertEqual(session["topics"], ["/visible"])
        self.assertEqual(session["excluded_topics"], unsupported)
        self.assertEqual(self.recording.refresh(session["id"], stop=True)["state"], "completed")
        with patch.object(LocalTransport, "call", probe):
            with self.assertRaises(SupportError) as error:
                self.recording.start("body", [], ["/bad_type"])
        self.assertEqual(error.exception.reason, "unsupported_topic_types")

    def test_empty_sqlite_is_not_recording_readiness(self):
        import sqlite3
        root = self.home / "empty-bag"
        root.mkdir()
        with sqlite3.connect(root / "data.db3") as db:
            db.execute("create table messages(id integer)")
        self.assertFalse(agent.recorded_data(root)["has_messages"])
        with sqlite3.connect(root / "data.db3") as db:
            db.execute("insert into messages values(1)")
        self.assertTrue(agent.recorded_data(root)["has_messages"])

    def test_live_status_uses_recorder_log_without_opening_sqlite(self):
        session = self.recording.start("body", [], ["/visible"])
        self.sessions.append(session["id"])
        with patch.object(agent, "recorded_data", side_effect=AssertionError("must not read live SQLite")):
            self.assertEqual(agent.record_status({"id": session["id"]})["state"], "recording")

    def test_script_failure_fallback_and_cancel_no_fallback(self):
        source = self.home / "log"
        source.mkdir()
        (source / "log.txt").write_text("hello")
        self.cfg["products"]["zsm1"]["boards"]["body"]["paths"]["log"] = [str(source)]
        self.cfg["products"]["zsm1"]["boards"]["body"]["log_scripts"] = ["/definitely/not/get_robot_log.sh"]
        result = self.collection.logs("body", output=str(self.root / "downloads"))
        self.assertEqual(result["method"], "directory_fallback")
        self.assertEqual(result["script_error"]["reason"], "script_missing")
        original = LocalTransport.call
        def cancelled(transport, action, **kwargs):
            if action == "log_archive":
                raise SupportError("cancelled", 1, "cancelled")
            return original(transport, action, **kwargs)
        with patch.object(LocalTransport, "call", cancelled), patch.object(self.collection, "pull") as pull:
            with self.assertRaises(SupportError):
                self.collection.logs("body")
            pull.assert_not_called()

    def _script(self, content):
        sudo = self.bin / "sudo"
        sudo.write_text("#!/usr/bin/env python3\nimport os,sys\nsys.stdin.readline()\na=sys.argv[1:]\na=a[a.index('--')+1:]\nos.execvp(a[0],a)\n")
        sudo.chmod(0o755)
        script = self.home / "脚本 目录" / "get_robot_log.sh"
        script.parent.mkdir(exist_ok=True)
        script.write_text(content)
        self.cfg["products"]["zsm1"]["boards"]["body"]["log_scripts"] = [str(script)]
        return script

    def _archive_path(self):
        path = Path("/tmp/robot_log_" + uuid.uuid4().hex + ".tar.gz")
        self.archives.append(path)
        return path

    def test_script_new_archive_verified_and_downloaded_not_old(self):
        old, archive = self._archive_path(), self._archive_path()
        old.write_bytes(b"old archive must not be selected")
        script = self._script("python3 - <<'PY'\nimport io,tarfile\np=" + repr(str(archive)) + "\nwith tarfile.open(p,'w:gz') as t:\n i=tarfile.TarInfo('robot.log'); i.size=5; t.addfile(i,io.BytesIO(b'hello'))\nprint(p)\nPY\n")
        result = self.collection.logs("body", output=str(self.root / "downloads"))
        self.assertEqual(result["method"], "script")
        self.assertEqual(result["source"], str(archive))
        self.assertEqual(result["archive"]["script"], str(script))
        self.assertTrue(result["verified"])
        self.assertTrue((Path(result["destination"]) / "data" / archive.name).is_file())
        manifest = json.loads((Path(result["destination"]) / "collection-manifest.json").read_text())
        self.assertEqual(manifest["method"], "script")
        self.assertTrue(old.exists())

    def test_script_invalid_archive_no_new_and_sudo_failure(self):
        for mode, reason in [("old", "archive_ambiguous"), ("empty", "archive_empty"),
                             ("corrupt", "archive_corrupt"), ("failure", "script_failed")]:
            with self.subTest(mode=mode):
                archive = self._archive_path()
                if mode == "old":
                    archive.write_bytes(b"old")
                    content = "printf '%s\\n' '" + str(archive) + "'\n"
                elif mode == "failure":
                    content = "exit 1\n"
                else:
                    content = "printf '%s' '" + ("bad archive" if mode == "corrupt" else "") + "' > '" + str(archive) + "'\n"
                script = self._script(content)
                with self.assertRaises(SupportError) as error:
                    self.transport.call("log_archive", scripts=[str(script)], script_timeout=3, _timeout=15)
                self.assertEqual(error.exception.reason, reason)

    def test_script_timeout_and_version_boundary(self):
        script = self._script("sleep 10\n")
        with self.assertRaises(SupportError) as error:
            self.transport.call("log_archive", scripts=[str(script)], script_timeout=1, _timeout=12)
        self.assertEqual(error.exception.reason, "script_timeout")
        source = self.home / "fallback"
        source.mkdir()
        (source / "log").write_text("data")
        self.cfg["products"]["zsm1"]["boards"]["body"]["paths"]["log"] = [str(source)]
        self.cfg["products"]["zsm1"]["boards"]["body"]["log_scripts"] = ["/missing/script"]
        self.version_file.write_text("version: V0.3.2\n")
        result = self.collection.logs("body", output=str(self.root / "downloads"))
        self.assertEqual(result["method"], "directory_fallback")
        self.version_file.write_text("version: 0.3.1\n")
        result = self.collection.logs("body", output=str(self.root / "downloads2"))
        self.assertEqual(result["method"], "directory")

    def test_script_lock_prevents_overlapping_execution(self):
        import fcntl
        script = self._script("exit 0\n")
        parent = self.home / ".local/state/zs-support"
        parent.mkdir(parents=True)
        with (parent / "log-script.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SupportError) as error:
                self.transport.call("log_archive", scripts=[str(script)], script_timeout=3, _timeout=10)
            self.assertEqual(error.exception.reason, "script_busy")

    def test_stop_leaves_foreign_recorder_running(self):
        foreign = subprocess.Popen([str(self.bin / "ros2"), "bag", "record", "-o", str(self.home / "foreign"), "/visible"], start_new_session=True)
        try:
            session = self.recording.start("body", [], ["/visible"])
            self.sessions.append(session["id"])
            self.assertEqual(self.recording.refresh(session["id"], stop=True)["state"], "completed")
            self.assertIsNone(foreign.poll())
        finally:
            foreign.terminate()
            foreign.wait(timeout=5)

    def test_resume_after_failed_transfer_keeps_partial(self):
        source = self.home / "map"
        source.mkdir()
        (source / "map.pcd").write_bytes(b"long map contents")
        self.cfg["retries"] = 0
        def fail(transport, path, destination, is_dir, control):
            Path(destination, "map.pcd").write_bytes(b"long")
            raise SupportError("connection lost", 4, "connection_failed")
        with patch.object(LocalTransport, "download", fail):
            with self.assertRaises(SupportError):
                self.collection.pull("body", "map", str(source), str(self.root / "downloads"))
        partial = list((self.root / "downloads/.partial").glob("*/data/map.pcd"))
        self.assertEqual(len(partial), 1)
        original = LocalTransport.download
        def resume(transport, path, destination, is_dir, control):
            self.assertEqual(Path(destination, "map.pcd").read_bytes(), b"long")
            return original(transport, path, destination, is_dir, control)
        with patch.object(LocalTransport, "download", resume):
            result = self.collection.pull("body", "map", str(source), str(self.root / "downloads"))
        self.assertTrue(result["verified"])
        self.assertFalse(partial[0].exists())


if __name__ == "__main__":
    unittest.main()
