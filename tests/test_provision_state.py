import json
import os
from pathlib import Path
import tempfile
import unittest

from engine.provisioning.state import (Journal, ProvisionError, atomic_write,
                                      check_heartbeat, exclusive_lock)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_atomic_private_file_and_symlink_rejection(self):
        path = self.root / 'state' / 'record'
        atomic_write(path, 'first')
        atomic_write(path, 'second')
        self.assertEqual(path.read_text(), 'second')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        link = self.root / 'redirect'
        link.symlink_to(path.parent)
        with self.assertRaises(ProvisionError):
            atomic_write(link / 'record', 'corruption')
        self.assertEqual(path.read_text(), 'second')

    def test_lock_rejects_competing_initializer_and_releases(self):
        path = self.root / 'lock'
        with exclusive_lock(path):
            with self.assertRaises(ProvisionError):
                with exclusive_lock(path):
                    self.fail('competing lock acquired')
        with exclusive_lock(path):
            pass

    def test_failed_stage_is_retryable_and_success_is_reverified(self):
        path = self.root / 'journal'
        record = Journal(path, {'hostname': 'metis'}, 'rev', 'digest')
        with self.assertRaises(RuntimeError):
            record.stage('package', lambda: (_ for _ in ()).throw(RuntimeError('SECRET')))
        self.assertNotIn('SECRET', path.read_text())
        self.assertEqual(json.loads(path.read_text())['failed_stage'], 'package')
        record = Journal(path, {'hostname': 'metis'}, 'rev', 'digest')
        results = iter(({'version': 'one'}, {'version': 'two'}))
        record.stage('package', lambda: next(results))
        record.stage('package', lambda: next(results))
        self.assertEqual(record.data['stages']['package']['result']['version'], 'two')

    def test_identity_or_bundle_change_cannot_retarget_partial_run(self):
        path = self.root / 'journal'
        Journal(path, {'hostname': 'metis'}, 'rev', 'digest').save()
        for identity, digest in (({'hostname': 'other'}, 'digest'), ({'hostname': 'metis'}, 'other')):
            with self.assertRaises(ProvisionError):
                Journal(path, identity, 'rev', digest)

    def test_unsupported_interface_cannot_resume_and_revisions_survive_retry(self):
        path = self.root / 'journal'
        Journal(path, {'hostname': 'metis'}, 'source-rev', 'digest').save()
        resumed = Journal(path, {'hostname': 'metis'}, 'source-rev', 'digest')
        self.assertEqual(resumed.data['baseline_revision'], 'source-rev')
        for version in (None, 999):
            resumed.data['enrollment_interface'] = version
            resumed.save()
            with self.assertRaisesRegex(ProvisionError, 'enrollment interface'):
                Journal(path, {'hostname': 'metis'}, 'source-rev', 'digest')

    def test_expired_heartbeat_prevents_next_operation(self):
        beat = self.root / 'heartbeat'
        beat.touch()
        os.utime(beat, (100, 100))
        check_heartbeat(beat, now=110)
        with self.assertRaises(ProvisionError):
            check_heartbeat(beat, now=200)
        record = Journal(self.root / 'journal', {}, 'rev', 'digest')
        called = []
        with self.assertRaises(ProvisionError):
            record.stage('next', lambda: called.append(True), beat)
        self.assertEqual(called, [])
