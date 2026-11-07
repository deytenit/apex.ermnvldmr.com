import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class BootstrapClientTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(__file__).resolve().parents[1] / "scripts/bootstrap-node"
        self.launcher = self.root / "bootstrap-node"
        shutil.copy(source, self.launcher)
        (self.root / "bootstrap-node-remote.py").write_text("pass\n")
        for name in ("operator", "deploy"):
            subprocess.run(["ssh-keygen", "-qt", "ed25519", "-N", "", "-f", str(self.root / name)],
                           check=True)
        self.public = (self.root / "operator.pub").read_text()
        (self.root / "known").write_text("[git.example]:2222 " + self.public)
        ssh = self.root / "ssh"
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > args\ncat > payload\n')
        ssh.chmod(0o700)

    def launch(self, *extra):
        with mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"]):
            return subprocess.run(
                [str(self.launcher), "--target", "admin@node", "--repo",
                 "ssh://git@git.example:2222/fleet/example.git", "--operator-key", "operator.pub",
                 "--deploy-key", "deploy", "--known-hosts", "known", *extra],
                cwd=self.root, capture_output=True, text=True,
            )

    def test_custom_git_port_and_secrets_delivered_via_stdin(self):
        run = self.launch()
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads((self.root / "payload").read_text())
        self.assertEqual(payload["node_name"], "example")
        secret = (self.root / "deploy").read_text()
        self.assertEqual(payload["deploy_key"], secret)
        self.assertNotIn(secret.strip().splitlines()[1], (self.root / "args").read_text()
                         + run.stdout + run.stderr)

    def test_git_host_pin_for_default_port_rejected(self):
        (self.root / "known").write_text("git.example " + self.public)
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertFalse((self.root / "payload").exists())

    def test_reused_operator_key_rejected(self):
        self.assertNotEqual(self.launch("--deploy-key", "operator").returncode, 0)
        self.assertFalse((self.root / "payload").exists())

    def test_encrypted_deploy_key_rejected(self):
        subprocess.run(["ssh-keygen", "-pq", "-P", "", "-N", "test", "-f",
                        str(self.root / "deploy")], check=True, capture_output=True)
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertFalse((self.root / "payload").exists())
