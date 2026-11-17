import base64
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
        source = Path(__file__).resolve().parents[1] / "scripts/bootstrap"
        self.launcher = self.root / "bootstrap"
        shutil.copy(source, self.launcher)
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
        lines = (self.root / "payload").read_text().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], "example")
        self.assertEqual(lines[1], "ssh://git@git.example:2222/fleet/example.git")
        self.assertEqual(lines[2], "git.example")
        self.assertEqual(base64.b64decode(lines[3]).decode(), self.public.strip())
        secret = (self.root / "deploy").read_text()
        self.assertEqual(base64.b64decode(lines[4]).decode(), secret.rstrip() + "\n")
        args_and_output = (self.root / "args").read_text() + run.stdout + run.stderr
        self.assertNotIn(secret.strip().splitlines()[1], args_and_output)
        self.assertNotIn("groupadd", args_and_output)
        self.assertNotIn("apt-get", args_and_output)

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
