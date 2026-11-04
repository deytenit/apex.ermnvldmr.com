import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock

from engine import overlay

backup = overlay.load_module(str(Path(__file__).resolve().parents[1] / 'actions/backup/run.py'),
                             'test_backup_action')


class BackupRecovery(unittest.TestCase):
    def test_failed_attempt_preserves_last_success_and_records_new_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            backup.write_backup_status(path, 'success', 100)
            backup.write_backup_status(path, 'running', 200)
            backup.write_backup_status(path, 'failed', 210)
            status = json.loads(path.read_text())
            self.assertEqual(status['last_success'], 100)
            self.assertEqual(status['state'], 'failed')
            self.assertEqual(status['updated_at'], 210)

    def test_recovers_namespace_owner_before_dependent_without_starting_other_containers(self):
        running = {'komf': False, 'komga': False, 'intentionally-stopped': False}
        def docker(command, **kwargs):
            if command[1] == 'inspect':
                return SimpleNamespace(returncode=0, stdout=json.dumps([
                    {'Id': name, 'State': {'Running': running[name]}} for name in command[2:]]))
            name = command[2]
            if name == 'komf' and not running['komga']:
                return SimpleNamespace(returncode=1)
            running[name] = True
            return SimpleNamespace(returncode=0)
        ctx = SimpleNamespace(sys=SimpleNamespace(run=docker), log=Mock())
        backup.restore_containers(ctx, ['komf', 'komga'])
        self.assertTrue(running['komf'])
        self.assertTrue(running['komga'])
        self.assertFalse(running['intentionally-stopped'])

    def test_unrecoverable_container_prevents_success(self):
        def docker(command, **kwargs):
            return SimpleNamespace(returncode=0 if command[1] == 'inspect' else 1,
                                   stdout='[{"Id":"broken","State":{"Running":false}}]')
        ctx = SimpleNamespace(sys=SimpleNamespace(run=docker), log=Mock())
        with self.assertRaisesRegex(RuntimeError, 'broken'):
            backup.restore_containers(ctx, ['broken'])
