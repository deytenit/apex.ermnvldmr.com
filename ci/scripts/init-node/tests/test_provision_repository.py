import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning.contract import Enrollment, Repository
from engine.provisioning.repository import reconcile_repository
from engine.provisioning.state import ProvisionError


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.upstream = self.root / 'upstream'
        self.commons = self.root / 'commons'
        self.srv = self.root / 'srv'
        self.srv.mkdir()
        self.home = self.root / 'adam'
        self.home.mkdir()
        for path in (self.upstream, self.commons):
            path.mkdir()
            self.git(['init', '-q'], path)
            self.git(['config', 'user.name', 'Test'], path)
            self.git(['config', 'user.email', 'test@example.invalid'], path)
        (self.commons / 'marker').write_text('commons')
        (self.commons / 'etc').mkdir()
        (self.commons / 'etc/.vimrc').write_text('set number\n')
        self.git(['add', '.'], self.commons)
        self.git(['commit', '-qm', 'initial'], self.commons)
        (self.upstream / 'node.env').write_text('APEX_NODE_FQDN=metis.example.com\nAPEX_ENROLLMENT_INTERFACE=1\n')
        (self.upstream / 'init.sh').write_text(
            '#!/bin/sh\nset -eu\ngit config core.hooksPath commons/githooks\n'
            'printf \'export PATH="%s/commons:$PATH"\\n\' "$PWD" > "$HOME/.bashrc"\n'
            'ln -sf "$PWD/commons/etc/.vimrc" "$HOME/.vimrc"\n')
        self.git(['-c', 'protocol.file.allow=always', 'submodule', 'add', str(self.commons), 'commons'], self.upstream)
        self.git(['add', '.'], self.upstream)
        self.git(['commit', '-qm', 'initial'], self.upstream)
        self.spec = Enrollment('metis.example.com', Repository('metis', str(self.upstream), None), (), (), ())
        self.state = {}
        self.saved = []
        self.account = patch('engine.provisioning.repository.pwd.getpwnam',
                             return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home)))
        self.account.start()
        self.addCleanup(self.account.stop)
        self.runner = patch('engine.provisioning.repository.as_adam', side_effect=self.local_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    @staticmethod
    def git(args, path):
        return subprocess.run(['git', *args], cwd=path, text=True, capture_output=True, check=True).stdout.strip()

    def local_run(self, args, cwd=None):
        if args[0] == 'git':
            args = ['git', '-c', 'protocol.file.allow=always', *args[1:]]
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True,
                              env={**os.environ, 'HOME': str(self.home)}).stdout.strip()

    def enroll(self):
        return reconcile_repository(self.spec, self.state, lambda: self.saved.append(True), self.srv)

    def test_repeat_does_not_follow_new_upstream_commit(self):
        first = self.enroll()
        (self.upstream / 'new').write_text('upstream advanced')
        self.git(['add', '.'], self.upstream)
        self.git(['commit', '-qm', 'advance'], self.upstream)
        second = self.enroll()
        self.assertEqual(first['commit'], second['commit'])
        self.assertFalse((self.srv / 'metis' / 'new').exists())
        self.assertTrue(self.state['repository']['hook_complete'])

    def test_local_changes_are_preserved_and_stop_initialization(self):
        self.enroll()
        path = self.srv / 'metis' / 'node.env'
        path.write_text('local changes')
        with self.assertRaisesRegex(ProvisionError, 'local changes'):
            self.enroll()
        self.assertEqual(path.read_text(), 'local changes')

    def test_unrecorded_checkout_is_not_adopted(self):
        (self.srv / 'metis').mkdir()
        with self.assertRaisesRegex(ProvisionError, 'adoption'):
            self.enroll()

    def test_noop_hook_cannot_claim_success(self):
        (self.upstream / 'init.sh').write_text('true\n')
        self.git(['add', '.'], self.upstream)
        self.git(['commit', '-qm', 'noop'], self.upstream)
        with self.assertRaises((ProvisionError, subprocess.CalledProcessError)):
            self.enroll()
        self.assertFalse(self.state['repository'].get('hook_complete', False))

    def test_missing_or_unsupported_interface_stops_before_hook(self):
        for version in ('', 'APEX_ENROLLMENT_INTERFACE=999\n'):
            with self.subTest(version=version):
                (self.upstream / 'node.env').write_text('APEX_NODE_FQDN=metis.example.com\n' + version)
                self.git(['add', '.'], self.upstream)
                self.git(['commit', '-qm', 'unsupported interface'], self.upstream)
                self.spec = Enrollment('metis.example.com', Repository('missing-' + str(len(version)), str(self.upstream), None), (), (), ())
                self.state = {}
                with self.assertRaisesRegex(ProvisionError, 'enrollment interface'):
                    self.enroll()
                self.assertFalse((self.home / '.bashrc').exists())
                self.assertNotIn('hook_complete', self.state['repository'])

    def test_retry_checks_hook_state_instead_of_completion_marker(self):
        self.enroll()
        self.git(['config', 'core.hooksPath', 'wrong-path'], self.srv / 'metis')
        with self.assertRaisesRegex(ProvisionError, 'hook configuration'):
            self.enroll()

    def test_ordinary_commons_directory_is_not_a_pinned_submodule(self):
        self.git(['rm', '-f', 'commons'], self.upstream)
        (self.upstream / 'commons').mkdir()
        (self.upstream / 'commons/ordinary').write_text('no submodule')
        self.git(['add', '.'], self.upstream)
        self.git(['commit', '-qm', 'ordinary directory'], self.upstream)
        with self.assertRaisesRegex(ProvisionError, 'pinned Git submodule'):
            self.enroll()

    def test_mismatched_node_hostname_stops_before_hook(self):
        (self.upstream / 'node.env').write_text('APEX_NODE_FQDN=wrong.example.com\n')
        self.git(['add', '.'], self.upstream)
        self.git(['commit', '-qm', 'wrong'], self.upstream)
        with self.assertRaisesRegex(ProvisionError, 'hostname'):
            self.enroll()
        self.assertNotIn('hook_complete', self.state['repository'])
