import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from engine.lib.sys import Sys

spec = importlib.util.spec_from_file_location('compose_lint', Path(__file__).resolve().parents[1] / 'actions/util/lint-docker-compose.py')
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.log = Mock()
        self.ctx = SimpleNamespace(log=self.log, sys=Sys(self.log), node=None,
                                   paths=SimpleNamespace(repo_root=str(self.repo), compositions=str(self.repo / 'compositions')))

    def stage(self, name, content):
        (self.repo / name).write_text(content)
        subprocess.run(['git', '-C', str(self.repo), 'add', name], check=True)

    def test_documentation_commit_does_not_require_docker(self):
        self.stage('README.md', 'Documentation\n')
        with patch.object(lint.shutil, 'which', return_value=None):
            self.assertEqual(lint.run(self.ctx, SimpleNamespace(hook=True)), 0)

    def test_compose_change_still_requires_docker(self):
        self.stage('docker-compose.yml', 'services: {}\n')
        with patch.object(lint.shutil, 'which', return_value=None):
            self.assertNotEqual(lint.run(self.ctx, SimpleNamespace(hook=True)), 0)
