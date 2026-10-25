import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning import target
from engine.provisioning.contract import normalize_user_data
from engine.provisioning.state import ProvisionError, atomic_write, safe_path
from tests.test_provision_contract import example


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_invalid_public_key_rejected_before_host_enrollment(self):
        with self.assertRaisesRegex(ProvisionError, 'cryptographic validation'):
            target.validate_keys(normalize_user_data(example()))

    def test_administrator_preserves_existing_keys_and_uses_narrow_writes(self):
        home = self.root / 'home/adam'
        (home / '.ssh').mkdir(parents=True)
        (home / '.ssh/authorized_keys').write_text('existing-key\n')
        account = SimpleNamespace(pw_dir='/home/adam', pw_uid=os.getuid(), pw_gid=os.getgid(), pw_shell='/bin/bash')
        commands = []

        def command(args, **kwargs):
            commands.append(args)
            if args == ['hostname']:
                return 'provider.example.com'
            if args == ['id', '-nG', 'adam']:
                return 'adam sudo docker'
            return ''

        def mapped(path):
            path = Path(path)
            return safe_path(path if path.is_relative_to(self.root) else self.root / str(path).lstrip('/'))

        def write(path, content, mode=0o600, **kwargs):
            # Keep real file writes; privileged ownership is the isolated boundary.
            kwargs.pop('uid', None)
            kwargs.pop('gid', None)
            atomic_write(mapped(path), content, mode, **kwargs)

        with patch.object(target.pwd, 'getpwnam', return_value=account), \
                patch.object(target, 'safe_path', side_effect=mapped), \
                patch.object(target, 'atomic_write', side_effect=write), \
                patch.object(target, 'run', side_effect=command):
            spec = normalize_user_data(example())
            target.administrator(spec)
            target.administrator(spec)
        content = (home / '.ssh/authorized_keys').read_text().splitlines()
        self.assertEqual(content, ['existing-key', 'ssh-ed25519 PUBLIC_TEST_KEY operator'])
        self.assertIn(['hostnamectl', 'set-hostname', spec.hostname], commands)
        self.assertEqual((self.root / 'etc/sudoers.d/99-apex-adam').stat().st_mode & 0o777, 0o440)
        self.assertFalse((self.root / 'etc/machine-id').exists())
        self.assertFalse((self.root / 'etc/network').exists())

    def test_completion_requires_committed_access_and_preserves_revisions(self):
        state = self.root / 'state'
        (state / 'access').mkdir(parents=True)
        record = {'status': 'enrolled', 'repository': {'commit': 'nodecommit', 'commons_commit': 'commonscommit'}}
        atomic_write(state / 'enrollment.json', json.dumps(record))
        atomic_write(state / 'access/state.json', json.dumps({'status': 'final'}))
        with patch.object(target, 'STATE', state), patch.object(target, 'preflight'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(target.main(['complete']), 1)
            self.assertEqual(json.loads((state / 'enrollment.json').read_text())['status'], 'enrolled')
            atomic_write(state / 'access/state.json', json.dumps({'status': 'committed'}))
            self.assertEqual(target.main(['complete']), 0)
        actual = json.loads((state / 'enrollment.json').read_text())
        self.assertEqual(actual['external_access'], 'verified')
        self.assertEqual(actual['repository'], record['repository'])

    def test_preflight_rejects_wrong_os_before_any_enrollment(self):
        with patch.object(target.os, 'geteuid', return_value=0), \
                patch.object(target.platform, 'machine', return_value='x86_64'), \
                patch.object(Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'):
            with self.assertRaisesRegex(ProvisionError, 'Debian 13'):
                target.preflight()
