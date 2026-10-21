import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zs_support.config import load
from zs_support.domain import Endpoint, SupportError, TaskControl
from zs_support.transports.ssh import SSHTransport

# An isolated local SSH stand-in. Real sshpass and rsync still execute and stream data.
FAKE_SSH = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
Path(os.environ['TEST_SSH_ARGS']).write_text(json.dumps(args))
while args and args[0].startswith('-'):
    flag=args.pop(0)
    if flag in ('-p','-o','-l'): args.pop(0)
args.pop(0)
os.execv('/bin/bash',['bash','-c',' '.join(args)])
'''


@unittest.skipUnless(shutil.which("sshpass") and shutil.which("rsync"), "requires local sshpass/rsync")
class ProcessTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "ssh").write_text(FAKE_SSH)
        (binary / "ssh").chmod(0o755)
        self.env = patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"],
                                         "XDG_CONFIG_HOME": str(self.root / "config"), "TEST_SSH_ARGS": str(self.root / "argv.json")})
        self.env.start()
        self.cfg = load(system=self.root / "missing")
        self.transport = SSHTransport(Endpoint("zsm1", "body", "local-test", "robot", "secret-test-value"), self.cfg)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_real_rsync_protects_chinese_spaces_and_metacharacters(self):
        source = self.root / "空 格 ' $(touch HACK) ; 地图"
        source.mkdir()
        (source / "file with space.txt").write_bytes(os.urandom(400000))
        destination = self.root / "download"
        events = []
        self.transport.download(str(source), destination, True, TaskControl(events.append))
        self.assertEqual((source / "file with space.txt").read_bytes(), (destination / "file with space.txt").read_bytes())
        self.assertNotIn("secret-test-value", (self.root / "argv.json").read_text())
        self.assertFalse((Path.cwd() / "HACK").exists())

    def test_protocol_and_timeout(self):
        result = self.transport.call("paths", paths=[str(self.root), "/no/such/zs-support-path"])
        self.assertEqual([p["status"] for p in result["paths"]], ["ok", "missing"])
        self.assertNotIn("secret-test-value", (self.root / "argv.json").read_text())
        with self.assertRaises(SupportError) as error:
            self.transport.run("sleep 10", timeout=.3)
        self.assertEqual(error.exception.reason, "timeout")

    def test_large_request_after_slow_ssh_start_receives_eof(self):
        payload = os.urandom(1024 * 1024)
        result = self.transport.run("sleep .6; python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'",
                                    stdin=payload, timeout=5)
        import hashlib
        self.assertEqual(result.stdout.strip(), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
