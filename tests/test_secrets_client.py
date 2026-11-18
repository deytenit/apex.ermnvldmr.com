import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SecretsClientTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(__file__).resolve().parents[1] / "scripts/secrets"
        self.launcher = self.root / "secrets"
        shutil.copy(source, self.launcher)
        self.repo = self.root / "node.example.com"
        (self.repo / "compositions/apex").mkdir(parents=True)
        (self.repo / "compositions/apex/.enc.env").write_text("TOKEN=ENC[test]\n")
        sops = self.root / "sops"
        sops.write_text('#!/bin/sh\nprintf "TOKEN=supersecret\\n"\n')
        sops.chmod(0o700)
        ssh = self.root / "ssh"
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > args\ncat > payload\n')
        ssh.chmod(0o700)

    def test_decrypts_and_streams_via_stdin(self):
        with mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"]):
            run = subprocess.run(
                [str(self.launcher), "--target", "adam@node", "--repo", str(self.repo)],
                cwd=self.root, capture_output=True, text=True,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = (self.root / "payload").read_text().splitlines()
        self.assertEqual(lines[0], "/srv/node.example.com")
        self.assertEqual(lines[1], "apex")
        self.assertEqual(base64.b64decode(lines[2]).decode(), "TOKEN=supersecret\n")
        self.assertNotIn("supersecret", (self.root / "args").read_text() + run.stdout + run.stderr)

    def test_decrypts_tier0_enc_files_and_streams_with_gitignore(self):
        t0 = self.repo / "compositions/apex/@tier0/xray/data"
        t0.mkdir(parents=True)
        (t0 / "config.enc.jsonc").write_text("{}\n")
        with mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"]):
            run = subprocess.run(
                [str(self.launcher), "--target", "adam@node", "--repo", str(self.repo)],
                cwd=self.root, capture_output=True, text=True,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = (self.root / "payload").read_text().splitlines()
        self.assertIn("apex/@tier0/xray/data/config.jsonc", lines)
        self.assertIn("apex/@tier0/.gitignore", lines)
        gi_idx = lines.index("apex/@tier0/.gitignore")
        gi_content = base64.b64decode(lines[gi_idx + 1]).decode()
        self.assertIn("/xray/data/config.jsonc", gi_content)
